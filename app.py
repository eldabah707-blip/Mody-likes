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

# Import Protobuf modules
import like_pb2
import like_count_pb2
import uid_generator_pb2

# --- Configurations & Global Variables ---
API_KEY = "SIAM"
KEY_LIMIT = 999
TOKEN_CACHE = {}
liked_cache = defaultdict(set)
tracker = defaultdict(lambda: [0, time.time()])  # [request_count, last_reset_time]

app = Flask(__name__)
app.config['JSON_AS_ASCII'] = False

# --- Helper Functions ---

def get_today_midnight_timestamp():
    """Get timestamp for today's midnight to reset counter."""
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day)
    return midnight.timestamp()

def load_accounts(server_name):
    """Load accounts (UID:Password) based on the specified server."""
    if server_name == "IND":
        filename = "account_ind.txt"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        filename = "account_br.txt"
    elif server_name == "ME":
        filename = "account_me.txt"
    else:
        filename = "account_bd.txt"

    if not os.path.exists(filename):
        print(f"⚠️ {filename} not found, falling back to account_ind.txt")
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
        print(f"❌ Error loading accounts: {e}")
        return []

def encrypt_message(plaintext):
    """Encrypt payload using AES-CBC with PKCS7 padding."""
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

# --- Token & API Request Handlers ---

async def generate_jwt_token(uid, password):
    """Fetch JWT token from external service."""
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
    """Retrieve cached token or generate a new one."""
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
    """Send like request to game servers."""
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
    """Process single account with concurrency control."""
    async with semaphore:
        token = await get_valid_token(account['uid'], account['password'])
        if not token:
            return 500, account['uid']
        
        status = await send_like(encrypted_uid, token, url)
        if status == 200:
            liked_cache[target_uid].add(account['uid'])
        return status, account['uid']

async def send_all_likes(target_uid, server_name, url):
    """Send likes from available accounts asynchronously."""
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
    """Fetch player information before and after like execution."""
    if server_name == "IND":
        url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
    elif server_name == "ME":
        url = "https://client.me.freefiremobile.com/GetPlayerPersonalShow"
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

# --- API Endpoints ---

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
        return jsonify({"error": "Invalid or missing API key 🔑"}), 403

    if not uid or not server_name:
        return jsonify({"error": "UID and server_name are required"}), 400

    valid_servers = ["IND", "BR", "US", "SAC", "NA", "BD", "RU", "ME"]
    if server_name not in valid_servers:
        return jsonify({"error": f"Invalid server. Available servers: {valid_servers}"}), 400

    accounts = load_accounts(server_name) or load_accounts("IND")
    if not accounts:
        return jsonify({"error": f"No accounts found for server {server_name}"}), 500

    # Check daily limit
    today_midnight = get_today_midnight_timestamp()
    count, last_reset = tracker[client_ip]

    if last_reset < today_midnight:
        tracker[client_ip] = [0, time.time()]
        count = 0

    if count >= KEY_LIMIT:
        return jsonify({"error": "Daily limit reached", "remains": f"(0/{KEY_LIMIT})"}), 429

    # Generate token for validation
    check_token = None
    for account in accounts[:5]:
        check_token = asyncio.run(get_valid_token(account['uid'], account['password']))
        if check_token:
            break
    
    if not check_token:
        return jsonify({"error": "Token generation failed - no valid accounts"}), 500
    
    encrypted_uid = enc(uid)

    # Fetch info before likes
    before = get_player_info(encrypted_uid, server_name, check_token)
    if before is None:
        return jsonify({"error": "Invalid UID or server mismatch", "status": 0}), 200

    try:
        before_data = json.loads(MessageToJson(before))
        before_like = int(before_data['AccountInfo'].get('Likes', 0))
    except Exception:
        return jsonify({"error": "Data parsing failed", "status": 0}), 200

    # Determine like URL by server
    if server_name == "IND":
        like_url = "https://client.ind.freefiremobile.com/LikeProfile"
    elif server_name in {"BR", "US", "SAC", "NA"}:
        like_url = "https://client.us.freefiremobile.com/LikeProfile"
    elif server_name == "ME":
        like_url = "https://client.me.freefiremobile.com/LikeProfile"
    else:
        like_url = "https://clientbp.ggpolarbear.com/LikeProfile"

    # Send likes
    asyncio.run(send_all_likes(uid, server_name, like_url))

    # Fetch info after likes
    after = get_player_info(encrypted_uid, server_name, check_token)
    if after is None:
        return jsonify({"error": "Could not verify likes after command", "status": 0}), 200

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
    """Reset liked accounts cache."""
    key = request.args.get("key")
    if key != API_KEY:
        return jsonify({"error": "Invalid key"}), 403
    
    liked_cache.clear()
    return jsonify({"message": "Cache cleared successfully"})

if __name__ == '__main__':
    print("🚀 Server started - Smart Like System!")
    print("📁 Account files:")
    print("   - account_ind.txt (IND server)")
    print("   - account_br.txt (BR/US/SAC/NA servers)")
    print("   - account_me.txt (ME server - Middle East)")
    print("   - account_bd.txt (BD/RU server)")
    app.run(host='0.0.0.0', port=5001, debug=True, use_reloader=False)

