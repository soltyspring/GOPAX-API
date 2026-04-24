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
    if 'N빵' in text:
        event_type = 'N빵'
    elif '매일 딸깍' in text:
        event_type = '매일 딸깍'
    else:
        return None

    # 코인명 추출 (제목에서 괄호 안 심볼)
    coin_match = re.search(r'\(([A-Z]{2,10})\)', title)
    coin = coin_match.group(1) + '-KRW' if coin_match else None

    # 이벤트 섹션 찾기
    section_keywords = [
        '매일 거래 N빵', 'N빵 이벤트',          # N빵 계열
        '매일 딸깍 거래 이벤트', '매일 딸깍',    # 매일 딸깍 계열
    ]
    idx = -1
    for kw in section_keywords:
        idx = text.find(kw)
        if idx != -1:
            break
    if idx == -1:
        idx = 0  # 섹션 못 찾으면 전체 텍스트 대상
    event_section = text[idx:]

    # 날짜 파싱
    date_match = re.search(
        r'(\d{4})\.(\d{2})\.(\d{2})\([^\)]+\)[^~]*~[^0-9]*(\d{4})\.(\d{2})\.(\d{2})',
        event_section
    )
    if date_match:
        start = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        end   = f"{date_match.group(4)}-{date_match.group(5)}-{date_match.group(6)}"
    else:
        start = end = None

    # 최소 거래금액
    amount_match = re.search(r'(\d+)만원', event_section)
    min_krw = int(amount_match.group(1)) * 10000 if amount_match else 100000

    return {
        'notice_id': notice['id'],
        'title': title,
        'coin': coin,
        'start': start,
        'end': end,
        'min_krw': min_krw,
        'event_type': event_type,
    }

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
