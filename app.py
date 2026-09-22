import os
import json
import time
import random
import binascii
import asyncio
import urllib.parse
from datetime import datetime, timedelta
from collections import defaultdict

import jwt
import requests
import aiohttp
from flask import Flask, request, jsonify, send_from_directory
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson

# استيراد ملفات Protobuf الخاصة باللعبة
import like_pb2
import like_count_pb2
import uid_generator_pb2

# --- الإعدادات والمتغيرات العامة ---
API_KEY = "SIAM"
KEY_LIMIT = 999
TOKEN_CACHE = {}
liked_cache = defaultdict(set)
tracker = defaultdict(lambda: [0, time.time()])  # [عدد الطلبات, وقت التعديل]

app = Flask(__name__)

# --- الدوال المساعدة (Helper Functions) ---

def get_today_midnight_timestamp():
    """الحصول على طابع زمني لمنتصف الليل لليوم الحالي لتصفير العداد."""
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day)
    return midnight.timestamp()

def load_accounts(server_name):
    """تحميل الحسابات (UID:Password) بناءً على السيرفر المحدد."""
    if server_name == "IND":
        filename = "account_ind.txt"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        filename = "account_br.txt"
    elif server_name == "ME":
        filename = "account_me.txt"  # ملف حسابات سيرفر الشرق الأوسط
    else:
        filename = "account_bd.txt"

    if not os.path.exists(filename):
        print(f"⚠️ الملف {filename} غير موجود، جاري استخدام account_ind.txt كبديل")
        filename = "account_ind.txt"
        if not os.path.exists(filename):
            return []

    accounts = []
    try:
        with open(filename, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if ':' in line:
                    uid, password = line.split(':', 1)
                    accounts.append({"uid": uid.strip(), "password": password.strip()})
        return accounts
    except Exception as e:
        print(f"❌ خطأ أثناء قراءة ملف الحسابات: {e}")
        return []

def encrypt_message(plaintext):
    """تشفير البيانات باستخدام AES-CBC مع PKCS7 Padding."""
    key = b'Yg&tc%DEuh6%Zc^8'
    iv = b'6oyZDr22E3ychjM%'
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded_message = pad(plaintext, AES.block_size)
    return binascii.hexlify(cipher.encrypt(padded_message)).decode('utf-8')

def create_protobuf_message(user_id, region):
    message = like_pb2.like()
    message.uid = int(user_id)
    message.region = region
    return message.SerializeToString()

def enc(uid):
    message = uid_generator_pb2.uid_generator()
    message.krishna_ = int(uid)
    message.teamXdarks = 1
    return encrypt_message(message.SerializeToString())

def decode_protobuf(binary):
    try:
        items = like_count_pb2.Info()
        items.ParseFromString(binary)
        return items
    except Exception:
        return None

# --- إدارة الرموز والتفاعل مع API ---

async def generate_jwt_token(uid, password):
    """جلب رمز JWT من خدمة خارجية."""
    try:
        encoded_password = urllib.parse.quote(password)
        url = f"https://jihad-jwt.lovable.app/api/public/token?uid={uid}&password={encoded_password}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=24) as response:
                if response.status == 200:
                    data = await response.json()
                    if isinstance(data, dict):
                        return data.get('jwt_token') or data.get('token')
    except Exception:
        pass
    return None

async def get_valid_token(uid, password):
    """استرجاع توكن صالح من الذاكرة المؤقتة أو توليد توكن جديد."""
    if uid in TOKEN_CACHE:
        cached = TOKEN_CACHE[uid]
        remaining = (cached["expires_at"] - datetime.utcnow()).total_seconds()
        if remaining > 1800:
            return cached["token"]

    token = await generate_jwt_token(uid, password)
    if not token:
        return None

    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        exp = payload.get("exp")
        TOKEN_CACHE[uid] = {
            "token": token,
            "expires_at": datetime.utcfromtimestamp(exp)
        }
    except Exception:
        TOKEN_CACHE[uid] = {
            "token": token,
            "expires_at": datetime.utcnow() + timedelta(hours=24)
        }

    return token

async def send_like(encrypted_uid, token, url):
    """إرسال طلب الإعجاب إلى خوادم اللعبة."""
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            'Authorization': f"Bearer {token}",
            'Content-Type': "application/x-www-form-urlencoded",
            'X-GA': "v1 1",
            'ReleaseVersion': "OB55"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers, timeout=5) as response:
                return response.status
    except Exception:
        return 500

async def process_account(target_uid, encrypted_uid, account, url, semaphore):
    """معالجة حساب واحد مع التحكم في معدل التزامن."""
    async with semaphore:
        token = await get_valid_token(account['uid'], account['password'])
        if not token:
            return 500, account['uid']
        
        status = await send_like(encrypted_uid, token, url)
        if status == 200:
            liked_cache[target_uid].add(account['uid'])
        return status, account['uid']

async def send_all_likes(target_uid, server_name, url):
    """إرسال الإعجابات باستخدام جميع الحسابات المتاحة بشكل غير متزامن."""
    protobuf_message = create_protobuf_message(target_uid, server_name)
    encrypted_uid = encrypt_message(protobuf_message)
    
    accounts = load_accounts(server_name)
    if not accounts: 
        return {'success': 0, 'failed': 0, 'total': 0, 'already_liked': 0}
    
    already_liked = liked_cache.get(target_uid, set())
    fresh_accounts = [acc for acc in accounts if acc['uid'] not in already_liked]
    
    if not fresh_accounts:
        return {
            'success': 0, 
            'failed': 0, 
            'total': len(accounts),
            'already_liked': len(already_liked),
            'fresh_used': 0
        }
    
    random.shuffle(fresh_accounts)
    semaphore = asyncio.Semaphore(25)
    tasks = [
        process_account(target_uid, encrypted_uid, acc, url, semaphore)
        for acc in fresh_accounts[:2000]
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    successful = sum(1 for r in results if isinstance(r, tuple) and r[0] == 200)
    failed = sum(1 for r in results if isinstance(r, tuple) and r[0] != 200)
    
    return {
        'success': successful,
        'failed': failed,
        'total': len(accounts),
        'already_liked': len(already_liked),
        'fresh_used': len(fresh_accounts[:2000])
    }

def get_player_info(encrypted_uid, server_name, token):
    """جلب معلومات اللاعب للتأكد من عدد الإعجابات قبل وبعد التنفيذ."""
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name == "ME":
        url = "https://client.me.freefiremobile.com/GetPlayerPersonalShow"  # رابط سيرفر الشرق الأوسط
    else:
        url = "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"

    edata = bytes.fromhex(encrypted_uid)
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB55"
    }

    try:
        response = requests.post(url, data=edata, headers=headers, verify=False, timeout=10)
        return decode_protobuf(response.content)
    except Exception:
        return None

# --- نقاط النهاية (Endpoints) ---

@app.route("/", methods=["GET"])
def home():
    return send_from_directory(app.root_path, "index.html")

@app.route('/like', methods=['GET'])
def handle_requests():
    uid = request.args.get("uid")
    server_name = request.args.get("server_name", "").upper()
    key = request.args.get("key")
    client_ip = request.remote_addr

    if key != API_KEY:
        return jsonify({"error": "مفتاح API غير صحيح أو مفقود 🔑"}), 403

    if not uid or not server_name:
        return jsonify({"error": "معرف UID واسم السيرفر مطلوبان"}), 400

    # إضافة ME إلى القائمة
    valid_servers = ["IND", "BR", "US", "SAC", "NA", "BD", "RU", "ME"]
    if server_name not in valid_servers:
        return jsonify({"error": f"سيرفر غير صالح. السيرفرات المتاحة: {valid_servers}"}), 400

    accounts = load_accounts(server_name) or load_accounts("IND")
    if not accounts:
        return jsonify({"error": f"لم يتم العثور على حسابات للسيرفر {server_name}"}), 500

    # التحقق من الحد اليومي للطلب
    today_midnight = get_today_midnight_timestamp()
    count, last_reset = tracker[client_ip]

    if last_reset < today_midnight:
        tracker[client_ip] = [0, time.time()]
        count = 0

    if count >= KEY_LIMIT:
        return jsonify({"error": "تم الوصول للحد اليومي", "remains": f"(0/{KEY_LIMIT})"}), 429

    # جلب توكن للتحقق
    check_token = None
    for account in accounts[:5]:
        check_token = asyncio.run(get_valid_token(account['uid'], account['password']))
        if check_token:
            break
    
    if not check_token:
        return jsonify({"error": "فشل توليد التوكن - لا توجد حسابات صالحة"}), 500
    
    encrypted_uid = enc(uid)

    # جلب معلومات الملف الشخصي قبل الإعجابات
    before = get_player_info(encrypted_uid, server_name, check_token)
    if before is None:
        return jsonify({"error": "UID غير صحيح أو السيرفر غير مطابق", "status": 0}), 200

    try:
        before_data = json.loads(MessageToJson(before))
        before_like = int(before_data['AccountInfo'].get('Likes', 0))
    except Exception:
        return jsonify({"error": "فشل في معالجة البيانات", "status": 0}), 200

    # تحديد رابط الإعجاب حسب السيرفر (إضافة ME)
    if server_name == "IND":
        like_url = "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        like_url = "https://client.us.freefiremobile.com/LikeProfile"
    elif server_name == "ME":
        like_url = "https://client.me.freefiremobile.com/LikeProfile"  # رابط الإعجاب لسيرفر الشرق الأوسط
    else:
        like_url = "https://clientbp.ggpolarbear.com/LikeProfile"

    # تنفيذ إرسال الإعجابات
    asyncio.run(send_all_likes(uid, server_name, like_url))

    # جلب معلومات الملف الشخصي بعد إرسال الإعجابات
    after = get_player_info(encrypted_uid, server_name, check_token)
    if after is None:
        return jsonify({"error": "تعذر التحقق من الإعجابات بعد التنفيذ", "status": 0}), 200

    try:
        after_data = json.loads(MessageToJson(after))
        after_like = int(after_data['AccountInfo']['Likes'])
        player_id = int(after_data['AccountInfo']['UID'])
        player_name = str(after_data['AccountInfo']['PlayerNickname'])
        
        like_given = after_like - before_like
        status = 1 if like_given != 0 else 2
        
        if like_given > 0:
            tracker[client_ip][0] += 1
            count += 1
        
        remains = KEY_LIMIT - count

        return jsonify({
            "LikesGivenByAPI": like_given,
            "LikesafterCommand": after_like,
            "LikesbeforeCommand": before_like,
            "PlayerNickname": player_name,
            "UID": player_id,
            "status": status,
            "remains": f"({remains}/{KEY_LIMIT})",    
        })
    except Exception as e:
        return jsonify({"error": str(e), "status": 0}), 500

@app.route('/reset-cache', methods=['GET'])
def reset_cache():
    """مسح الذاكرة المؤقتة للحسابات التي أرسلت إعجابات."""
    key = request.args.get("key")
    if key != API_KEY:
        return jsonify({"error": "مفتاح غير صحيح"}), 403
    
    liked_cache.clear()
    return jsonify({"message": "تم مسح الذاكرة المؤقتة بنجاح"})

if __name__ == '__main__':
    print("🚀 Server started - Smart Like System!")
    print("📁 Account files:")
    print("   - account_ind.txt (IND server)")
    print("   - account_br.txt (BR/US/SAC/NA servers)")
    print("   - account_me.txt (ME server - Middle East)")
    print("   - account_bd.txt (BD/RU server)")
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)

