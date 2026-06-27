from http.server import BaseHTTPRequestHandler
import urllib.request
import urllib.parse
import json
import time
import hashlib
import hmac
import sys
import os

# Environment Variables থেকে কনফিগারেশন রিড করা
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Vercel KV-এর ভেরিয়েবল নাম
KV_REST_API_URL = os.environ.get("KV_REST_API_URL")
KV_REST_API_TOKEN = os.environ.get("KV_REST_API_TOKEN")

# Environment থেকে অ্যাডমিন আইডি
ADMIN_ENV = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS = [int(x.strip()) for x in ADMIN_ENV.split(",") if x.strip().isdigit()]

# ==========================================
# ০. Vercel KV Database (Upstash) ফাংশনসমূহ
# ==========================================

def kv_command(command, *args):
    if not KV_REST_API_URL or not KV_REST_API_TOKEN:
        return None
    
    clean_url = KV_REST_API_URL.rstrip('/')
    url = f"{clean_url}/{command}/" + "/".join(urllib.parse.quote(str(a)) for a in args)
    
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"})
    try:
        with urllib.request.urlopen(req) as response:
            res = json.loads(response.read().decode())
            return res.get("result")
    except Exception:
        return None

def get_bot_status():
    status = kv_command("GET", "bot_active")
    return False if status == "false" else True

def set_bot_status(active):
    val = "true" if active else "false"
    kv_command("SET", "bot_active", val)

def add_user(user_id):
    kv_command("SADD", "bot_users", user_id)

def get_user_count():
    count = kv_command("SCARD", "bot_users")
    return count if count is not None else 0

# ওটিপি ট্র্যাকিং ফাংশনসমূহ (Sorted Set)
def log_otp_request():
    now = int(time.time())
    # unique member হিসেবে টাইমস্ট্যাম্প এবং মাইক্রোসেকেন্ড ব্যবহার করা হয়েছে
    member = f"{now}_{time.perf_counter()}"
    kv_command("ZADD", "otp_stats", now, member)

def get_otp_count(seconds_ago):
    now = int(time.time())
    start_time = now - seconds_ago
    # নির্দিষ্ট সময়ের মধ্যে কতগুলো কি (Key) এসেছে তা কাউন্ট করবে
    count = kv_command("ZCOUNT", "otp_stats", start_time, now)
    return count if count is not None else 0

def clean_old_otp_stats():
    # ২৪ ঘণ্টার বেশি পুরোনো ডেটা ডাটাবেজ থেকে অটো ক্লিন করবে (অপটিমাইজেশন)
    one_day_ago = int(time.time()) - 86400
    kv_command("ZREMRANGEBYSCORE", "otp_stats", "-inf", one_day_ago)

# ==========================================
# ১. ওটিপি (TOTP) জেনারেশন লজিক
# ==========================================

def base32_decode(base32_str):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    cleaned = base32_str.replace("=", "").replace(" ", "").upper()
    if not cleaned:
        return b""
    for char in cleaned:
        if char not in alphabet:
            raise ValueError("Invalid Base32")
    binary_str = ""
    for char in cleaned:
        val = alphabet.index(char)
        binary_str += format(val, '05b')
    byte_array = bytearray()
    for i in range(0, len(binary_str) - (len(binary_str) % 8), 8):
        byte_array.append(int(binary_str[i:i+8], 2))
    return bytes(byte_array)

def generate_totp(secret_base32, time_step=30):
    key = base32_decode(secret_base32)
    epoch = int(time.time())
    counter = epoch // time_step
    msg = counter.to_bytes(8, byteorder='big')
    hmac_result = hmac.new(key, msg, hashlib.sha1).digest()
    offset = hmac_result[-1] & 0x0f
    bin_code = ((hmac_result[offset] & 0x7f) << 24 |
                (hmac_result[offset+1] & 0xff) << 16 |
                (hmac_result[offset+2] & 0xff) << 8 |
                (hmac_result[offset+3] & 0xff))
    otp = bin_code % 1000000
    return str(otp).zfill(6)

# ==========================================
# ২. টেলিগ্রাম API ফাংশনসমূহ
# ==========================================

def send_api_request(method, payload):
    url = f"{BASE_URL}/{method}"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode())
    except Exception:
        return None

def send_message(chat_id, text, reply_markup=None, disable_preview=False):
    payload = {
        "chat_id": chat_id, 
        "text": text, 
        "parse_mode": "Markdown",
        "disable_web_page_preview": disable_preview
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return send_api_request("sendMessage", payload)

def edit_message_text(chat_id, message_id, text, reply_markup):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
        "reply_markup": reply_markup
    }
    return send_api_request("editMessageText", payload)

def answer_callback_query(callback_query_id):
    payload = {"callback_query_id": callback_query_id, "show_alert": False}
    send_api_request("answerCallbackQuery", payload)

# ==========================================
# ৩. মেসেজ প্রসেসিং লজিক
# ==========================================

def handle_update(update):
    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"].strip()
        user_id = update["message"]["from"]["id"]
        
        add_user(user_id)
        bot_active = get_bot_status()

        # --- অ্যাডমিন কমান্ডসমূহ ---
        if user_id in ADMIN_IDS:
            if text == "/st":
                status_text = "🟢 Active" if bot_active else "🔴 Off"
                total_users = get_user_count()
                
                # ১ ঘণ্টা (৩৬০০ সেকেন্ড) এবং ২৪ ঘণ্টার ডেটা নিয়ে আসা
                last_1h_otps = get_otp_count(3600)
                today_otps = get_otp_count(86400)
                
                # পুরোনো ডেটা ক্লিন করা (ডাটাবেজ লাইট রাখার জন্য)
                clean_old_otp_stats()
                
                msg = (
                    f"📊 *Bot Admin Dashboard*\n\n"
                    f"👥 Total Lifetime Users: `{total_users}`\n"
                    f"⏱️ Last 1 Hour Keys: `{last_1h_otps}`\n"
                    f"📅 Today Total Keys: `{today_otps}`\n\n"
                    f"⚙️ Bot Status: {status_text}\n"
                    f"🛠️ DB Connection: ✅ Connected\n"
                    f"👑 Admin Access: ✅ Approved"
                )
                send_message(chat_id, msg)
                return
                
            elif text == "/on":
                set_bot_status(True)
                send_message(chat_id, "🟢 বট সফলভাবে *অন (ON)* করা হয়েছে।")
                return
                
            elif text == "/off":
                set_bot_status(False)
                send_message(chat_id, "🔴 বট সফলভাবে *অফ (OFF)* করা হয়েছে।")
                return

        # --- সাধারণ ইউজার কমান্ডসমূহ ---
        if text == "/start":
            if not bot_active:
                send_message(chat_id, "⚠️ দুঃখিত, বটটি বর্তমানে বন্ধ আছে।")
                return
            send_message(chat_id, "আপনার 2FA Key দিন 🔠🔢")
            return

        elif text == "/share":
            bot_info = send_api_request("getMe", {})
            bot_username = bot_info.get("result", {}).get("username", "bot") if bot_info else "bot"
            
            share_text = (
                "📢 *Fastest 2FA TOTP Generator Bot*\n\n"
                "🛡️ আপনার ফেসবুক, জিমেইল, ইনস্টাগ্রাম বা যেকোনো অ্যাকাউন্টের ২-ফ্যাক্টর অথেনটিকেশন (2FA) কোড জেনারেট করুন সম্পূর্ণ নিরাপদে এবং পলকের মধ্যে।\n\n"
                f"🔗 *বট লিংক:* https://t.me/{bot_username}\n\n"
                "নিচের বাটনে ক্লিক করে বন্ধুদের সাথে শেয়ার করুন! 👇"
            )
            
            share_msg = f"🔐 এই বটটি দিয়ে খুব সহজেই ফেসবুক বা জিমেইলের ২-ফ্যাক্টর (2FA) কোড বের করা যায়। ট্রাই করে দেখতে পারো!\n\n🔗 https://t.me/{bot_username}"
            encoded_text = urllib.parse.quote(share_msg)
            
            inline_keyboard = {
                "inline_keyboard": [
                    [{"text": "Share now 🚀", "url": f"https://t.me/share/url?url=https://t.me/{bot_username}&text={encoded_text}"}]
                ]
            }
            send_message(chat_id, share_text, reply_markup=json.dumps(inline_keyboard), disable_preview=False)
            return

        # --- ওটিপি জেনারেশন ---
        if not bot_active:
            send_message(chat_id, "⚠️ দুঃখিত, বটটি বর্তমানে বন্ধ আছে।")
            return

        try:
            otp_code = generate_totp(text)
            
            # ওটিপি রিকোয়েস্ট ডাটাবেজে কাউন্ট বা লগ করা
            log_otp_request()
            
            epoch = int(time.time())
            remaining = 30 - (epoch % 30)
            formatted_otp = f"{otp_code[:3]} {otp_code[3:]}"
            message_text = f"নিচের বাটনে ট্যাপ করে কোড কপি করুন ⤵️\n\n⏳Valid for : {remaining}s"
            
            inline_keyboard = {
                "inline_keyboard": [
                    [{"text": "🔄 Copy " + formatted_otp, "copy_text": {"text": otp_code}}],
                    [{"text": "🔄 Refresh", "callback_data": f"refresh_{text}"}]
                ]
            }
            send_message(chat_id, message_text, reply_markup=json.dumps(inline_keyboard))
        except Exception:
            send_message(chat_id, "❌ *ভুল 2FA Key!* অনুগ্রহ করে সঠিক Key দিন।")

    elif "callback_query" in update:
        callback_id = update["callback_query"]["id"]
        callback_data = update["callback_query"]["data"]
        chat_id = update["callback_query"]["message"]["chat"]["id"]
        message_id = update["callback_query"]["message"]["message_id"]

        bot_active = get_bot_status()
        if not bot_active:
            answer_callback_query(callback_id)
            return

        if callback_data.startswith("refresh_"):
            secret_key = callback_data.replace("refresh_", "")
            try:
                new_otp = generate_totp(secret_key)
                
                # রিফ্রেশ করলেও সেটি নতুন ওটিপি জেনারেশন হিসেবে কাউন্ট হবে
                log_otp_request()
                
                epoch = int(time.time())
                remaining = 30 - (epoch % 30)
                formatted_otp = f"{new_otp[:3]} {new_otp[3:]}"
                message_text = f"নিচের বাটনে ট্যাপ করে কোড কপি করুন ⤵️\n\n⏳Valid for : {remaining}s"
                
                inline_keyboard = {
                    "inline_keyboard": [
                        [{"text": "🔄 Copy " + formatted_otp, "copy_text": {"text": new_otp}}],
                        [{"text": "🔄 Refresh", "callback_data": f"refresh_{secret_key}"}]
                    ]
                }
                edit_message_text(chat_id, message_id, message_text, json.dumps(inline_keyboard))
                answer_callback_query(callback_id)
            except Exception:
                answer_callback_query(callback_id)

# ==========================================
# ৪. Vercel Serverless Request Handler
# ==========================================

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        try:
            update = json.loads(post_data.decode('utf-8'))
            handle_update(update)
        except Exception:
            pass
            
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({"status": "ok"}).encode())

    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-type', 'text/plain; charset=utf-8')
        self.end_headers()
        self.wfile.write("Permanent Multi-Admin Bot is Active! fast".encode())
