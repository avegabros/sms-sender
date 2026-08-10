import time
import logging
import serial
import os
import threading
from fastapi import FastAPI, HTTPException, Security, Depends, status, Request
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("sms-sender")

tags_metadata = [
    {
        "name": "SMS Operations",
        "description": "Send SMS messages using the cellular transceiver.",
    },
    {
        "name": "Admin Key Management",
        "description": "Generate, list, and revoke API keys for client application access. Requires Admin Key.",
    },
    {
        "name": "System",
        "description": "System health and status endpoints.",
    },
]

from fastapi.security import APIKeyHeader, HTTPBasic, HTTPBasicCredentials

app = FastAPI(
    title="SMS Sender API",
    description="Raspberry Pi 5 + SIM800L cellular gateway for sending and receiving SMS via HTTP REST",
    version="1.0.0",
    openapi_tags=tags_metadata,
    docs_url=None,
    redoc_url=None,
    openapi_url=None
)

import json
import secrets

# Security & Credentials configuration
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False, description="API Key for clients or Master Admin Key")
raw_api_key = os.getenv("SMS_SENDER_API_KEY")
SMS_SENDER_API_KEY = raw_api_key.strip().strip('"').strip("'") if raw_api_key else None

raw_user = os.getenv("DASHBOARD_USERNAME")
DASHBOARD_USERNAME = raw_user.strip().strip('"').strip("'") if (raw_user and raw_user.strip()) else "admin"

raw_pass = os.getenv("DASHBOARD_PASSWORD")
DASHBOARD_PASSWORD = raw_pass.strip().strip('"').strip("'") if (raw_pass and raw_pass.strip()) else None

security_basic = HTTPBasic(auto_error=False)

if DASHBOARD_PASSWORD:
    logger.info(f"Dashboard HTTP Basic Auth enabled (Username: '{DASHBOARD_USERNAME}')")

def verify_dashboard_auth(credentials: HTTPBasicCredentials = Depends(security_basic)):
    if DASHBOARD_PASSWORD:
        if not credentials:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Dashboard authentication required",
                headers={"WWW-Authenticate": 'Basic realm="SMS Sender Gateway"'},
            )
        user_input = credentials.username.strip() if credentials.username else ""
        pass_input = credentials.password.strip() if credentials.password else ""
        
        is_user_correct = secrets.compare_digest(user_input, DASHBOARD_USERNAME)
        is_pass_correct = secrets.compare_digest(pass_input, DASHBOARD_PASSWORD)
        if not (is_user_correct and is_pass_correct):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Incorrect username or password",
                headers={"WWW-Authenticate": 'Basic realm="SMS Sender Gateway"'},
            )
    return credentials

import sqlite3
import datetime

DB_FILE = "data/sms_sender.db"
db_lock = threading.Lock()
RESET_GPIO_PIN = int(os.getenv("RESET_GPIO_PIN", "17"))

def init_db():
    if not os.path.exists("data"):
        os.makedirs("data", exist_ok=True)
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                phone_number TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL,
                raw_response TEXT,
                app_name TEXT
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                app_name TEXT PRIMARY KEY,
                api_key TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL
            )
        """)
        
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS inbox (
                id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                sender TEXT NOT NULL,
                message TEXT NOT NULL,
                read_status INTEGER DEFAULT 0,
                webhook_status TEXT DEFAULT 'none'
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_timestamp ON history(timestamp DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inbox_timestamp ON inbox(timestamp DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inbox_read_status ON inbox(read_status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_inbox_sender ON inbox(sender)")

        # Seed default gateway settings if missing

        default_settings = {
            "webhook_url": os.getenv("WEBHOOK_URL", ""),
            "webhook_secret": "",
            "blocked_numbers": "",
            "auto_delete_senders": "GLOBE,SMART,TELCO_PROMO,2256,8080",
            "auto_delete_keywords": "PROMO,LOAN,FREE,CONGRATS",
            "retention_days": "30"
        }
        for k_name, v_val in default_settings.items():
            cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k_name, v_val))

        conn.commit()



        # Migrate legacy json files if present
        KEYS_FILE = "data/api_keys.json"
        HISTORY_FILE = "data/sms_history.json"
        
        if os.path.exists(KEYS_FILE):
            try:
                with open(KEYS_FILE, "r") as f:
                    legacy_keys = json.load(f)
                    if isinstance(legacy_keys, dict):
                        now_str = datetime.datetime.now().isoformat()
                        for app_n, k_val in legacy_keys.items():
                            cursor.execute(
                                "INSERT OR IGNORE INTO api_keys (app_name, api_key, created_at) VALUES (?, ?, ?)",
                                (app_n, k_val, now_str)
                            )
                conn.commit()
                os.rename(KEYS_FILE, f"{KEYS_FILE}.migrated")
                logger.info("Successfully migrated legacy api_keys.json to SQLite database")
            except Exception as e:
                logger.error(f"Error migrating legacy api_keys.json: {e}")

        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r") as f:
                    legacy_history = json.load(f)
                    if isinstance(legacy_history, list):
                        for rec in legacy_history:
                            cursor.execute(
                                """
                                INSERT OR IGNORE INTO history (id, timestamp, phone_number, message, status, raw_response, app_name)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    rec.get("id"),
                                    rec.get("timestamp"),
                                    rec.get("phone_number"),
                                    rec.get("message"),
                                    rec.get("status"),
                                    rec.get("raw_response"),
                                    rec.get("app_name", "Dashboard")
                                )
                            )
                conn.commit()
                os.rename(HISTORY_FILE, f"{HISTORY_FILE}.migrated")
                logger.info("Successfully migrated legacy sms_history.json to SQLite database")
            except Exception as e:
                logger.error(f"Error migrating legacy sms_history.json: {e}")

        conn.close()

# Initialize DB on module startup
init_db()

def get_history_stats():
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    COUNT(*) as total,
                    COALESCE(SUM(CASE WHEN status = 'success' THEN 1 ELSE 0 END), 0) as success,
                    COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0) as failed
                FROM history
            """)
            row = cursor.fetchone()
            conn.close()
            if row:
                return {"total": row[0], "success": row[1], "failed": row[2]}
        except Exception as e:
            logger.error(f"Error reading stats from DB: {e}")
    return {"total": 0, "success": 0, "failed": 0}

def load_history_paginated(page=1, limit=50, search=None, export_all=False):
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            where_clause = ""
            params = []
            if search and search.strip():
                where_clause = "WHERE phone_number LIKE ? OR message LIKE ? OR app_name LIKE ?"
                pattern = f"%{search.strip()}%"
                params = [pattern, pattern, pattern]
                
            count_sql = f"SELECT COUNT(*) FROM history {where_clause}"
            cursor.execute(count_sql, params)
            total_records = cursor.fetchone()[0]

            if export_all:
                sql = f"SELECT id, timestamp, phone_number, message, status, raw_response, app_name FROM history {where_clause} ORDER BY timestamp DESC LIMIT 50000"
                cursor.execute(sql, params)
            else:
                offset = (page - 1) * limit
                sql = f"SELECT id, timestamp, phone_number, message, status, raw_response, app_name FROM history {where_clause} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
                cursor.execute(sql, params + [limit, offset])
                
            rows = cursor.fetchall()
            conn.close()
            
            total_pages = (total_records + limit - 1) // limit if limit > 0 else 1
            
            return {
                "records": [dict(row) for row in rows],
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total_records": total_records,
                    "total_pages": max(1, total_pages)
                }
            }
        except Exception as e:
            logger.error(f"Error reading paginated history from DB: {e}")
            return {"records": [], "pagination": {"page": page, "limit": limit, "total_records": 0, "total_pages": 1}}

import re
import urllib.request
import urllib.error

def decode_ucs2_text(text):
    if not text:
        return text
    clean_text = text.replace("\n", "").replace("\r", "").strip()
    if len(clean_text) >= 4 and len(clean_text) % 4 == 0 and all(c in "0123456789ABCDEFabcdef" for c in clean_text):
        try:
            return bytes.fromhex(clean_text).decode("utf-16-be")
        except Exception:
            pass
    return text

def parse_cmgl_response(raw_text):
    messages = []
    if not raw_text or "+CMGL:" not in raw_text:
        return messages

    # Flexible CMGL regex matching index, status, sender, and timestamp across various SIM800L firmware formats
    header_pattern = re.compile(r'\+CMGL:\s*(\d+),\s*"([^"]+)",\s*"([^"]+)"')
    lines = raw_text.splitlines()
    
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("+CMGL:"):
            match = header_pattern.search(line)
            if match:
                idx = int(match.group(1))
                status_str = match.group(2)
                sender = match.group(3)
                
                # Extract timestamp (last quoted string in CMGL line)
                quoted_strings = re.findall(r'"([^"]*)"', line)
                time_str = quoted_strings[-1] if len(quoted_strings) >= 3 else ""
                
                body_lines = []
                i += 1
                while i < len(lines):
                    next_line = lines[i]
                    if next_line.startswith("+CMGL:") or next_line.strip() in ("OK", "ERROR"):
                        i -= 1
                        break
                    body_lines.append(next_line)
                    i += 1
                    
                body = "\n".join(body_lines).strip()
                body = decode_ucs2_text(body)
                
                iso_ts = datetime.datetime.now().isoformat()
                if time_str and "," in time_str:
                    try:
                        ts_parts = time_str.split(",")
                        date_p = ts_parts[0].split("/")
                        time_p = ts_parts[1].split("+")[0].split("-")[0]
                        iso_ts = f"20{date_p[0]}-{date_p[1]}-{date_p[2]}T{time_p}"
                    except Exception:
                        pass
                    
                messages.append({
                    "index": idx,
                    "status_str": status_str,
                    "sender": sender,
                    "timestamp": iso_ts,
                    "message": body
                })
        i += 1

    return messages

def get_all_settings():
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM settings")
            rows = cursor.fetchall()
            conn.close()
            return {row["key"]: row["value"] for row in rows}
        except Exception as e:
            logger.error(f"Error reading settings from DB: {e}")
            return {}

def get_setting(key_name, default=""):
    settings = get_all_settings()
    return settings.get(key_name, default)

def update_settings_bulk(settings_dict):
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            for k, v in settings_dict.items():
                cursor.execute(
                    "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (str(k), str(v))
                )
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error updating settings in DB: {e}")
            return False

def is_number_blocked(phone_number):
    if not phone_number:
        return False
    blocked_str = get_setting("blocked_numbers", "")
    if not blocked_str:
        return False
    
    clean_phone = phone_number.replace("+", "").replace("-", "").replace(" ", "").lower()
    blocked_items = [b.strip().replace("+", "").replace("-", "").replace(" ", "").lower() for b in blocked_str.split(",") if b.strip()]
    
    for item in blocked_items:
        if item and item in clean_phone:
            return True
    return False

def should_auto_delete_inbox(sender, message):
    senders_str = get_setting("auto_delete_senders", "")
    if senders_str:
        sender_clean = sender.replace("+", "").replace("-", "").replace(" ", "").lower()
        blocked_senders = [s.strip().replace("+", "").replace("-", "").replace(" ", "").lower() for s in senders_str.split(",") if s.strip()]
        for s in blocked_senders:
            if s and s in sender_clean:
                return True, f"Matched auto-delete sender filter '{s}'"

    keywords_str = get_setting("auto_delete_keywords", "")
    if keywords_str and message:
        msg_clean = message.lower()
        keywords = [k.strip().lower() for k in keywords_str.split(",") if k.strip()]
        for kw in keywords:
            if kw and kw in msg_clean:
                return True, f"Matched auto-delete keyword filter '{kw}'"

    return False, ""

def dispatch_webhook(payload):
    webhook_url_str = get_setting("webhook_url", os.getenv("WEBHOOK_URL", ""))
    webhook_secret = get_setting("webhook_secret", "")
    
    if not webhook_url_str or not webhook_url_str.strip():
        return "none"
        
    urls = [u.strip() for u in webhook_url_str.split(",") if u.strip()]
    if not urls:
        return "none"

    statuses = []
    for webhook_url in urls:
        try:
            req_headers = {
                "Content-Type": "application/json",
                "User-Agent": "sms-sender-gateway/1.0"
            }
            if webhook_secret and webhook_secret.strip():
                req_headers["X-Webhook-Secret"] = webhook_secret.strip()

            req_data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                webhook_url,
                data=req_data,
                headers=req_headers,
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                logger.info(f"Successfully posted webhook payload to {webhook_url} (HTTP {resp.status})")
                statuses.append("delivered")
        except Exception as e:
            logger.error(f"Failed to post webhook to {webhook_url}: {e}")
            statuses.append("failed")

    return ", ".join(statuses)


def dispatch_webhook_async(msg_id, payload):
    def _worker():
        status = dispatch_webhook(payload)
        init_db()
        with db_lock:
            try:
                conn = sqlite3.connect(DB_FILE)
                cursor = conn.cursor()
                cursor.execute("UPDATE inbox SET webhook_status = ? WHERE id = ?", (status, msg_id))
                conn.commit()
                conn.close()
            except Exception as e:
                logger.error(f"Error updating webhook status for {msg_id}: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def save_inbox_message(msg):
    should_filter, reason = should_auto_delete_inbox(msg["sender"], msg["message"])
    if should_filter:
        logger.info(f"[SPAM FILTER] Dropped/auto-deleted incoming SMS from '{msg['sender']}': {reason}")
        return

    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            
            # Prevent duplicate message entry
            cursor.execute(
                "SELECT COUNT(*) FROM inbox WHERE sender = ? AND timestamp = ? AND message = ?",
                (msg["sender"], msg["timestamp"], msg["message"])
            )
            if cursor.fetchone()[0] > 0:
                conn.close()
                return

            msg_id = f"inbox_{int(time.time())}_{secrets.token_hex(4)}"
            cursor.execute(
                """
                INSERT INTO inbox (id, timestamp, sender, message, read_status, webhook_status)
                VALUES (?, ?, ?, ?, 0, 'pending')
                """,
                (
                    msg_id,
                    msg["timestamp"],
                    msg["sender"],
                    msg["message"]
                )
            )
            conn.commit()
            conn.close()
            logger.info(f"Saved incoming SMS from {msg['sender']} to SQLite inbox database")
            
            # Asynchronously dispatch webhook outside db_lock and serial_lock
            dispatch_webhook_async(msg_id, {
                "event": "sms_received",
                "id": msg_id,
                "sender": msg["sender"],
                "message": msg["message"],
                "timestamp": msg["timestamp"]
            })
        except Exception as e:
            logger.error(f"Error saving incoming SMS to inbox: {e}")


def poll_inbox_messages():
    # Non-blocking lock acquire so poller never blocks HTTP requests or freezes Uvicorn threadpool
    if not serial_lock.acquire(blocking=False):
        logger.debug("Inbox poll cycle skipped: serial line in use by API")
        return
    try:
        ser = None
        try:
            ser = get_serial_device(timeout=4, fast_init=True)
            send_at_command(ser, "AT+CMGF=1", timeout=2)
            send_at_command(ser, 'AT+CPMS="SM","SM","SM"', timeout=2)
            send_at_command(ser, 'AT+CNMI=2,1,0,0,0', timeout=2)
            raw_res = query_at_command(ser, 'AT+CMGL="ALL"', timeout=8)
            if raw_res and "+CMGL:" in raw_res:
                logger.info(f"[INBOX POLL] Discovered SMS raw response: {repr(raw_res)}")
                parsed_messages = parse_cmgl_response(raw_res)
                if parsed_messages:
                    logger.info(f"Discovered {len(parsed_messages)} incoming SMS message(s) on SIM800L")
                    for msg in parsed_messages:
                        save_inbox_message(msg)
            # Always purge SIM card memory so SIM card capacity remains 0/40 and never blocks incoming carrier SMS
            send_at_command(ser, "AT+CMGD=1,4", timeout=4)
        finally:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
    except Exception as e:
        logger.debug(f"Inbox poll cycle error: {e}")
    finally:
        serial_lock.release()

def load_inbox_paginated(page=1, limit=50, search=None):
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            where_clause = ""
            params = []
            if search and search.strip():
                where_clause = "WHERE sender LIKE ? OR message LIKE ?"
                pattern = f"%{search.strip()}%"
                params = [pattern, pattern]
                
            cursor.execute(f"SELECT COUNT(*) FROM inbox {where_clause}", params)
            total_records = cursor.fetchone()[0]

            cursor.execute("SELECT COALESCE(SUM(CASE WHEN read_status = 0 THEN 1 ELSE 0 END), 0) FROM inbox")
            unread_records = cursor.fetchone()[0]

            offset = (page - 1) * limit
            cursor.execute(
                f"SELECT id, timestamp, sender, message, read_status, webhook_status FROM inbox {where_clause} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            )
            rows = cursor.fetchall()
            conn.close()
            
            total_pages = (total_records + limit - 1) // limit if limit > 0 else 1
            
            return {
                "stats": {"total": total_records, "unread": unread_records},
                "records": [dict(row) for row in rows],
                "pagination": {
                    "page": page,
                    "limit": limit,
                    "total_records": total_records,
                    "total_pages": max(1, total_pages)
                }
            }
        except Exception as e:
            logger.error(f"Error reading inbox from DB: {e}")
            return {"stats": {"total": 0, "unread": 0}, "records": [], "pagination": {"page": page, "limit": limit, "total_records": 0, "total_pages": 1}}


def delete_inbox_message(msg_id):
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM inbox WHERE id = ?", (msg_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Error deleting inbox message: {e}")
            return False

def delete_inbox_messages_bulk(msg_ids):
    if not msg_ids:
        return 0
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            placeholders = ",".join(["?"] * len(msg_ids))
            cursor.execute(f"DELETE FROM inbox WHERE id IN ({placeholders})", msg_ids)
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted_count
        except Exception as e:
            logger.error(f"Error bulk deleting inbox messages: {e}")
            return 0

def clear_all_inbox_messages():
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM inbox")
            deleted_count = cursor.rowcount
            conn.commit()
            conn.close()
            return deleted_count
        except Exception as e:
            logger.error(f"Error clearing all inbox messages: {e}")
            return 0


def background_inbox_poller():
    logger.info("Background SIM800L Inbox Poller active")
    while True:
        try:
            time.sleep(15)
            poll_inbox_messages()
        except Exception as e:
            logger.error(f"Background inbox poller error: {e}")

# Launch background poller thread
inbox_thread = threading.Thread(target=background_inbox_poller, daemon=True)
inbox_thread.start()


def load_history():
    res = load_history_paginated(page=1, limit=1000)
    return res["records"]


def add_history_record(record):
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO history (id, timestamp, phone_number, message, status, raw_response, app_name)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.get("id"),
                    record.get("timestamp"),
                    record.get("phone_number"),
                    record.get("message"),
                    record.get("status"),
                    record.get("raw_response"),
                    record.get("app_name", "Dashboard")
                )
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error saving history record to SQLite DB: {e}")

def load_keys():
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("SELECT app_name, api_key FROM api_keys")
            rows = cursor.fetchall()
            conn.close()
            return {app_name: api_key for app_name, api_key in rows}
        except Exception as e:
            logger.error(f"Error reading API keys from SQLite DB: {e}")
            return {}

def save_key(app_name, api_key):
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO api_keys (app_name, api_key, created_at) VALUES (?, ?, ?)",
                (app_name, api_key, datetime.datetime.now().isoformat())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error saving API key to SQLite DB: {e}")

def delete_key(app_name):
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute("DELETE FROM api_keys WHERE app_name = ?", (app_name,))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"Error deleting API key from SQLite DB: {e}")


def resolve_app_name(request: Request, api_key: str = None) -> str:
    source_header = request.headers.get("X-Request-Source")
    if source_header and source_header.lower() == "dashboard":
        return "Dashboard"
    if api_key:
        if SMS_SENDER_API_KEY and api_key == SMS_SENDER_API_KEY:
            return "Master Admin Key"
        keys_data = load_keys()
        for app, key in keys_data.items():
            if key == api_key:
                return app
        return "Master Admin Key"
    return "Dashboard" if not SMS_SENDER_API_KEY else "Anonymous"

def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    if SMS_SENDER_API_KEY:
        if api_key == SMS_SENDER_API_KEY:
            return api_key
        keys_data = load_keys()
        if api_key in keys_data.values():
            return api_key
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing API Key"
        )
    return api_key

def verify_api_key_or_dashboard(
    api_key: str = Security(API_KEY_HEADER),
    credentials: HTTPBasicCredentials = Depends(security_basic)
):
    if api_key:
        if not SMS_SENDER_API_KEY or api_key == SMS_SENDER_API_KEY:
            return api_key
        keys_data = load_keys()
        if api_key in keys_data.values():
            return api_key
            
    if DASHBOARD_PASSWORD:
        if credentials:
            user_input = credentials.username.strip() if credentials.username else ""
            pass_input = credentials.password.strip() if credentials.password else ""
            if secrets.compare_digest(user_input, DASHBOARD_USERNAME) and secrets.compare_digest(pass_input, DASHBOARD_PASSWORD):
                return "dashboard"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="SMS Sender Gateway"'},
        )
        
    if not SMS_SENDER_API_KEY and not DASHBOARD_PASSWORD:
        return "public"

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Invalid or missing API Key or Dashboard Auth"
    )

def verify_admin_key(
    api_key: str = Security(API_KEY_HEADER),
    credentials: HTTPBasicCredentials = Depends(security_basic)
):
    if api_key and SMS_SENDER_API_KEY and api_key == SMS_SENDER_API_KEY:
        return api_key

    if DASHBOARD_PASSWORD and credentials:
        user_input = credentials.username.strip() if credentials.username else ""
        pass_input = credentials.password.strip() if credentials.password else ""
        if secrets.compare_digest(user_input, DASHBOARD_USERNAME) and secrets.compare_digest(pass_input, DASHBOARD_PASSWORD):
            return "dashboard"

    if not SMS_SENDER_API_KEY and not DASHBOARD_PASSWORD:
        return "public"

    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Master Admin API Key or Dashboard Auth is required for this operation"
    )

class KeyCreateRequest(BaseModel):
    app_name: str

class KeyCreateResponse(BaseModel):
    app_name: str
    key: str

class RevokeResponse(BaseModel):
    success: bool
    message: str

class HealthResponse(BaseModel):
    status: str
    hardware: str | None = None
    error: str | None = None
    details: dict[str, str] | None = None

class SMSSuccessResponse(BaseModel):
    success: bool
    phone_number: str
    message: str
    raw_response: str

SERIAL_PORT = "/dev/ttyAMA0"
BAUD_RATE = 9600
serial_lock = threading.Lock()

class SMSRequest(BaseModel):
    phone_number: str
    message: str

def send_at_command(ser, cmd, expected_response="OK", timeout=5, delay=None):
    logger.info(f"Sending AT Command: {cmd}")
    # Clear any previous buffers
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    
    response_lines = []
    start_time = time.time()
    found = False
    
    orig_timeout = ser.timeout
    ser.timeout = timeout
    
    while time.time() - start_time < timeout:
        raw_line = ser.readline()
        if not raw_line:
            # readline returned empty bytes due to actual timeout
            break
        line = raw_line.decode(errors="ignore").strip()
        if line:
            response_lines.append(line)
            logger.info(f"Command: {cmd} -> Response Line: {line}")
            if expected_response in line:
                found = True
                break
            if "ERROR" in line or "+CME ERROR:" in line or "+CMS ERROR:" in line:
                break
            
    ser.timeout = orig_timeout
    if found:
        return "\n".join(response_lines)
    return None

def query_at_command(ser, cmd, timeout=3):
    logger.info(f"Querying AT Command: {cmd}")
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    
    response_lines = []
    start_time = time.time()
    
    orig_timeout = ser.timeout
    ser.timeout = timeout
    
    while time.time() - start_time < timeout:
        raw_line = ser.readline()
        if not raw_line:
            break
        line = raw_line.decode(errors="ignore").strip()
        if line:
            response_lines.append(line)
            logger.info(f"Query: {cmd} -> Response Line: {line}")
            if "OK" in line or "ERROR" in line or "+CME ERROR:" in line or "+CMS ERROR:" in line:
                break
                
    ser.timeout = orig_timeout
    return "\n".join(response_lines) if response_lines else None

def get_serial_device(port=SERIAL_PORT, baud=BAUD_RATE, timeout=5, fast_init=False):
    ser = serial.Serial(port, baud, timeout=timeout)
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    
    # Send ESC to cancel any active SMS input prompt (> prompt)
    ser.write(b'\x1b\r\n')
    time.sleep(0.1)
    ser.read_all()

    if fast_init:
        ser.reset_input_buffer()
        ser.write(b'AT\r\n')
        time.sleep(0.15)
        ser.read_all()
        send_at_command(ser, "ATE0", timeout=1)
        return ser
    
    # Auto-baud sync sequence: Send 'AT' to lock SIM800L auto-bauding
    synced = False
    for attempt in range(3):
        ser.reset_input_buffer()
        ser.write(b'AT\r\n')
        time.sleep(0.2)
        res = ser.read_all().decode(errors="ignore")
        if "OK" in res or "AT" in res:
            synced = True
            break
            
    # If sync failed, SIM800L may have locked onto Pi bootloader noise at 115200 baud
    if not synced:
        logger.warning("Failed 9600 baud sync. Attempting 115200 baud recovery...")
        try:
            ser.baudrate = 115200
            for attempt in range(2):
                ser.reset_input_buffer()
                ser.write(b'AT+IPR=9600\r\n')
                time.sleep(0.2)
                res = ser.read_all().decode(errors="ignore")
                if "OK" in res or "AT" in res:
                    logger.info("Forced SIM800L back from 115200 to 9600 baud")
                    break
        except Exception as e:
            logger.error(f"Baud recovery exception: {e}")
        finally:
            ser.baudrate = baud
            time.sleep(0.2)

    # Disable local echo to prevent command loops/responses in output
    send_at_command(ser, "ATE0", timeout=2)
    # Enable verbose error reporting
    send_at_command(ser, "AT+CMEE=2", timeout=2)
    return ser


@app.get(
    "/api/keys",
    response_model=dict[str, str],
    tags=["Admin Key Management"],
    summary="List all registered API keys",
    description="Retrieves a list of all client application names and their associated API keys. Requires the Master Admin Key."
)
def get_api_keys(admin_key: str = Depends(verify_admin_key)):
    return load_keys()

@app.post(
    "/api/keys",
    response_model=KeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
    tags=["Admin Key Management"],
    summary="Create a new client API key",
    description="Generates a new API key for the specified application name. Requires the Master Admin Key."
)
def create_api_key(payload: KeyCreateRequest, admin_key: str = Depends(verify_admin_key)):
    app_name = payload.app_name.strip()
    if not app_name:
        raise HTTPException(status_code=400, detail="Application name cannot be empty")
    keys_data = load_keys()
    if app_name in keys_data:
        raise HTTPException(status_code=400, detail="Key already exists for this application")
    new_key = secrets.token_hex(16)
    save_key(app_name, new_key)
    return {"app_name": app_name, "key": new_key}

@app.delete(
    "/api/keys/{app_name}",
    response_model=RevokeResponse,
    tags=["Admin Key Management"],
    summary="Revoke an existing API key",
    description="Deletes the API key associated with the specified application name. Requires the Master Admin Key."
)
def delete_api_key(app_name: str, admin_key: str = Depends(verify_admin_key)):
    keys_data = load_keys()
    if app_name not in keys_data:
        raise HTTPException(status_code=404, detail="Key not found for this application")
    delete_key(app_name)
    return {"success": True, "message": f"Key for {app_name} revoked successfully"}

def trigger_gpio_reset(pin=RESET_GPIO_PIN):
    logger.info(f"Attempting hardware GPIO pulse on Pin {pin}...")
    # 1. Try RPi.GPIO or gpiod
    try:
        import RPi.GPIO as GPIO
        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)
        GPIO.setup(pin, GPIO.OUT)
        # Active LOW reset pulse for SIM800L RST pin
        GPIO.output(pin, GPIO.LOW)
        time.sleep(0.2)
        GPIO.output(pin, GPIO.HIGH)
        time.sleep(0.1)
        GPIO.cleanup(pin)
        logger.info(f"Hardware reset pulse sent via RPi.GPIO (GPIO {pin})")
        return True, f"Hardware GPIO reset executed on GPIO {pin}"
    except Exception as e1:
        logger.debug(f"RPi.GPIO reset unavailable: {e1}")

    # 2. Try sysfs GPIO fallback
    try:
        gpio_dir = f"/sys/class/gpio/gpio{pin}"
        if not os.path.exists(gpio_dir):
            with open("/sys/class/gpio/export", "w") as f:
                f.write(str(pin))
        with open(f"{gpio_dir}/direction", "w") as f:
            f.write("out")
        with open(f"{gpio_dir}/value", "w") as f:
            f.write("0")
        time.sleep(0.2)
        with open(f"{gpio_dir}/value", "w") as f:
            f.write("1")
        logger.info(f"Hardware reset pulse sent via sysfs (GPIO {pin})")
        return True, f"Hardware GPIO reset executed on GPIO {pin} (sysfs)"
    except Exception as e2:
        logger.debug(f"sysfs GPIO reset unavailable: {e2}")

    return False, f"GPIO pin {pin} unmapped or inaccessible inside container"

def trigger_at_reset():
    acquired = serial_lock.acquire(timeout=3.0)
    if not acquired:
        return False, "Serial lock busy, reset deferred"
    try:
        ser = None
        try:
            ser = get_serial_device(timeout=5)
            send_at_command(ser, "AT+CFUN=1,1", timeout=5)
            time.sleep(1)
            logger.info("Software AT reset command (AT+CFUN=1,1) issued to SIM800L")
            return True, "Software AT reset executed (AT+CFUN=1,1)"
        finally:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"AT software reset failed: {e}")
        return False, f"AT soft reset error: {str(e)}"
    finally:
        serial_lock.release()

def reset_sim800_module():
    gpio_ok, gpio_msg = trigger_gpio_reset()
    time.sleep(0.5)
    at_ok, at_msg = trigger_at_reset()

    if gpio_ok or at_ok:
        details = []
        if gpio_ok:
            details.append(gpio_msg)
        if at_ok:
            details.append(at_msg)
        return {"success": True, "method": "success", "message": " | ".join(details)}
    else:
        return {"success": False, "method": "failed", "message": f"Reset failed: {gpio_msg}; {at_msg}"}


from fastapi.openapi.docs import get_swagger_ui_html, get_redoc_html
from fastapi.openapi.utils import get_openapi

@app.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=False
)
def get_dashboard(auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error loading dashboard: {str(e)}</h3>", status_code=500)

@app.get(
    "/integration",
    response_class=HTMLResponse,
    include_in_schema=False
)
def get_integration_guide(auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    try:
        with open("static/integration.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error loading integration guide: {str(e)}</h3>", status_code=500)

@app.get("/docs", include_in_schema=False)
def get_swagger_documentation(auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    resp = get_swagger_ui_html(openapi_url="/openapi.json", title="SMS Sender API - Docs")
    html = resp.body.decode("utf-8")
    # Inject URL credential sanitizer script to prevent browser fetch credential security exception when URL contains user:pass@
    sanitizer_script = "<script>if(window.location.href.includes('@')){history.replaceState(null,'',window.location.pathname+window.location.search);}</script></head>"
    html = html.replace("</head>", sanitizer_script, 1)
    return HTMLResponse(content=html, status_code=200)

@app.get("/redoc", include_in_schema=False)
def get_redoc_documentation(auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    return get_redoc_html(openapi_url="/openapi.json", title="SMS Sender API - ReDoc")

@app.get("/openapi.json", include_in_schema=False)
def get_open_api_endpoint(auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    return get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes, tags=tags_metadata)

@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="Service health check",
    description="Verifies container health and performs comprehensive hardware diagnostic tests on the SIM800L module."
)
def health_check():
    acquired = serial_lock.acquire(timeout=1.5)
    if not acquired:
        return {
            "status": "busy",
            "hardware": "SIM800L serial port is actively processing a request or polling SMS",
            "details": {"info": "Serial lock busy"}
        }
    try:
        ser = None
        try:
            ser = get_serial_device(timeout=2)
            
            # 1. Basic UART test
            if not send_at_command(ser, "AT"):
                return {
                    "status": "unhealthy",
                    "hardware": "SIM800L module defective or not responding to AT commands (Check power & TX/RX wiring)",
                    "error": "UART Communication Failed"
                }
            
            details = {}
            
            # 2. Module Info Check (ATI)
            ati = query_at_command(ser, "ATI", timeout=1.5)
            if ati:
                details["module_info"] = ati.replace("\r", " ").replace("\n", " ").strip()
                
            # 3. Power Supply Voltage Check (AT+CBC)
            cbc = query_at_command(ser, "AT+CBC", timeout=1.5)
            if cbc:
                details["power_supply"] = cbc.replace("\r", " ").replace("\n", " ").strip()
                
            # 4. SIM Card Status (AT+CPIN?)
            cpin = query_at_command(ser, "AT+CPIN?", timeout=1.5)
            sim_ok = False
            if cpin:
                cpin_clean = cpin.replace("\r", " ").replace("\n", " ").strip()
                details["sim_card"] = cpin_clean
                if "READY" in cpin_clean:
                    sim_ok = True
            else:
                details["sim_card"] = "No response from SIM card"
                
            # 5. Signal Quality (AT+CSQ)
            csq = query_at_command(ser, "AT+CSQ", timeout=1.5)
            if csq:
                details["signal_quality"] = csq.replace("\r", " ").replace("\n", " ").strip()
                
            # 6. Network Registration (AT+CREG?)
            creg = query_at_command(ser, "AT+CREG?", timeout=1.5)
            if creg:
                details["network_registration"] = creg.replace("\r", " ").replace("\n", " ").strip()
                
            if not sim_ok:
                return {
                    "status": "degraded",
                    "hardware": "SIM800L operational, but SIM card is defective, missing, or locked",
                    "error": details.get("sim_card", "SIM card error"),
                    "details": details
                }
                
            return {
                "status": "healthy",
                "hardware": "SIM800L module fully functional",
                "details": details
            }
        finally:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
    except Exception as e:
        return {"status": "error", "error": str(e)}
    finally:
        serial_lock.release()

@app.post(
    "/api/hardware/reset",
    tags=["System"],
    summary="Reset SIM800L module",
    description="Triggers a hardware reset pulse on GPIO 17 (RST pin) and an AT software reset (AT+CFUN=1,1) to reboot the SIM800L module."
)
def reset_hardware_module(auth: str = Depends(verify_api_key_or_dashboard)):
    result = reset_sim800_module()
    if not result["success"]:
        raise HTTPException(status_code=500, detail=result["message"])
    return result


@app.get(
    "/api/debug/inbox-poll",
    tags=["System"],
    summary="Debug SMS inbox polling",
    description="Performs an immediate manual poll of the SIM800L SMS inbox and returns complete raw AT command traces for diagnostics."
)
def debug_inbox_poll(auth: str = Depends(verify_api_key_or_dashboard)):
    acquired = serial_lock.acquire(timeout=5.0)
    if not acquired:
        return {"success": False, "error": "Serial lock busy. Retry in a few seconds."}
    try:
        ser = None
        trace = {}
        try:
            ser = get_serial_device(timeout=3, fast_init=True)
            trace["at"] = send_at_command(ser, "AT", timeout=2)
            trace["cpin"] = query_at_command(ser, "AT+CPIN?", timeout=2)
            trace["creg"] = query_at_command(ser, "AT+CREG?", timeout=2)
            trace["csq"] = query_at_command(ser, "AT+CSQ", timeout=2)
            trace["cmgf"] = send_at_command(ser, "AT+CMGF=1", timeout=2)
            trace["cpms"] = query_at_command(ser, 'AT+CPMS="SM","SM","SM"', timeout=2)
            trace["cnmi"] = send_at_command(ser, 'AT+CNMI=2,1,0,0,0', timeout=2)
            
            raw_cmgl = query_at_command(ser, 'AT+CMGL="ALL"', timeout=5)
            trace["cmgl_raw"] = raw_cmgl
            
            parsed = parse_cmgl_response(raw_cmgl) if raw_cmgl else []
            trace["parsed_messages_count"] = len(parsed)
            trace["parsed_messages"] = parsed
            
            # Also test PDU mode CMGL just in case
            send_at_command(ser, "AT+CMGF=0", timeout=2)
            trace["cmgl_pdu_mode_raw"] = query_at_command(ser, 'AT+CMGL=4', timeout=5)
            
            return {
                "success": True,
                "timestamp": datetime.datetime.now().isoformat(),
                "trace": trace
            }
        finally:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        serial_lock.release()

@app.get(
    "/inbox",
    response_class=HTMLResponse,
    include_in_schema=False
)
def get_inbox_page(auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    try:
        with open("static/inbox.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error loading inbox page: {str(e)}</h3>", status_code=500)

@app.get(
    "/api/inbox",
    response_model=dict,
    tags=["SMS Operations"],
    summary="Get received SMS inbox messages",
    description="Retrieves a paginated list of all SMS text messages received by the SIM800L module."
)
def get_inbox_messages(
    page: int = 1,
    limit: int = 50,
    search: str = None,
    auth: str = Depends(verify_api_key_or_dashboard)
):
    return load_inbox_paginated(page=page, limit=limit, search=search)

class BulkDeleteRequest(BaseModel):
    ids: list[str]

@app.delete(
    "/api/inbox/{msg_id}",
    tags=["SMS Operations"],
    summary="Delete received SMS from inbox",
    description="Deletes an inbox record by ID."
)
def delete_inbox_msg(msg_id: str, auth: str = Depends(verify_api_key_or_dashboard)):
    if delete_inbox_message(msg_id):
        return {"success": True, "message": f"Message {msg_id} deleted"}
    raise HTTPException(status_code=404, detail="Message not found")

@app.post(
    "/api/inbox/delete-bulk",
    tags=["SMS Operations"],
    summary="Mass delete selected received SMS from inbox",
    description="Deletes a list of inbox records by ID."
)
def bulk_delete_inbox_msgs(payload: BulkDeleteRequest, auth: str = Depends(verify_api_key_or_dashboard)):
    count = delete_inbox_messages_bulk(payload.ids)
    return {"success": True, "deleted_count": count, "message": f"Successfully deleted {count} inbox record(s)"}

class SettingsUpdateRequest(BaseModel):
    settings: dict[str, str]

def run_retention_cleanup():
    retention_str = get_setting("retention_days", "30")
    try:
        days = int(retention_str)
        if days <= 0:
            return
        init_db()
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cutoff_date = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat()
            cursor.execute("DELETE FROM history WHERE timestamp < ?", (cutoff_date,))
            del_hist = cursor.rowcount
            cursor.execute("DELETE FROM inbox WHERE timestamp < ?", (cutoff_date,))
            del_inb = cursor.rowcount
            conn.commit()
            conn.close()
            if del_hist > 0 or del_inb > 0:
                logger.info(f"[RETENTION CLEANUP] Deleted {del_hist} history & {del_inb} inbox records older than {days} days")
    except Exception as e:
        logger.error(f"Error during retention cleanup: {e}")

def background_retention_scheduler():
    logger.info("Background Retention Cleanup Scheduler active")
    while True:
        try:
            time.sleep(86400)
            run_retention_cleanup()
        except Exception as e:
            logger.error(f"Background retention scheduler error: {e}")

retention_thread = threading.Thread(target=background_retention_scheduler, daemon=True)
retention_thread.start()

@app.get(
    "/settings",
    response_class=HTMLResponse,
    include_in_schema=False
)
def get_settings_page(auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    try:
        with open("static/settings.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error loading settings page: {str(e)}</h3>", status_code=500)

@app.get(
    "/api/settings",
    response_model=dict,
    tags=["System"],
    summary="Get Gateway Settings",
    description="Retrieves current gateway settings, blacklist rules, auto-delete filters, and webhook configurations."
)
def get_gateway_settings(auth: str = Depends(verify_api_key_or_dashboard)):
    return get_all_settings()

@app.post(
    "/api/settings",
    tags=["System"],
    summary="Update Gateway Settings",
    description="Updates gateway configuration settings in database."
)
def update_gateway_settings(payload: SettingsUpdateRequest, auth: str = Depends(verify_api_key_or_dashboard)):
    if update_settings_bulk(payload.settings):
        return {"success": True, "message": "Settings updated successfully", "settings": get_all_settings()}
    raise HTTPException(status_code=500, detail="Failed to update settings in database")

@app.post(
    "/api/inbox/clear",
    tags=["SMS Operations"],
    summary="Clear all received SMS from inbox",
    description="Deletes all inbox records from database."
)
def clear_all_inbox_msgs(auth: str = Depends(verify_api_key_or_dashboard)):
    count = clear_all_inbox_messages()
    return {"success": True, "deleted_count": count, "message": f"Cleared all {count} inbox record(s)"}



@app.get(
    "/history",
    response_class=HTMLResponse,
    include_in_schema=False
)
def get_history_page(auth: HTTPBasicCredentials = Depends(verify_dashboard_auth)):
    try:
        with open("static/history.html", "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error loading history page: {str(e)}</h3>", status_code=500)


@app.get(
    "/api/history",
    response_model=dict,
    tags=["System"],
    summary="Get SMS dispatch history & metrics",
    description="Retrieves a list of all recorded SMS dispatch attempts, along with summary counts for credit tracking."
)
def get_sms_history(
    page: int = 1,
    limit: int = 50,
    search: str = None,
    export_all: bool = False,
    auth: str = Depends(verify_api_key_or_dashboard)
):
    stats = get_history_stats()
    result = load_history_paginated(page=page, limit=limit, search=search, export_all=export_all)
    return {
        "stats": stats,
        "pagination": result["pagination"],
        "history": result["records"]
    }


@app.post(
    "/send-sms",
    response_model=SMSSuccessResponse,
    tags=["SMS Operations"],
    summary="Send an SMS message",
    description="Instructs the SIM800L module to transmit a text message to the specified phone number. Requires a valid API Key or Master Admin Key."
)
def send_sms(payload: SMSRequest, request: Request, api_key: str = Depends(verify_api_key)):
    import datetime

    if is_number_blocked(payload.phone_number):
        raise HTTPException(
            status_code=400,
            detail=f"Destination phone number '{payload.phone_number}' is blacklisted in gateway settings."
        )

    app_name = resolve_app_name(request, api_key)

    acquired = serial_lock.acquire(timeout=10.0)
    if not acquired:
        raise HTTPException(status_code=503, detail="SIM800L serial port actively in use. Please retry in a few seconds.")
    
    logger.info(f"Received request from [{app_name}] to send SMS to {payload.phone_number}")
    ser = None
    try:
        try:
            ser = get_serial_device(timeout=10)
            
            # Test communication
            if not send_at_command(ser, "AT"):
                raise HTTPException(status_code=502, detail="SIM800L hardware not responding")
                
            # Wait for SIM card to finish initializing if busy or in CFUN 0/4 state
            for attempt in range(5):
                cpin = query_at_command(ser, "AT+CPIN?", timeout=2)
                if cpin and "READY" in cpin:
                    break
                if cpin and ("CFUN state" in cpin or "SIM busy" in cpin):
                    logger.info(f"SIM card in busy/CFUN state (attempt {attempt+1}/5), executing AT+CFUN=1 and waiting 2s...")
                    send_at_command(ser, "AT+CFUN=1", timeout=3)
                    time.sleep(2)
                else:
                    break
                    
            # Select Text Mode (with retries for transient busy state)
            cmgf_success = False
            for attempt in range(3):
                if send_at_command(ser, "AT+CMGF=1"):
                    cmgf_success = True
                    break
                if attempt < 2:
                    logger.info(f"AT+CMGF=1 attempt {attempt+1} failed, retrying in 1.5 seconds...")
                    time.sleep(1.5)
                
            if not cmgf_success:
                # Collect diagnostic information
                cpin_res = query_at_command(ser, "AT+CPIN?", timeout=2)
                creg_res = query_at_command(ser, "AT+CREG?", timeout=2)
                csq_res = query_at_command(ser, "AT+CSQ", timeout=2)
                
                detail_msg = "Failed to set GSM text mode."
                diagnostics = []
                if cpin_res:
                    diagnostics.append(f"SIM: {cpin_res.replace(chr(10), ' ').replace(chr(13), ' ').strip()}")
                if creg_res:
                    diagnostics.append(f"Network: {creg_res.replace(chr(10), ' ').replace(chr(13), ' ').strip()}")
                if csq_res:
                    diagnostics.append(f"Signal: {csq_res.replace(chr(10), ' ').replace(chr(13), ' ').strip()}")
                
                if diagnostics:
                    detail_msg += " Diagnostics: " + " | ".join(diagnostics)
                
                add_history_record({
                    "id": f"sms_{int(time.time())}_{secrets.token_hex(4)}",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "phone_number": payload.phone_number,
                    "message": payload.message,
                    "status": "failed",
                    "raw_response": detail_msg,
                    "app_name": app_name
                })
                raise HTTPException(status_code=502, detail=detail_msg)
                
            # Set character set to GSM
            send_at_command(ser, 'AT+CSCS="GSM"')
                
            # Send recipient number
            ser.reset_input_buffer()
            ser.write(f'AT+CMGS="{payload.phone_number}"\r\n'.encode())
            
            # Wait up to 3s for > prompt
            prompt_found = False
            start_p = time.time()
            while time.time() - start_p < 3:
                p_line = ser.readline().decode(errors="ignore")
                if ">" in p_line:
                    prompt_found = True
                    break
            
            # Write SMS body and terminate with Ctrl+Z (ASCII 26)
            ser.write((payload.message + chr(26)).encode())
            logger.info("Transmitting message payload over GSM network...")
            
            # Wait up to 15 seconds for carrier response line-by-line
            response_lines = []
            start_t = time.time()
            ser.timeout = 1.0
            
            while time.time() - start_t < 15:
                raw_l = ser.readline()
                if raw_l:
                    l = raw_l.decode(errors="ignore").strip()
                    if l:
                        response_lines.append(l)
                        logger.info(f"CMGS Transmit Response Line: {l}")
                        if "+CMGS:" in l or "OK" in l or "ERROR" in l or "Call Ready" in l:
                            # Read any trailing OK line
                            time.sleep(0.3)
                            extra = ser.read_all().decode(errors="ignore").strip()
                            if extra:
                                response_lines.append(extra)
                            break

            response = "\n".join(response_lines)
            logger.info(f"Full Carrier Response: {response.strip()}")
            
            if "+CMGS:" in response or ("OK" in response and prompt_found):
                add_history_record({
                    "id": f"sms_{int(time.time())}_{secrets.token_hex(4)}",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "phone_number": payload.phone_number,
                    "message": payload.message,
                    "status": "success",
                    "raw_response": response.strip(),
                    "app_name": app_name
                })
                return {
                    "success": True,
                    "phone_number": payload.phone_number,
                    "message": payload.message,
                    "raw_response": response.strip()
                }
            elif "Call Ready" in response or "SMS Ready" in response or "NORMAL POWER DOWN" in response:
                logger.error(f"Hardware brownout detected during transmission. Module output: {response.strip()}")
                err_text = f"Hardware Brownout Reset: SIM800L rebooted during transmission. (Raw output: {response.strip()})"
                add_history_record({
                    "id": f"sms_{int(time.time())}_{secrets.token_hex(4)}",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "phone_number": payload.phone_number,
                    "message": payload.message,
                    "status": "failed",
                    "raw_response": err_text,
                    "app_name": app_name
                })
                raise HTTPException(
                    status_code=500,
                    detail=f"Hardware Brownout Reset: SIM800L rebooted during transmission due to peak current voltage drop. Ensure 4.0V / 2A+ power supply and add a 1000uF capacitor across VCC and GND. (Raw output: {response.strip()})"
                )
            else:
                err_text = f"SMS rejected by network carrier: {response.strip()}"
                add_history_record({
                    "id": f"sms_{int(time.time())}_{secrets.token_hex(4)}",
                    "timestamp": datetime.datetime.now().isoformat(),
                    "phone_number": payload.phone_number,
                    "message": payload.message,
                    "status": "failed",
                    "raw_response": err_text,
                    "app_name": app_name
                })
                raise HTTPException(
                    status_code=500,
                    detail=err_text
                )
        finally:
            if ser and ser.is_open:
                try:
                    ser.close()
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Error executing SMS dispatch: {e}")
        if isinstance(e, HTTPException):
            raise e
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        serial_lock.release()
