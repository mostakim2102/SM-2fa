from http.server import BaseHTTPRequestHandler
import urllib.request
import json
import time
import hashlib
import hmac
import sys
import os

# Environment Variables থেকে কনফিগারেশন রিড করা
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# Vercel KV Database Credentials (স্টোরেজ কানেক্ট করলে অটো সেট হয়)
KV_REST_API_URL = os.environ.get("KV_REST_API_URL")
KV_REST_API_TOKEN = os.environ.get("KV_REST_API_TOKEN")

# Environment থেকে অ্যাডমিন আইডিগুলোর লিস্ট তৈরি করা
ADMIN_ENV = os.environ.get("ADMIN_IDS", "")
# কমা দিয়ে আলাদা করা আইডিগুলোকে পাইথন লিস্টে রূপান্তর (যেমন: [123456, 789012])
ADMIN_IDS = [int(x.strip()) for x in ADMIN_ENV.split(",") if x.strip().isdigit()]

# ==========================================
# ০. Vercel KV Database ফাংশনসমূহ (চিরস্থায়ী ডেটার জন্য)
# ==========================================

def kv_command(command, *args):
    """Vercel KV-তে কমান্ড পাঠানোর অফিশিয়াল REST মেকানিজম"""
    if not KV_REST_API_URL or not KV_REST_API_TOKEN:
        return None
    url = f"{KV_REST_API_URL}/{command}/" + "/".join(urllib.parse.quote(str(a)) for a in args)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {KV_REST_API_TOKEN}"})
    try:
        with urllib.request.urlopen(req) as response:
            res = json.loads(response.read().decode())
            return res.get("result")
    except Exception:
        return None

def get_bot_status():
    """বট অন নাকি অফ তা ডাটাবেজ থেকে জানা"""
    status = kv_command("GET", "bot_active")
    return False if status == "false" else True

def set_bot_status(active):
    """বটের অন/অফ স্ট্যাটাস ডাটাবেজে সেভ করা"""
    val = "true" if active else "false"
    kv_command("SET", "bot_active", val)

def add_user(user_id):
    """ইউনিক ইউজার আইডি ডাটাবেজের 'Set'-এ যুক্ত করা (কখনো ডুপ্লিকেট হবে না)"""
    kv_command("SADD", "bot_users", user_id)

def get_user_count():
    """মোট ব্যবহারকারীর সংখ্যা ডাটাবেজ থেকে গোনা"""
    count = kv_command("SCARD", "bot_users")
    return count if count is not None else 0

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
    bot_info = send_api_request("getMe", {})
    bot_username = bot_info["result"]["username"] if bot_info and "result" in bot_info else "YourBot"

    if "message" in update and "text" in update["message"]:
        chat_id = update["message"]["chat"]["id"]
        text = update["message"]["text"].strip()
        user_id = update["message"]["from"]["id"]
        
        # ডাটাবেজে ইউজার যুক্ত করা (স্থায়ী ট্র্যাকিং)
        add_user(user_id)
        
        # ডাটাবেজ থেকে বটের বর্তমান অন/অফ অবস্থা চেক
        bot_active = get_bot_status()

        # --- মাল্টি-অ্যাডমিন কমান্ডসমূহ ---
        if user_id in ADMIN_IDS:
            if text == "/st":
                status_text = "🟢 Active" if bot_active else "🔴 Off"
                total_users = get_user_count()
                msg = (
                    f"📊 *Bot Admin Dashboard*\n\n"
                    f"👥 Total Lifetime Users: `{total_users}`\n"
                    f"⚙️ Bot Status: {status_text}\n"
                    f"🛠️ DB Connection: ✅ Connected\n"
                    f"👑 Admin Access: ✅ Approved"
                )
                send_message(chat_id, msg)
                return
                
            elif text == "/on":
                set_bot_status(True)
                send_message(chat_id, "🟢 বট সফলভাবে *অন (ON)* করা হয়েছে।")
                return
                
            elif text == "/off":
                set_bot_status(False)
                send_message(chat_id, "🔴 বট সফলভাবে *অফ (OFF)* করা হয়েছে।")
                return

        # --- সাধারণ ইউজার কমান্ডসমূহ ---
        if text == "/start":
            if not bot_active:
                send_message(chat_id, "⚠️ দুঃখিত, বটটি বর্তমানে বন্ধ আছে।")
                return
            send_message(chat_id, "আপনার 2FA Key দিন 🔠🔢")
            return

        elif text == "/share":
            share_text = (
                "🔐 *Fastest 2FA TOTP Generator Bot*\n\n"
                "আপনার ফেসবুক, জিমেইল বা যেকোনো অ্যাকাউন্টের ২-ফ্যাক্টর কোড জেনারেট করুন সহজে।\n\n"
                f"🔗 *বট লিংক:* https://t.me/{bot_username}"
            )
            encoded_text = urllib.parse.quote(f"আমি এই চমৎকার ২এফএ ওটিপি বটটি ব্যবহার করছি! চাইলে তুমিও ব্যবহার করতে পারো।\n\n🔗 https://t.me/{bot_username}")
            inline_keyboard = {
                "inline_keyboard": [
                    [{"text": "🚀 বন্ধুদের সাথে শেয়ার করুন", "url": f"https://t.me/share/url?url=https://t.me/{bot_username}&text={encoded_text}"}]
                ]
            }
            send_message(chat_id, share_text, reply_markup=inline_keyboard, disable_preview=False)
            return

        # --- ওটিপি জেনারেশন ---
        if not bot_active:
            send_message(chat_id, "⚠️ দুঃখিত, বটটি বর্তমানে বন্ধ আছে।")
            return

        try:
            otp_code = generate_totp(text)
            epoch = int(time.time())
            remaining = 30 - (epoch % 30)
            formatted_otp = f"{otp_code[:3]} {otp_code[3:]}"
            message_text = f"নিচের বাটনে ট্যাপ করে কোড কপি করুন ⤵️\n\n⏳Valid for : {remaining}s"
            
            inline_keyboard = {
                "inline_keyboard": [
                    [{"text": formatted_otp, "copy_text": {"text": otp_code}}],
                    [{"text": "🔄 Refresh", "callback_data": f"refresh_{text}"}]
                ]
            }
            send_message(chat_id, message_text, reply_markup=inline_keyboard)
        except Exception:
            send_message(chat_id, "❌ *ভুল 2FA Key!* অনুগ্রহ করে সঠিক Key দিন।")

    elif "callback_query" in update:
        callback_id = update["callback_query"]["id"]
        callback_data = update["callback_query"]["data"]
        chat_id = update["callback_query"]["message"]["chat"]["id"]
        message_id = update["update"]["callback_query"]["message"]["message_id"] if "update" in locals() else update["callback_query"]["message"]["message_id"]

        bot_active = get_bot_status()
        if not bot_active:
            answer_callback_query(callback_id)
            return

        if callback_data.startswith("refresh_"):
            secret_key = callback_data.replace("refresh_", "")
            try:
                new_otp = generate_totp(secret_key)
                epoch = int(time.time())
                remaining = 30 - (epoch % 30)
                formatted_otp = f"{new_otp[:3]} {new_otp[3:]}"
                message_text = f"নিচের বাটনে ট্যাপ করে কোড কপি করুন ⤵️\n\n⏳Valid for : {remaining}s"
                
                inline_keyboard = {
                    "inline_keyboard": [
                        [{"text": formatted_otp, "copy_text": {"text": new_otp}}],
                        [{"text": "🔄 Refresh", "callback_data": f"refresh_{secret_key}"}]
                    ]
                }
                edit_message_text(chat_id, message_id, message_text, inline_keyboard)
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
        self.wfile.write("Permanent Multi-Admin Bot is Active!".encode())
