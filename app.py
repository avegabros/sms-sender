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
        
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_history_timestamp ON history(timestamp DESC)")
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

def load_history():
    init_db()
    with db_lock:
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT id, timestamp, phone_number, message, status, raw_response, app_name FROM history ORDER BY timestamp DESC LIMIT 10000")
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error reading history from SQLite DB: {e}")
            return []

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

def verify_admin_key(api_key: str = Security(API_KEY_HEADER)):
    if SMS_SENDER_API_KEY:
        if api_key != SMS_SENDER_API_KEY:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Master Admin API Key is required for this operation"
            )
    return api_key

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

def get_serial_device(port=SERIAL_PORT, baud=BAUD_RATE, timeout=10):
    ser = serial.Serial(port, baud, timeout=timeout)
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    
    # Send ESC to cancel any active SMS input prompt (> prompt)
    ser.write(b'\x1b\r\n')
    time.sleep(0.3)
    ser.read_all()
    
    # Auto-baud sync sequence: Send 'AT' multiple times to lock SIM800L auto-bauding
    synced = False
    for attempt in range(5):
        ser.reset_input_buffer()
        ser.write(b'AT\r\n')
        time.sleep(0.3)
        res = ser.read_all().decode(errors="ignore")
        if "OK" in res or "AT" in res:
            synced = True
            logger.info("SIM800L serial sync successful")
            break
            
    # If sync failed, SIM800L may have locked onto Pi bootloader noise at 115200 baud
    if not synced:
        logger.warning("Failed 9600 baud sync. Attempting 115200 baud recovery...")
        try:
            ser.baudrate = 115200
            for attempt in range(3):
                ser.reset_input_buffer()
                ser.write(b'AT+IPR=9600\r\n')
                time.sleep(0.3)
                res = ser.read_all().decode(errors="ignore")
                if "OK" in res or "AT" in res:
                    logger.info("Forced SIM800L back from 115200 to 9600 baud")
                    break
        except Exception as e:
            logger.error(f"Baud recovery exception: {e}")
        finally:
            ser.baudrate = baud
            time.sleep(0.3)

    # Permanently lock baud rate to 9600 in SIM800L non-volatile memory (EEPROM)
    send_at_command(ser, "AT+IPR=9600", timeout=2)
    send_at_command(ser, "AT&W", timeout=2)

    # Disable local echo to prevent command loops/responses in output
    send_at_command(ser, "ATE0", timeout=2)
    # Enable verbose error reporting
    send_at_command(ser, "AT+CMEE=2", timeout=2)
    # Force full RF & SIM functionality (exit minimum functionality / flight mode)
    send_at_command(ser, "AT+CFUN=1", timeout=3)
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
    with serial_lock:
        try:
            ser = get_serial_device(timeout=5)
            send_at_command(ser, "AT+CFUN=1,1", timeout=5)
            time.sleep(2)
            ser.close()
            logger.info("Software AT reset command (AT+CFUN=1,1) issued to SIM800L")
            return True, "Software AT reset executed (AT+CFUN=1,1)"
        except Exception as e:
            logger.error(f"AT software reset failed: {e}")
            return False, f"AT soft reset error: {str(e)}"

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
    return get_swagger_ui_html(openapi_url="/openapi.json", title="SMS Sender API - Docs")

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
    with serial_lock:
        try:
            ser = get_serial_device(timeout=3)
            
            # 1. Basic UART test
            if not send_at_command(ser, "AT"):
                ser.close()
                return {
                    "status": "unhealthy",
                    "hardware": "SIM800L module defective or not responding to AT commands (Check power & TX/RX wiring)",
                    "error": "UART Communication Failed"
                }
            
            details = {}
            
            # 2. Module Info Check (ATI)
            ati = query_at_command(ser, "ATI", timeout=2)
            if ati:
                details["module_info"] = ati.replace("\r", " ").replace("\n", " ").strip()
                
            # 3. Power Supply Voltage Check (AT+CBC)
            cbc = query_at_command(ser, "AT+CBC", timeout=2)
            if cbc:
                details["power_supply"] = cbc.replace("\r", " ").replace("\n", " ").strip()
                
            # 4. SIM Card Status (AT+CPIN?)
            cpin = query_at_command(ser, "AT+CPIN?", timeout=2)
            sim_ok = False
            if cpin:
                cpin_clean = cpin.replace("\r", " ").replace("\n", " ").strip()
                details["sim_card"] = cpin_clean
                if "READY" in cpin_clean:
                    sim_ok = True
            else:
                details["sim_card"] = "No response from SIM card"
                
            # 5. Signal Quality (AT+CSQ)
            csq = query_at_command(ser, "AT+CSQ", timeout=2)
            if csq:
                details["signal_quality"] = csq.replace("\r", " ").replace("\n", " ").strip()
                
            # 6. Network Registration (AT+CREG?)
            creg = query_at_command(ser, "AT+CREG?", timeout=2)
            if creg:
                details["network_registration"] = creg.replace("\r", " ").replace("\n", " ").strip()
                
            ser.close()
            
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
        except Exception as e:
            return {"status": "error", "error": str(e)}

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
def get_sms_history(auth: str = Depends(verify_api_key_or_dashboard)):
    history = load_history()
    success_count = sum(1 for item in history if item.get("status") == "success")
    failed_count = sum(1 for item in history if item.get("status") == "failed")
    return {
        "stats": {
            "total": len(history),
            "success": success_count,
            "failed": failed_count
        },
        "history": history
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
    app_name = resolve_app_name(request, api_key)
    with serial_lock:
        logger.info(f"Received request from [{app_name}] to send SMS to {payload.phone_number}")
        try:
            ser = get_serial_device(timeout=10)
            
            # Test communication
            if not send_at_command(ser, "AT"):
                ser.close()
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
                ser.close()
                
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
            ser.write(f'AT+CMGS="{payload.phone_number}"\r\n'.encode())
            time.sleep(0.5)
            
            # Write SMS body and terminate with Ctrl+Z (ASCII 26)
            ser.write((payload.message + chr(26)).encode())
            logger.info("Transmitting message payload...")
            
            # Wait for carrier response (can take several seconds)
            time.sleep(4)
            response = ser.read_all().decode(errors="ignore")
            ser.close()
            
            logger.info(f"Carrier Response: {response.strip()}")
            
            if "+CMGS:" in response:
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
                
        except Exception as e:
            logger.error(f"Error executing SMS dispatch: {e}")
            if isinstance(e, HTTPException):
                raise e
            raise HTTPException(status_code=500, detail=str(e))
