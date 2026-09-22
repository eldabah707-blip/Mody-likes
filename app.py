from flask import Flask, request, jsonify, send_from_directory
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
from google.protobuf.json_format import MessageToJson
import binascii
import aiohttp
import json
import like_pb2
import like_count_pb2
import uid_generator_pb2
import time
from collections import defaultdict
from datetime import datetime, timedelta
import random
import os
import urllib.parse
import jwt

APP_NAME = "MODY ELBANA LIKE API"
API_KEY = os.getenv("API_KEY", "SIAM")
KEY_LIMIT = int(os.getenv("KEY_LIMIT", "999"))
RELEASE_VERSION = os.getenv("FF_RELEASE_VERSION", "OB54")
JWT_SERVICE_URL = os.getenv(
    "JWT_SERVICE_URL",
    "https://jihad-jwt.lovable.app/api/public/token",
)

app = Flask(__name__)
TOKEN_CACHE = {}
tracker = defaultdict(lambda: [0, time.time()])
liked_cache = defaultdict(set)

# Region-specific client endpoints. BD/ME/RU use the global client cluster.
REGIONS = {
    "IND": {
        "label": "India",
        "client": "https://client.ind.freefiremobile.com",
        "account_file": "account_ind.txt",
    },
    "BR": {
        "label": "Brazil",
        "client": "https://client.us.freefiremobile.com",
        "account_file": "account_br.txt",
    },
    "US": {
        "label": "United States",
        "client": "https://client.us.freefiremobile.com",
        "account_file": "account_us.txt",
    },
    "SAC": {
        "label": "South America",
        "client": "https://client.us.freefiremobile.com",
        "account_file": "account_sac.txt",
    },
    "NA": {
        "label": "North America",
        "client": "https://client.us.freefiremobile.com",
        "account_file": "account_na.txt",
    },
    "ME": {
        "label": "Middle East",
        "client": "https://clientbp.ggpolarbear.com",
        "account_file": "account_me.txt",
    },
    "BD": {
        "label": "Bangladesh",
        "client": "https://clientbp.ggpolarbear.com",
        "account_file": "account_bd.txt",
    },
    "RU": {
        "label": "Russia",
        "client": "https://clientbp.ggpolarbear.com",
        "account_file": "account_ru.txt",
    },
}

# Backward compatibility: if a dedicated file is absent, these are the legacy pools.
LEGACY_ACCOUNT_FILES = {
    "US": "account_br.txt",
    "SAC": "account_br.txt",
    "NA": "account_br.txt",
    "RU": "account_bd.txt",
}


def region_config(server_name):
    return REGIONS.get(server_name.upper())


def get_today_midnight_timestamp():
    now = datetime.now()
    return datetime(now.year, now.month, now.day).timestamp()


def load_accounts(server_name):
    cfg = region_config(server_name)
    if not cfg:
        return []

    candidates = [cfg["account_file"]]
    legacy = LEGACY_ACCOUNT_FILES.get(server_name.upper())
    if legacy and legacy not in candidates:
        candidates.append(legacy)

    filename = next((f for f in candidates if os.path.exists(f)), None)
    if not filename:
        print(f"⚠️ No account file found for {server_name}: {candidates}")
        return []

    accounts = []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or ":" not in line:
                    continue
                uid, password = line.split(":", 1)
                uid, password = uid.strip(), password.strip()
                if uid.isdigit() and password and password.lower() not in {"pass", "password"}:
                    accounts.append({"uid": uid, "password": password})
        print(f"📂 {server_name}: loaded {len(accounts)} accounts from {filename}")
        return accounts
    except Exception as exc:
        print(f"❌ Account load error ({filename}): {exc}")
        return []


async def generate_jwt_token(uid, password):
    try:
        encoded_password = urllib.parse.quote(password, safe="")
        url = f"{JWT_SERVICE_URL}?uid={urllib.parse.quote(uid)}&password={encoded_password}"
        timeout = aiohttp.ClientTimeout(total=24)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status != 200:
                    return None
                data = await response.json(content_type=None)
                if isinstance(data, dict):
                    return data.get("jwt_token") or data.get("token")
    except Exception as exc:
        print(f"JWT error for {uid}: {exc}")
    return None


async def get_valid_token(uid, password):
    cached = TOKEN_CACHE.get(uid)
    if cached:
        remaining = (cached["expires_at"] - datetime.utcnow()).total_seconds()
        if remaining > 1800:
            return cached["token"]

    token = await generate_jwt_token(uid, password)
    if not token:
        TOKEN_CACHE.pop(uid, None)
        return None

    try:
        payload = jwt.decode(token, options={"verify_signature": False})
        exp = payload.get("exp")
        expires_at = datetime.utcfromtimestamp(exp) if exp else datetime.utcnow() + timedelta(hours=12)
    except Exception:
        expires_at = datetime.utcnow() + timedelta(hours=12)

    TOKEN_CACHE[uid] = {"token": token, "expires_at": expires_at}
    return token


def encrypt_message(plaintext):
    key = b"Yg&tc%DEuh6%Zc^8"
    iv = b"6oyZDr22E3ychjM%"
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return binascii.hexlify(cipher.encrypt(pad(plaintext, AES.block_size))).decode("utf-8")


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


def request_headers(token, host=None):
    headers = {
        "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
        "X-GA": "v1 1",
        "ReleaseVersion": RELEASE_VERSION,
        "X-Unity-Version": "2018.4.11f1",
        "Accept": "*/*",
        "Accept-Encoding": "gzip, deflate, br",
        "Accept-Language": "en-GB,en-US;q=0.9,en;q=0.8",
        "Connection": "keep-alive",
    }
    if host:
        headers["Host"] = host
    return headers


async def send_like(encrypted_uid, token, url):
    try:
        edata = bytes.fromhex(encrypted_uid)
        host = urllib.parse.urlparse(url).netloc
        timeout = aiohttp.ClientTimeout(total=15)
        headers = request_headers(token, host)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(2):
                try:
                    async with session.post(url, data=edata, headers=headers) as response:
                        body = (await response.text())[:300]
                        if response.status in (408, 429, 500, 502, 503, 504) and attempt == 0:
                            await asyncio.sleep(0.35)
                            continue
                        return response.status, body
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    if attempt == 0:
                        await asyncio.sleep(0.35)
                        continue
                    return 599, str(exc)[:300]
    except Exception as exc:
        return 599, str(exc)[:300]


def get_player_info(encrypted_uid, server_name, token):
    cfg = region_config(server_name)
    if not cfg:
        return None
    url = f"{cfg['client']}/GetPlayerPersonalShow"
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = request_headers(token, urllib.parse.urlparse(url).netloc)
        import requests
        response = requests.post(url, data=edata, headers=headers, verify=True, timeout=12)
        if response.status_code != 200:
            print(f"GetPlayerPersonalShow {server_name}: HTTP {response.status_code}")
            return None
        return decode_protobuf(response.content)
    except Exception as exc:
        print(f"Player info error ({server_name}): {exc}")
        return None


async def process_account(target_uid, encrypted_uid, account, url, semaphore):
    async with semaphore:
        token = await get_valid_token(account["uid"], account["password"])
        if not token:
            return 599, account["uid"], "token_generation_failed"
        status, detail = await send_like(encrypted_uid, token, url)
        if status == 200:
            liked_cache[target_uid].add(account["uid"])
        return status, account["uid"], detail


async def send_all_likes(target_uid, server_name, like_url):
    encrypted_uid = encrypt_message(create_protobuf_message(target_uid, server_name))
    accounts = load_accounts(server_name)
    if not accounts:
        return {"success": 0, "failed": 0, "total": 0, "already_liked": 0, "fresh_used": 0}

    already_liked = liked_cache.get(target_uid, set())
    fresh_accounts = [a for a in accounts if a["uid"] not in already_liked]
    random.shuffle(fresh_accounts)
    batch = fresh_accounts[:2000]

    semaphore = asyncio.Semaphore(25)
    results = await asyncio.gather(
        *(process_account(target_uid, encrypted_uid, account, like_url, semaphore) for account in batch),
        return_exceptions=True,
    )

    successful = 0
    failed = 0
    status_counts = {}
    samples = []
    for result in results:
        if isinstance(result, tuple) and len(result) == 3:
            status, uid, detail = result
            status_counts[str(status)] = status_counts.get(str(status), 0) + 1
            if status == 200:
                successful += 1
            else:
                failed += 1
                if len(samples) < 5:
                    samples.append({"account": uid, "status": status, "detail": detail})
        else:
            failed += 1

    return {
        "success": successful,
        "failed": failed,
        "total": len(accounts),
        "already_liked": len(already_liked),
        "fresh_used": len(batch),
        "http_status_counts": status_counts,
        "failure_samples": samples,
    }


def extract_player_data(message):
    data = json.loads(MessageToJson(message))
    info = data.get("AccountInfo", {})
    return {
        "likes": int(info.get("Likes", 0)),
        "uid": int(info.get("UID", 0)),
        "nickname": str(info.get("PlayerNickname", "")),
    }


@app.route("/", methods=["GET"])
def home():
    return send_from_directory(app.root_path, "index.html")


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "name": APP_NAME,
        "release_version": RELEASE_VERSION,
        "regions": list(REGIONS.keys()),
    })


@app.route("/servers", methods=["GET"])
def servers():
    result = []
    for code, cfg in REGIONS.items():
        accounts = load_accounts(code)
        result.append({
            "code": code,
            "name": cfg["label"],
            "accounts": len(accounts),
            "ready": len(accounts) > 0,
        })
    return jsonify({"servers": result})


@app.route("/like", methods=["GET"])
def handle_requests():
    uid = (request.args.get("uid") or "").strip()
    server_name = (request.args.get("server_name") or request.args.get("region") or "").upper().strip()
    key = request.args.get("key")
    client_ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()

    if key != API_KEY:
        return jsonify({"status": 0, "error": "Invalid or missing API key"}), 403
    if not uid or not uid.isdigit():
        return jsonify({"status": 0, "error": "UID must contain digits only"}), 400
    if server_name not in REGIONS:
        return jsonify({"status": 0, "error": f"Invalid server. Use: {list(REGIONS)}"}), 400

    accounts = load_accounts(server_name)
    if not accounts:
        return jsonify({
            "status": 0,
            "error": f"No valid accounts configured for {server_name}",
            "server": server_name,
            "hint": f"Add UID:PASSWORD lines to {REGIONS[server_name]['account_file']}",
        }), 503

    today_midnight = get_today_midnight_timestamp()
    count, last_reset = tracker[client_ip]
    if last_reset < today_midnight:
        tracker[client_ip] = [0, time.time()]
        count = 0
    if count >= KEY_LIMIT:
        return jsonify({"status": 0, "error": "Daily limit reached", "remains": f"(0/{KEY_LIMIT})"}), 429

    # Try every configured account until one provides a valid token.
    check_token = None
    checked_accounts = 0
    for account in accounts:
        checked_accounts += 1
        check_token = asyncio.run(get_valid_token(account["uid"], account["password"]))
        if check_token:
            break

    if not check_token:
        return jsonify({
            "status": 0,
            "error": "Token generation failed for all configured accounts",
            "server": server_name,
            "accounts_checked": checked_accounts,
        }), 502

    encrypted_uid = enc(uid)
    before = get_player_info(encrypted_uid, server_name, check_token)
    if before is None:
        return jsonify({"status": 0, "error": "Invalid UID, region, or player-info endpoint"}), 200

    try:
        before_data = extract_player_data(before)
    except Exception:
        return jsonify({"status": 0, "error": "Player data parsing failed"}), 502

    like_url = f"{REGIONS[server_name]['client']}/LikeProfile"
    result = asyncio.run(send_all_likes(uid, server_name, like_url))

    after = get_player_info(encrypted_uid, server_name, check_token)
    if after is None:
        return jsonify({
            "status": 0,
            "error": "Could not verify likes after command",
            "send_result": result,
        }), 200

    try:
        after_data = extract_player_data(after)
        like_given = after_data["likes"] - before_data["likes"]
        status = 1 if like_given > 0 else 2
        if like_given > 0:
            tracker[client_ip][0] += 1
            count += 1
        remains = max(0, KEY_LIMIT - count)
        return jsonify({
            "status": status,
            "server": server_name,
            "UID": after_data["uid"],
            "PlayerNickname": after_data["nickname"],
            "LikesbeforeCommand": before_data["likes"],
            "LikesafterCommand": after_data["likes"],
            "LikesGivenByAPI": like_given,
            "send_result": result,
            "remains": f"({remains}/{KEY_LIMIT})",
        })
    except Exception as exc:
        return jsonify({"status": 0, "error": str(exc), "send_result": result}), 500


@app.route("/reset-cache", methods=["GET"])
def reset_cache():
    if request.args.get("key") != API_KEY:
        return jsonify({"status": 0, "error": "Invalid key"}), 403
    liked_cache.clear()
    return jsonify({"status": 1, "message": "Cache cleared", "owner": "MODY ELBANA"})


if __name__ == "__main__":
    print(f"🚀 {APP_NAME} started")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5001")), debug=False)

