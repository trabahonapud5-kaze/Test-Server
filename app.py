import os
import random
import string
import time
import uuid
import traceback
from flask import Flask, jsonify, request
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor
import requests

# 1. GAWIN MUNA ANG APP DITO
app = Flask(__name__)
CORS(app)

# ======================
# CONFIGURATIONS & CONSTANTS
# ======================
TOKEN_EXPIRY = 300
COOLDOWN = 30
KEY_LIMIT = 30
COOLDOWN_LIMIT = 5

db_cache = {"tokens": {}, "device_limit": {}, "daily_limit": {}}

TELEGRAM_BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")

DB_URL_INJECTOR = os.getenv("DATABASE_URL_INJECTOR") or os.getenv("DATABASE_URL")
DB_URL_SCRIPT = os.getenv("DATABASE_URL_SCRIPT")


def get_db_connection(db_type="injector"):
    if db_type == "script":
        url = DB_URL_SCRIPT
        db_name = "DATABASE_URL_SCRIPT"
    else:
        url = DB_URL_INJECTOR
        db_name = "DATABASE_URL_INJECTOR"

    if not url:
        raise ValueError(f"{db_name} environment variable is missing sa Render!")
    return psycopg2.connect(url)


# 2. INIT_DB FUNCTION
def init_db():
    try:
        conn = get_db_connection("injector")
        cur = conn.cursor()
        cur.execute("""
            ALTER TABLE keys ADD COLUMN IF NOT EXISTS message TEXT DEFAULT NULL;
            
            -- Table para sa pag-uugnay ng device ID sa Telegram username/ID
            CREATE TABLE IF NOT EXISTS device_links (
                device_id TEXT PRIMARY KEY,
                telegram_user TEXT,
                linked_at REAL
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
        print("Database initialized successfully: message and device_links checked/added.")
    except Exception as e:
        print(f"Database init error: {e}")

with app.app_context():
    init_db()


def cleanup():
    now = time.time()
    for t in list(db_cache["tokens"].keys()):
        if now - db_cache["tokens"][t]["time"] > TOKEN_EXPIRY:
            del db_cache["tokens"][t]
    for dev in list(db_cache["device_limit"].keys()):
        if now - db_cache["device_limit"][dev] > KEY_LIMIT:
            del db_cache["device_limit"][dev]
            
    for dev in list(db_cache["daily_limit"].keys()):
        if now - db_cache["daily_limit"][dev]["time"] > COOLDOWN_LIMIT:
            del db_cache["daily_limit"][dev]


def is_vpn_or_proxy(ip: str) -> bool:
    if ip in ["127.0.0.1", "localhost", "::1"]:
        return False
    try:
        response = requests.get(f"http://ip-api.com/json/{ip}?fields=status,hosting", timeout=3)
        data = response.json()
        if data.get("status") == "success":
            if data.get("hosting") == True:
                return True
    except Exception:
        pass
    return False


def send_telegram_alert(message: str):
    if not TELEGRAM_BOT_TOKEN or not OWNER_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": OWNER_ID,
        "text": message,
        "parse_mode": "Markdown",
    }
    try:
        requests.post(url, data=payload, timeout=5)
    except Exception:
        pass


def format_remaining(expiry_timestamp):
    if expiry_timestamp == 0 or expiry_timestamp > 32503680000: # Lifetime
        return "Lifetime"
    
    diff = expiry_timestamp - time.time()
    if diff <= 0:
        return "Expired"
        
    days = int(diff // 86400)
    hours = int((diff % 86400) // 3600)
    minutes = int((diff % 3600) // 60)
    
    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0 or days > 0:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    
    return " ".join(parts)


def convert_duration(duration: str) -> int:
    if not duration:
        return 10800
    duration = str(duration).lower().strip()
    try:
        if duration.endswith("m"):
            return int(duration[:-1]) * 60
        if duration.endswith("h"):
            return int(duration[:-1]) * 3600
        if duration.endswith("d"):
            return int(duration[:-1]) * 86400
        if duration == "lifetime":
            return 999999999
        return int(duration)
    except ValueError:
        return 10800


@app.route("/")
def home():
    return "KAZE SERVER ONLINE"


@app.route("/token")
def token():
    cleanup()
    ip = request.remote_addr
    now = time.time()
    device_id = request.args.get("device_id")

    if is_vpn_or_proxy(ip):
        return jsonify({
            "status": "error",
            "type": "vpn",
            "message": "VPN detected please turn off your vpn"
        }), 403

    if not device_id:
        return jsonify({"status": "error", "message": "Missing device ID"}), 400

    if device_id in db_cache["daily_limit"]:
        elapsed = now - db_cache["daily_limit"][device_id]["time"]
        remaining_sec = int(COOLDOWN_LIMIT - elapsed)
        return jsonify({
            "status": "limit",
            "type": "limit",
            "remaining_seconds": remaining_sec,
            "message": "Your free key has ended please try again tomorrow"
        }), 403

    token_id = str(uuid.uuid4())
    db_cache["tokens"][token_id] = {"device_id": device_id, "time": now}
    return jsonify({"status": "success", "token": token_id})


def handle_getkey(db_type):
    source = request.args.get("src", "bot")
    duration = request.args.get("duration", "1h")
    max_dev = request.args.get("max", "1")
    device_id = request.args.get("device_id", "unknown_device")
    now = time.time()

    ip = request.remote_addr

    if is_vpn_or_proxy(ip):
        return jsonify({"status": "error", "type": "vpn", "message": "VPN detected please turn off your vpn"}), 403

    if device_id in db_cache["daily_limit"]:
        elapsed = now - db_cache["daily_limit"][device_id]["time"]
        remaining_sec = int(COOLDOWN_LIMIT - elapsed)
        if remaining_sec > 0:
            return jsonify({
                "status": "limit",
                "type": "limit",
                "remaining_seconds": remaining_sec,
                "message": "Your free key has ended please try again tomorrow"
            }), 403

    if duration.lower() == 'lifetime':
        formatted_dur = "Lifetime"
    else:
        formatted_dur = duration.lower()

    if source == "bot":
        prefix = f"Kaze-{formatted_dur}"
    else:
        prefix = f"KazeFreeKey-{formatted_dur}-"

    key = prefix + "".join(random.choices(string.ascii_letters + string.digits, k=12))
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection(db_type) 
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO keys (key_code, expiry, device, revoked, login_time, max_devices)
            VALUES (%s, %s, NULL, FALSE, NULL, %s);
            """,
            (key, now + expiry_seconds, int(max_dev)),
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Database error: {str(e)}"}), 500

    db_cache["daily_limit"][device_id] = {"time": now}

    return jsonify({
        "status": "success",
        "key": key,
        "expires_in": expiry_seconds,
        "max_devices": max_dev,
    })


def handle_customkey(db_type):
    custom_name = request.args.get("name")
    duration = request.args.get("duration", "3h")
    max_dev = request.args.get("max", "1")
    now = time.time()

    if not custom_name:
        return jsonify({"status": "error", "message": "Custom key name is missing"}), 400

    key = custom_name.strip().replace(" ", "-")
    expiry_seconds = convert_duration(duration)

    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("SELECT key_code FROM keys WHERE key_code = %s;", (key,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"status": "error", "message": "Key name already exists!"}), 409

        cur.execute(
            """
            INSERT INTO keys (key_code, expiry, device, revoked, login_time, max_devices)
            VALUES (%s, %s, NULL, FALSE, NULL, %s);
            """,
            (key, now + expiry_seconds, int(max_dev)),
        )
        conn.commit()
        cur.close()
        conn.close()

        tag = "[SCRIPT]" if db_type == "script" else "[INJECTOR]"
        send_telegram_alert(
            f"🎁 *{tag} Custom Key Created*\n"
            f"Key: `{key}`\n"
            f"Duration: `{duration}`\n"
            f"Max Devices: `{max_dev}`"
        )
        return jsonify({
            "status": "success",
            "key": key,
            "expires_in": expiry_seconds,
            "max_devices": max_dev,
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

def handle_verify(db_type):
    try:
        cleanup()
        key = request.args.get("key")
        device = request.args.get("device")
        if not key or not device:
            return jsonify({"status": "invalid", "message": "Missing key or device"}), 400

        conn = get_db_connection(db_type)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        tag = "[SCRIPT]" if db_type == "script" else "[INJECTOR]"

        # 1. UNAHIN MURING ICECK KUNG TOOTOO/EXISTS ANG KEY SA DATABASE
        cur.execute("SELECT * FROM keys WHERE key_code = %s;", (key,))
        data = cur.fetchone()

        if not data:
            cur.close()
            conn.close()
            # Kapag mali o peke ang key, INVALID agad ang isasagot!
            return jsonify({"status": "invalid"})

        # 2. SUNOD, TSAKA PA LANG ICECK KUNG NAKA-LINK NA ANG DEVICE SA TELEGRAM
        cur.execute("SELECT telegram_user FROM device_links WHERE device_id = %s;", (device,))
        link_data = cur.fetchone()
        telegram_user = link_data["telegram_user"] if link_data else None

        if not telegram_user:
            cur.close()
            conn.close()
            bot_username = "CodmInjCheckingbot"
            bot_link = f"https://t.me/{bot_username}?start={device}"
            return jsonify({
                "status": "link_required",
                "message": "Please start the Telegram bot first!",
                "bot_url": bot_link
            })

        # 3. MGA SUSUNOD NA CHECKS (Custom message, revoked, expired, max devices, etc.)
        raw_message = data.get("message")
        custom_message = str(raw_message).strip() if raw_message else ""

        if custom_message != "":
            cur.close()
            conn.close()
            send_telegram_alert(f"🚫 *{tag} Custom Message Triggered*\nKey: `{key}`\nUser Login: `@{telegram_user}`\nMessage: `{custom_message}`")
            return jsonify({
                "status": "custom",
                "message": custom_message
            })

        if data["revoked"]:
            cur.close()
            conn.close()
            send_telegram_alert(f"❌ *{tag} Key Revoked Attempt*\nKey: `{key}`\nUser Login: `@{telegram_user}`\nDevice: `{device}`")
            return jsonify({"status": "revoked"})

        now = time.time()
        if now > data["expiry"]:
            cur.close()
            conn.close()
            send_telegram_alert(f"❌ *{tag} Key Expired Attempt*\nKey: `{key}`\nUser Login: `@{telegram_user}`\nDevice: `{device}`")
            return jsonify({"status": "expired"})

        current_devices = data["device"].split(",") if data["device"] else []
        max_allowed = data.get("max_devices", 1)
        remaining_seconds = int(data["expiry"] - now)
        time_left_str = format_remaining(data["expiry"])

        tg_display = f"\nUser Login: @{telegram_user}"

        def success_response():
            return jsonify({
                "status": "valid",
                "expires_in_sec": remaining_seconds,
                "expire_str": time_left_str,
                "message": custom_message,
                "telegram_user": telegram_user
            })

        if device in current_devices:
            cur.close()
            conn.close()
            device_index = current_devices.index(device) + 1
            counter_str = f" ({device_index}/{max_allowed})" if max_allowed > 1 else ""
            send_telegram_alert(
                f"✓ *{tag} Key Used{counter_str}*\n"
                f"Key: `{key}`\n"
                f"Device: `{device}`\n"
                f"Expires in: `{time_left_str}`"
                f"{tg_display}"
            )
            return success_response()

        if len(current_devices) < max_allowed:
            current_devices.append(device)
            new_device_string = ",".join(current_devices)

            cur.execute(
                "UPDATE keys SET device = %s, login_time = %s WHERE key_code = %s;",
                (new_device_string, now, key),
            )
            conn.commit()
            cur.close()
            conn.close()

            counter_str = (
                f" ({len(current_devices)}/{max_allowed})" if max_allowed > 1 else ""
            )
            send_telegram_alert(
                f"✓ *{tag} Key Used{counter_str}*\n"
                f"Key: `{key}`\n"
                f"Device: `{device}`\n"
                f"Expires in: `{time_left_str}`"
                f"{tg_display}"
            )
            return success_response()

        cur.close()
        conn.close()
        send_telegram_alert(
            f"🔒 *{tag} Max Device Limit Reached*\n"
            f"Key: `{key}`\n"
            f"User Login: `@{telegram_user}`\n"
            f"Attempt Device: `{device}`\n"
            f"Slots: `{len(current_devices)}/{max_allowed}`"
        )
        return jsonify({"status": "locked"})

    except Exception as e:
        print("-----------------------------------------")
        print("CRASH ERROR SA /verify:")
        traceback.print_exc()
        print("-----------------------------------------")
        return jsonify({
            "status": "error",
            "message": f"Server Exception: {str(e)}"
        }), 500
        
def handle_unrevoke(db_type):
    key = request.args.get("key")
    if not key:
        return jsonify({"status": "error", "message": "Missing key"}), 400
    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("UPDATE keys SET revoked = FALSE WHERE key_code = %s;", (key,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def handle_reset(db_type):
    key = request.args.get("key")
    if not key:
        return jsonify({"status": "error"}), 400
    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("UPDATE keys SET device = NULL, login_time = NULL WHERE key_code = %s;", (key,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def handle_list(db_type):
    try:
        status_filter = request.args.get("status", "active")
        now = time.time()
        conn = get_db_connection(db_type)
        cur = conn.cursor(cursor_factory=RealDictCursor)

        if status_filter == "revoked":
            cur.execute("SELECT key_code, device, expiry, max_devices FROM keys WHERE revoked = TRUE ORDER BY expiry DESC;")
        else:
            cur.execute("SELECT key_code, device, expiry, max_devices FROM keys WHERE revoked = FALSE AND expiry > %s ORDER BY expiry ASC;", (now,))

        rows = cur.fetchall()
        cur.close()
        conn.close()

        result = []
        for r in rows:
            result.append({
                "key": r.get("key_code") or "UNKNOWN",
                "device": r.get("device"),
                "max_devices": r.get("max_devices") or 1,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def handle_delete(db_type):
    key = request.args.get("key")
    if not key:
        return jsonify({"status": "error", "message": "Missing key"}), 400
    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("DELETE FROM keys WHERE key_code = %s;", (key,))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def handle_stats(db_type):
    try:
        now = time.time()
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM keys;")
        total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM keys WHERE revoked = FALSE AND expiry > %s;", (now,))
        active = cur.fetchone()[0]
        cur.close()
        conn.close()
        return jsonify({"total_keys": total, "active_keys": active, "expired_keys": total - active})
    except Exception:
        return jsonify({"total_keys": 0, "active_keys": 0, "expired_keys": 0})


def handle_extend(db_type):
    key = request.args.get("key")
    duration = request.args.get("duration", "1d")
    
    if not key:
        return jsonify({"status": "error", "message": "Missing key"}), 400
    
    extension_seconds = convert_duration(duration)
    now = time.time()
    
    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT expiry FROM keys WHERE key_code = %s;", (key,))
        data = cur.fetchone()
        
        if not data:
            cur.close()
            conn.close()
            return jsonify({"status": "error", "message": "Key not found!"}), 404
            
        current_expiry = data["expiry"]
        base_time = now if current_expiry < now else current_expiry
        new_expiry = base_time + extension_seconds
        
        cur.execute("UPDATE keys SET expiry = %s WHERE key_code = %s;", (new_expiry, key))
        conn.commit()
        cur.close()
        conn.close()
        
        readable_time = format_remaining(new_expiry)
        return jsonify({
            "status": "success",
            "key": key,
            "new_expiry": new_expiry,
            "remaining_time": readable_time,
            "added_duration": duration
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ======================
# ROUTES REGISTRATION
# ======================
@app.route("/getkey")
def getkey_injector(): return handle_getkey("injector")
@app.route("/customkey")
def custom_key_injector(): return handle_customkey("injector")
@app.route("/verify")
def verify_injector(): return handle_verify("injector")
@app.route("/revoke")
def revoke_injector(): return jsonify({"status": "success"})
@app.route("/unrevoke")
def unrevoke_injector(): return handle_unrevoke("injector")
@app.route("/reset")
def reset_injector(): return handle_reset("injector")
@app.route("/list")
def list_injector(): return handle_list("injector")
@app.route("/delete")
def delete_injector(): return handle_delete("injector")
@app.route("/stats")
def stats_injector(): return handle_stats("injector")
@app.route("/extend")
def extend_injector(): return handle_extend("injector")

from flask import jsonify, request

@app.route("/unregister_bot_user", methods=["GET"])
def unregister_bot_user():
    identifier = request.args.get("identifier", "").strip()
    # Tanggalin ang '@' kung sinama ng user sa pag-type
    if identifier.startswith("@"):
        identifier = identifier[1:]
        
    try:
        conn = get_db_connection('injector') # O kung aling db man
        cur = conn.cursor()
        
        # Burahin sa database base sa telegram_user (o kaya ay device_id kung un ang pinasa)
        cur.execute("DELETE FROM device_links WHERE telegram_user ILIKE %s;", (identifier,))
        conn.commit()
        
        deleted_rows = cur.rowcount
        cur.close()
        conn.close()
        
        if deleted_rows > 0:
            return {"status": "success", "message": "User unlinked successfully."}
        else:
            return {"status": "error", "message": "User/Device not found in database."}, 404
            
    except Exception as e:
        return {"status": "error", "message": str(e)}, 500
    
@app.route("/script/getkey")
def getkey_script(): return handle_getkey("script")
@app.route("/script/customkey")
def custom_key_script(): return handle_customkey("script")
@app.route("/script/verify")
def verify_script(): return handle_verify("script")
@app.route("/script/revoke")
def revoke_script(): return jsonify({"status": "success"})
@app.route("/script/unrevoke")
def unrevoke_script(): return handle_unrevoke("script")
@app.route("/script/reset")
def reset_script(): return handle_reset("script")
@app.route("/script/list")
def list_script(): return handle_list("script")
@app.route("/script/delete")
def delete_script(): return handle_delete("script")
@app.route("/script/stats")
def stats_script(): return handle_stats("script")
@app.route("/script/extend")
def extend_script(): return handle_extend("script")

@app.route('/setmessage')
def set_message():
    key = request.args.get('key')
    msg = request.args.get('msg')
    db_type = request.args.get('db_type', 'injector')
    
    if not key or not msg:
        return jsonify({"status": "error", "message": "Missing key or message"}), 400
        
    try:
        conn = get_db_connection(db_type)
        cur = conn.cursor()
        cur.execute("SELECT key_code FROM keys WHERE key_code = %s;", (key,))
        if not cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"status": "error", "message": "Key does not exist!"}), 404
            
        cur.execute("UPDATE keys SET message = %s WHERE key_code = %s;", (msg, key))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success", "message": "Custom message updated successfully!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route("/admin/clear_devices", methods=["GET"])
def clear_devices():
    try:
        conn = get_db_connection('injector')
        cur = conn.cursor()
        cur.execute("DELETE FROM device_links;")
        conn.commit()
        cur.close()
        conn.close()
        return "SUCCESS: Lahat ng device links ay nabura na!"
    except Exception as e:
        return f"Error: {e}", 500
        
@app.route('/telegram_webhook', methods=['POST'])
def telegram_bot():
    data = request.json
    if not data:
        return "OK", 200

    if "message" in data:
        msg = data["message"]
        msg_text = msg.get("text", "")
        chat_id = msg["chat"]["id"]
        user_info = msg.get("from", {})
        
        username = user_info.get("username")
        if not username:
            first_name = user_info.get("first_name", "User")
            user_id = user_info.get("id")
            username = f"{first_name}_{user_id}"

        if msg_text.startswith("/start"):
            parts = msg_text.split(" ")
            if len(parts) > 1:
                device_id = parts[1].strip()
                db_type = 'injector'
                
                try:
                    conn = get_db_connection(db_type)
                    cur = conn.cursor()
                    cur.execute("""
                        INSERT INTO device_links (device_id, telegram_user, linked_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (device_id) 
                        DO UPDATE SET telegram_user = EXCLUDED.telegram_user, linked_at = EXCLUDED.linked_at;
                    """, (device_id, username, time.time()))
                    conn.commit()
                    cur.close()
                    conn.close()
                except Exception as e:
                    print(f"Link error: {e}")
            
            reply_text = "Success!"
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": reply_text,
            }
            try:
                requests.post(url, json=payload, timeout=5)
            except Exception:
                pass

        elif msg_text.startswith("/unblockmess"):
            parts = msg_text.split(" ")
            if len(parts) > 1:
                target_key = parts[1].strip()
                db_type = request.args.get('db_type', 'injector')
                
                try:
                    conn = get_db_connection(db_type)
                    cur = conn.cursor()
                    cur.execute("UPDATE keys SET message = NULL WHERE key_code = %s;", (target_key,))
                    conn.commit()
                    
                    if cur.rowcount > 0:
                        reply_text = f"✅ *Successfully unblocked/cleared custom message for key:*\n`{target_key}`\n\nGagana na ulit ito bilang regular valid key!"
                    else:
                        reply_text = f"❌ *Key not found in database:* `{target_key}`"
                    
                    cur.close()
                    conn.close()
                except Exception as e:
                    reply_text = f"❌ *Database Error:* {str(e)}"
                
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": reply_text,
                    "parse_mode": "Markdown",
                }
                try:
                    requests.post(url, data=payload, timeout=5)
                except Exception:
                    pass
            else:
                url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
                payload = {
                    "chat_id": chat_id,
                    "text": "⚠️ *Usage:* `/unblockmess <iyong_key>`",
                    "parse_mode": "Markdown",
                }
                try:
                    requests.post(url, data=payload, timeout=5)
                except Exception:
                    pass
                    
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
