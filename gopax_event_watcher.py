# -*- coding: utf-8 -*-
import asyncio
import json
import re
import os
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from bs4 import BeautifulSoup
import discord
from discord.ext import tasks

# ======== 설정 ========
KST = timezone(timedelta(hours=9))
BASE_DIR = Path(__file__).resolve().parent

def load_env(env_path: Path = BASE_DIR / ".env") -> None:
    if not env_path.exists():
        raise FileNotFoundError(f".env 파일을 찾을 수 없습니다: {env_path}")

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f".env에 {name} 값을 설정해주세요.")
    return value

def env_path(name: str, default_path: Path) -> Path:
    value = os.getenv(name)
    path = Path(value) if value else default_path
    if not path.is_absolute():
        path = BASE_DIR / path
    return path

load_env()

CONFIG_PATH = env_path("COIN_CONFIG_PATH", BASE_DIR / "config.json")
DISCORD_TOKEN = require_env("DISCORD_TOKEN")
DISCORD_CHANNEL_ID = int(require_env("DISCORD_CHANNEL_ID"))
SEEN_PATH = env_path("SEEN_NOTICES_PATH", BASE_DIR / "seen_notices.json")
PENDING_PATH = env_path("PENDING_EVENTS_PATH", BASE_DIR / "pending_events.json")

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# ======== 공지 파싱 ========
def fetch_notices():
    res = requests.get('https://api.gopax.co.kr/notices', timeout=10)
    return res.json() if res.status_code == 200 else []

def parse_nbang_event(notice: dict):
    title = notice.get('title', '')
    content = notice.get('content', '')

    soup = BeautifulSoup(content, 'html.parser')
    text = title + '\n' + soup.get_text()

    # N빵 또는 매일 딸깍 이벤트 감지
    event_type = None
    if '매일 딸깍' in text:
        event_type = '매일 딸깍'
    elif 'N빵' in text:
        event_type = 'N빵'
    else:
        return None

    coin = extract_coin(title, text)
    event_section = extract_event_section(text, event_type, coin)

    # 날짜 파싱
    start, end = extract_event_dates(text)

    # 최소 거래금액: 보상/지급 금액보다 "거래", "이상" 주변 금액을 우선합니다.
    min_krw = extract_min_trade_krw(event_section, coin)

    return {
        'notice_id': notice['id'],
        'title': title,
        'coin': coin,
        'start': start,
        'end': end,
        'min_krw': min_krw,
        'event_type': event_type,
    }

def extract_coin(title: str, text: str) -> str | None:
    coin_match = re.search(r'\(([A-Z0-9]{1,10})\)', title)
    if coin_match:
        return coin_match.group(1) + '-KRW'

    coin_match = re.search(r'\b([A-Z0-9]{1,10})\s*(?:N빵|매일 딸깍|데일리|리뉴얼|거래)', text)
    if coin_match:
        return coin_match.group(1) + '-KRW'

    return None

def extract_event_section(text: str, event_type: str, coin: str | None) -> str:
    symbol = coin.split('-')[0] if coin else None
    primary_keywords = []
    fallback_keywords = []

    if event_type == 'N빵':
        if symbol:
            primary_keywords.append(f'{symbol} N빵')
        primary_keywords.extend(['N빵 리워드', 'N빵 이벤트', '매일 거래 N빵', 'N빵'])
    else:
        if symbol:
            primary_keywords.append(f'{symbol} 매일 딸깍')
        primary_keywords.extend([
            '매일 딸깍 거래 이벤트',
            '매일 딸깍',
        ])
        fallback_keywords.extend([
            '일일 참여 최소 거래량',
            '일별 최소 거래량',
        ])

    content_start = text.find('\n') + 1
    positions = find_keyword_positions(text, primary_keywords, content_start)
    if not positions:
        positions = find_keyword_positions(text, fallback_keywords, content_start)
    if not positions:
        positions = find_keyword_positions(text, primary_keywords + fallback_keywords, 0)
    if not positions:
        return text

    start = min(positions)
    tail = text[start:]
    end_positions = []
    for pattern in (r'\d+\.\s', r'※', r'감사합니다'):
        end_match = re.search(pattern, tail[1:])
        if end_match:
            end_positions.append(end_match.start() + 1)

    if end_positions:
        return tail[:min(end_positions)]
    return tail

def find_keyword_positions(text: str, keywords: list[str], min_position: int) -> list[int]:
    positions = []
    for keyword in keywords:
        start = 0
        while True:
            position = text.find(keyword, start)
            if position == -1:
                break
            if position >= min_position:
                positions.append(position)
            start = position + len(keyword)
    return positions

def extract_event_dates(text: str) -> tuple[str | None, str | None]:
    date_match = re.search(
        r'(\d{4})\.(\d{2})\.(\d{2})\([^\)]+\)[^~]*~[^0-9]*(\d{4})\.(\d{2})\.(\d{2})',
        text
    )
    if date_match:
        return (
            f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}",
            f"{date_match.group(4)}-{date_match.group(5)}-{date_match.group(6)}",
        )

    date_match = re.search(
        r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일[^~]*~[^0-9]*(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일',
        text
    )
    if date_match:
        return (
            f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}",
            f"{date_match.group(4)}-{int(date_match.group(5)):02d}-{int(date_match.group(6)):02d}",
        )

    return None, None

def parse_krw_amount(number_text: str, unit: str) -> int:
    number = float(number_text.replace(',', ''))
    if '만' in unit:
        return int(number * 10000)
    return int(number)

def extract_min_trade_krw(text: str, coin: str | None) -> int:
    amount_pattern = re.compile(r'(\d+(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(만\s*원|만원|원)')
    symbol = coin.split('-')[0] if coin else r'[A-Z]{2,10}'
    trade_patterns = [
        rf'(?:일일|일별)?\s*(?:참여\s*)?최소\s*거래량[^:：]*[:：]?\s*.*?(\d+(?:,\d{{3}})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(만\s*원|만원|원)\s*이상[^.\n]*{symbol}[^.\n]*거래',
        rf'(\d+(?:,\d{{3}})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(만\s*원|만원|원)\s*이상[^.\n]*{symbol}[^.\n]*거래',
        rf'{symbol}[^.\n]*?(\d+(?:,\d{{3}})*(?:\.\d+)?|\d+(?:\.\d+)?)\s*(만\s*원|만원|원)\s*이상[^.\n]*거래',
    ]

    for pattern in trade_patterns:
        match = re.search(pattern, text)
        if match:
            return parse_krw_amount(match.group(1), match.group(2))

    candidates = []

    for match in amount_pattern.finditer(text):
        start, end = match.span()
        window = text[max(0, start - 80):min(len(text), end + 80)]
        score = 0

        if '거래' in window:
            score += 5
        if '이상' in window:
            score += 3
        if any(keyword in window for keyword in ('매수', '매도', '합산', '일별', '매일')):
            score += 2
        if coin and coin.split('-')[0] in window:
            score += 2
        if any(keyword in window for keyword in ('보상', '지급', '리워드', '혜택', '당첨', '상금')):
            score -= 4

        amount = parse_krw_amount(match.group(1), match.group(2))
        candidates.append((score, amount, match.group(0), window))

    if not candidates:
        return 100000

    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][1]

def load_seen():
    if SEEN_PATH.exists():
        return set(json.loads(SEEN_PATH.read_text()))
    return set()

def save_seen(seen: set):
    SEEN_PATH.write_text(json.dumps(list(seen)))

def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    return {}

def save_config(cfg: dict):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')

def load_pending():
    if PENDING_PATH.exists():
        return json.loads(PENDING_PATH.read_text(encoding='utf-8'))
    return {}

def save_pending(data: dict):
    PENDING_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')

# ======== 디스코드 버튼 (Persistent) ========
class EventConfirmView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # 영구 활성화

    @discord.ui.button(label='✅ 등록', style=discord.ButtonStyle.success, custom_id='event_confirm')
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = str(interaction.message.id)
        pending = load_pending()
        info = pending.get(msg_id)
        if not info:
            await interaction.response.send_message(
                '⚠️ 만료되었거나 찾을 수 없는 이벤트예요.', ephemeral=True
            )
            return

        coin = info['coin']
        cfg = load_config()
        entry = {
            "amount": info['amount'],
            "start_date": info['start'],
            "end_date": info['end']
        }
        if coin not in cfg:
            cfg[coin] = []
        if not any(e['start_date'] == entry['start_date'] for e in cfg[coin]):
            cfg[coin].append(entry)
            save_config(cfg)
            await interaction.response.send_message(
                f"✅ `{coin}` 등록 완료!\n```json\n{json.dumps(entry, ensure_ascii=False, indent=2)}\n```"
            )
        else:
            await interaction.response.send_message(f"⚠️ `{coin}` 이미 등록되어 있어요.")

        pending.pop(msg_id, None)
        save_pending(pending)

    @discord.ui.button(label='❌ 취소', style=discord.ButtonStyle.danger, custom_id='event_cancel')
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = str(interaction.message.id)
        pending = load_pending()
        pending.pop(msg_id, None)
        save_pending(pending)
        await interaction.response.send_message('취소했어요.')

# ======== 주기적 공지 체크 ========
@tasks.loop(hours=1)
async def check_notices():
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not channel:
        return

    seen = load_seen()
    notices = fetch_notices()

    for notice in notices:
        nid = notice['id']
        if nid in seen:
            continue
        seen.add(nid)
        save_seen(seen)  # 즉시 저장 (중복 방지)

        event = parse_nbang_event(notice)
        if not event or not event['coin']:
            continue

        # 이미 끝난 이벤트 스킵
        if event['end']:
            end_date = datetime.strptime(event['end'], '%Y-%m-%d').date()
            if end_date < datetime.now(KST).date():
                continue

        # 이미 config에 등록된 이벤트 스킵
        cfg = load_config()
        if event['coin'] in cfg:
            existing = [e['start_date'] for e in cfg[event['coin']]]
            if event['start'] in existing:
                continue

        suggested = event['min_krw'] // 2 + 500
        etype = event.get('event_type', 'N빵')
        view = EventConfirmView()
        msg = await channel.send(
            f"🎉 **{etype} 이벤트 발견!**\n"
            f"• 코인: `{event['coin']}`\n"
            f"• 기간: `{event['start']} ~ {event['end']}`\n"
            f"• 최소거래금액: `{event['min_krw']:,}원`\n"
            f"• 매수금액: `{suggested:,}원` (절반+여유)\n"
            f"• 등록할까요?",
            view=view
        )

        # pending에 이벤트 정보 저장 (메시지 ID를 키로)
        pending = load_pending()
        pending[str(msg.id)] = {
            'coin': event['coin'],
            'start': event['start'],
            'end': event['end'],
            'amount': suggested,
        }
        save_pending(pending)

@bot.event
async def on_ready():
    print(f'봇 로그인: {bot.user}', flush=True)
    # Persistent View 등록 (재시작 후에도 버튼 살아있게)
    bot.add_view(EventConfirmView())
    try:
        await check_notices()
        print('check_notices 완료', flush=True)
    except Exception as e:
        print(f'check_notices 에러: {e}', flush=True)
    if not check_notices.is_running():
        check_notices.start()

bot.run(DISCORD_TOKEN)
