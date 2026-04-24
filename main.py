# -*- coding: utf-8 -*-
import asyncio
import sys
import os
from pathlib import Path

# Windows 전용 이벤트 루프 정책 → 리눅스에서는 기본 루프 사용
if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from datetime import datetime, timezone, timedelta
import base64, hmac, hashlib, requests, json
from telethon import TelegramClient

# ======== 설정 ========
KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent

def load_env(env_path: Path = BASE_DIR / ".env") -> None:
    """간단한 .env 로더: KEY=VALUE 형식을 환경변수로 등록합니다."""
    if not env_path.exists():
        raise FileNotFoundError(f".env 파일을 찾을 수 없습니다: {env_path}")

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)

def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f".env에 {name} 값을 설정해주세요.")
    return value

def load_accounts() -> list[dict[str, str]]:
    accounts = []
    idx = 1

    while True:
        api_key = os.getenv(f"GOPAX_API_KEY_{idx}")
        secret = os.getenv(f"GOPAX_SECRET_{idx}")

        if not api_key and not secret:
            break
        if not api_key or not secret:
            raise RuntimeError(f"GOPAX {idx}번 계정의 API_KEY/SECRET 값이 모두 필요합니다.")

        accounts.append({"API_KEY": api_key, "SECRET": secret})
        idx += 1

    if not accounts:
        raise RuntimeError(".env에 GOPAX 계정 정보가 없습니다.")

    return accounts

def env_path(name: str, default_path: Path) -> Path:
    value = os.getenv(name)
    path = Path(value) if value else default_path
    if not path.is_absolute():
        path = BASE_DIR / path
    return path

load_env()
ACCOUNTS = load_accounts()

config_path = env_path("COIN_CONFIG_PATH", BASE_DIR / "config.json")
with open(config_path, encoding='utf-8') as f:
    COIN_CONFIG = json.load(f)

# ======== Telegram 설정 ========
api_id = int(require_env("TELEGRAM_API_ID"))
api_hash = require_env("TELEGRAM_API_HASH")
client = TelegramClient('session_file', api_id, api_hash)

def is_coin_active(start_date_str, end_date_str):
    today = datetime.now(KST).date()
    if start_date_str:
        start_date = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        if today < start_date:
            return False
    if end_date_str:
        end_date = datetime.strptime(end_date_str, "%Y-%m-%d").date()
        if today > end_date:   # end_date 당일은 포함
            return False
    return True

def get_balance(api_key: str, secret: str, pair: str) -> float:
    """특정 코인의 사용 가능 잔고 조회"""
    coin = pair.split('-')[0]
    nonce = str(int(datetime.now(KST).timestamp() * 1000))
    method = 'GET'
    path = '/balances'

    # 1. 서명 생성
    message = nonce + method + path
    raw_secret = base64.b64decode(secret)
    signature = hmac.new(raw_secret, message.encode(), hashlib.sha512)
    signature_b64 = base64.b64encode(signature.digest()).decode()

    # 2. API 요청
    headers = {
        'API-Key': api_key,
        'Signature': signature_b64,
        'Nonce': nonce
    }
    response = requests.get(
        'https://api.gopax.co.kr' + path,
        headers=headers
    )

    # 3. 잔고 추출
    if response.status_code == 200:
        for asset in response.json():
            if asset['asset'] == coin:
                return float(asset['avail'])
    return 0.0

def create_order(api_key: str, secret: str, pair: str, side: str, amount: float) -> requests.Response:
    """시장가 주문 실행"""
    nonce = str(int(datetime.now(KST).timestamp() * 1000))
    method = 'POST'
    path = '/orders'

    # 1. 주문 정보 구성
    body = {
        "amount": amount,
        "side": side.lower(),
        "tradingPairName": pair,
        "type": "market"
    }

    # 2. 서명 생성
    message = nonce + method + path + json.dumps(body, sort_keys=True)
    raw_secret = base64.b64decode(secret)
    signature = hmac.new(raw_secret, message.encode(), hashlib.sha512)
    signature_b64 = base64.b64encode(signature.digest()).decode()

    # 3. API 요청
    body_str = json.dumps(body, sort_keys=True)
    headers = {
        'API-Key': api_key,
        'Signature': signature_b64,
        'Nonce': nonce,
        'Content-Type': 'application/json'
    }
    return requests.post(
        'https://api.gopax.co.kr' + path,
        headers=headers,
        data=body_str  # json= 대신 data= 로 변경
    )

async def telegram_auth():
    """텔레그램 인증 처리"""
    await client.connect()
    if not await client.is_user_authorized():
        print("Telegram 인증 필요")
        phone_number = require_env("TELEGRAM_PHONE_NUMBER")
        await client.send_code_request(phone_number)
        code = input('인증 코드 입력: ')
        await client.sign_in(phone_number, code)

async def process_account(account: dict, account_idx: int, result_lines: list):
    """계정별 거래 처리"""
    api_key = account["API_KEY"]
    secret = account["SECRET"]

    result_lines.append(f"\n🔑 {account_idx+1}번 계정")

    for pair, conf_list in COIN_CONFIG.items():
        for conf in conf_list:
            krw_amount = float(conf["amount"])  # 원화 금액
            start_date = conf.get("start_date")
            end_date = conf.get("end_date")

            if not is_coin_active(start_date, end_date):
                result_lines.append(f"⏸️ {pair} ({start_date}~{end_date}) 기간외")
                continue

            try:
                # === 매수 (원화 금액 그대로 사용) ===
                buy_res = create_order(api_key, secret, pair, 'buy', krw_amount)
                result_lines.append(
                    f"{pair} 매수 : {buy_res.status_code} {krw_amount:,.0f}원"
                )

                # === 잔고 확인 ===
                await asyncio.sleep(1)
                balance = get_balance(api_key, secret, pair)

                # === 매도 ===
                if balance > 0:
                    sell_amount = round(balance * 0.9999, 8)
                    sell_res = create_order(api_key, secret, pair, 'sell', sell_amount)
                    result_lines.append(
                        f"{pair} 매도: {sell_res.status_code} {sell_amount}"
                    )
                else:
                    result_lines.append(f"⚠️ {pair} ({start_date}~{end_date}) 매수 체결 안 됨 (잔고=0)")

            except Exception as e:
                result_lines.append(f"❌ {pair} ({start_date}~{end_date}) 오류: {str(e)}")

async def main():
    """메인 비동기 함수"""
    await telegram_auth()
    result_lines = ["🎉 GOPAX 자동 거래 시작 🎉"]

    for idx, account in enumerate(ACCOUNTS):
        await process_account(account, idx, result_lines)
        await asyncio.sleep(0.3)

    try:
        await client.send_message('eventbithumb_bot', '\n'.join(result_lines))
        print("✅ 텔레그램 메시지 전송 완료")
    except Exception as e:
        print(f"❌ 메시지 전송 실패: {e}")

if __name__ == '__main__':
    with client:
        client.loop.run_until_complete(main())
