# System Architecture & Technical Specification

**SMS Sender Gateway (`sms-sender`)**  
A high-performance, Dockerized RESTful SMS API Gateway microservice running on Raspberry Pi 5 using a SIM800L cellular transceiver module.

---

## 1. System Architecture Overview

The `sms-sender` system bridges standard web client applications (Node.js, Python, PHP, Laravel, cURL) with physical GSM cellular networks. It translates high-level REST API calls into low-level Hayes AT commands executed over serial UART.

```mermaid
graph TD
    Client["Client Applications / Systems"] -->|HTTP / HTTPS REST| Proxy["Nginx Proxy / Cloudflare"]
    Proxy -->|Port 8080| App["FastAPI Application (Container)"]
    
    subgraph Container ["Docker Container (/app)"]
        App --> Auth["API Key & Basic Auth Guard"]
        App --> DB["SQLite Database (data/sms_sender.db)"]
        App --> Worker["Background Inbox Poller Thread"]
        App --> Serial["Thread-Safe Serial Lock (threading.Lock)"]
    end
    
    Serial -->|UART /dev/ttyAMA0| SIM800L["SIM800L Cellular Transceiver"]
    Worker -->|HTTP POST| Webhook["External Webhook Endpoint"]
    SIM800L <-->|GSM RF 850/900/1800/1900 MHz| Tower["Cellular Carrier Network"]
```

---

## 2. Hardware Architecture & Wiring

### 2.1 Raspberry Pi 5 & SIM800L Interface
The SIM800L module operates at **3.4V – 4.4V** (optimal **4.0V DC**) and requires peak transient currents of up to **2.0 Amps** during cellular transmission bursts.

```mermaid
flowchart LR
    power["5V / 12V External Power"] --> buck["LM2596 Buck Converter (Adjusted to 4.0V)"]
    buck -->|4.0V VCC| sim["SIM800L VCC Pin"]
    buck -->|GND| sim["SIM800L GND Pin"]
    
    pi5["Raspberry Pi 5"] -->|Pin 6 GND| sim["SIM800L GND (Common Ground)"]
    pi5 -->|Pin 8 TXD (GPIO 14)| sim["SIM800L RXD"]
    pi5 -->|Pin 10 RXD (GPIO 15)| sim["SIM800L TXD"]
    pi5 -->|Pin 11 (GPIO 17)| sim["SIM800L RST (Hardware Reset)"]
```

### 2.2 Complete Pin Map Table

| Raspberry Pi 5 Header | SIM800L Module | Function / Specification |
| :--- | :--- | :--- |
| **LM2596 OUT+ (4.0V)** | **VCC** | Main Power Input (4.0V DC / 2.0A peak) |
| **LM2596 OUT- (GND)** | **GND** | Power Ground |
| **Physical Pin 6 (GND)** | **GND** | **Common Ground** (Essential for serial logic reference) |
| **Physical Pin 8 (GPIO 14 / TXD)** | **RXD** | UART Data Transfer (Pi TX -> SIM800 RX) |
| **Physical Pin 10 (GPIO 15 / RXD)** | **TXD** | UART Data Receive (Pi RX <- SIM800 TX) |
| **Physical Pin 11 (GPIO 17)** | **RST** | Active-LOW Hardware Reset Pulse (200ms) |

> [!IMPORTANT]
> **Decoupling Capacitor Requirement**: A **1000µF – 2200µF Low-ESR electrolytic capacitor** must be soldered directly across the SIM800L `VCC` and `GND` pins to absorb RF transmit current spikes and prevent module brownout reboots (`Call Ready` errors).

---

## 3. Software Component Architecture

### 3.1 Core Layers
1. **ASGI Application Server**: Uvicorn running FastAPI (Python 3.11).
2. **Security & Authentication Layer**:
   - `HTTPBasicCredentials` guard for Web Dashboard routes (`/`, `/inbox`, `/history`, `/integration`, `/docs`).
   - `APIKeyHeader` (`X-API-Key`) validation for client application API requests.
3. **Database Persistence Layer**: `sqlite3` thread-safe manager writing to `data/sms_sender.db`.
4. **Serial Lock Manager**: `threading.Lock()` synchronizing all AT serial operations to prevent command collisions.
5. **Background Inbox Poller**: `daemon` thread polling unread SMS messages every 15 seconds.

---

## 4. Database Schema Specification (`data/sms_sender.db`)

```mermaid
erDiagram
    history {
        TEXT id PK
        TEXT timestamp
        TEXT phone_number
        TEXT message
        TEXT status
        TEXT raw_response
        TEXT app_name
    }
    inbox {
        TEXT id PK
        TEXT timestamp
        TEXT sender
        TEXT message
        INTEGER read_status
        TEXT webhook_status
    }
    api_keys {
        TEXT app_name PK
        TEXT api_key UK
        TEXT created_at
    }
```

---

## 5. Sequence & Data Flow Diagrams

### 5.1 Outbound SMS Dispatch Sequence (`POST /send-sms`)

```mermaid
sequenceDiagram
    autonumber
    actor Client
    participant API as FastAPI Gateway
    participant Lock as Serial Lock
    participant DB as SQLite DB
    participant SIM as SIM800L Transceiver
    participant Carrier as Cellular Carrier

    Client->>API: POST /send-sms {phone_number, message} (X-API-Key)
    API->>API: Verify API Key & Resolve App Name
    API->>Lock: Acquire serial_lock
    API->>SIM: AT+CMGF=1 (Set Text Mode)
    SIM-->>API: OK
    API->>SIM: AT+CSCS="GSM" (Set Charset)
    SIM-->>API: OK
    API->>SIM: AT+CMGS="+639171234567"\r\n
    SIM-->>API: > (Prompt)
    API->>SIM: Message Body + Ctrl+Z (ASCII 26)
    SIM->>Carrier: RF Transmission
    Carrier-->>SIM: +CMGS: 42 OK
    SIM-->>API: +CMGS: 42 OK
    API->>Lock: Release serial_lock
    API->>DB: INSERT INTO history (status='success')
    API-->>Client: 200 OK {success: true, raw_response: "+CMGS: 42 OK"}
```

### 5.2 Inbound SMS & Webhook Dispatch Sequence

```mermaid
sequenceDiagram
    autonumber
    actor Sender as Remote Cellphone
    participant SIM as SIM800L Transceiver
    participant Poller as Inbox Poller Thread
    participant DB as SQLite DB
    participant Webhook as External Webhook Endpoint

    Sender->>SIM: Send SMS ("YES booking confirmed")
    Note over SIM: Message stored on SIM memory
    Poller->>SIM: AT+CMGL="ALL"
    SIM-->>Poller: +CMGL: 1,"REC UNREAD","+639171234567","","..."
    Poller->>Poller: Parse Sender, Timestamp, Body
    opt WEBHOOK_URL Configured
        Poller->>Webhook: POST {event: "sms_received", sender, message, timestamp}
        Webhook-->>Poller: 200 OK
    end
    Poller->>DB: INSERT INTO inbox (status='delivered')
    Poller->>SIM: AT+CMGD=1,4 (Purge Read SIM Memory)
    Note over SIM: SIM Memory cleared (0/20 capacity)
```

---

## 6. Network & Reverse Proxy Architecture

```mermaid
graph LR
    User["Public Internet Client"] -->|HTTPS 443| CF["Cloudflare Edge (SSL)"]
    CF -->|Port 80 / 443| Sophos["Sophos Firewall (DNAT)"]
    Sophos -->|Port 8080| NPM["Nginx Proxy Manager"]
    NPM -->|http://sms-sender:8000| Docker["sms-sender Container"]
```

---

## 7. Complete REST API Specifications

| Method | Endpoint | Authorization | Description |
| :--- | :--- | :--- | :--- |
| **`POST`** | `/send-sms` | `X-API-Key` | Transmits an SMS message to a phone number. |
| **`GET`** | `/api/inbox` | `X-API-Key` / Basic Auth | Retrieves received SMS inbox messages with pagination. |
| **`DELETE`** | `/api/inbox/{id}` | `X-API-Key` / Basic Auth | Deletes an inbox record by ID. |
| **`GET`** | `/api/history` | `X-API-Key` / Basic Auth | Retrieves dispatch history metrics & paginated records. |
| **`GET`** | `/health` | Public | Diagnostics check on serial port & SIM800L transceiver. |
| **`POST`** | `/api/hardware/reset` | `X-API-Key` / Basic Auth | Triggers GPIO 17 hardware pulse & AT soft-reset. |
| **`GET`** | `/api/keys` | Master Admin Key | Lists all registered application API keys. |
| **`POST`** | `/api/keys` | Master Admin Key | Generates a new application API key. |
| **`DELETE`** | `/api/keys/{name}` | Master Admin Key | Revokes an application API key. |

---

## 8. Deployment & Environment Variables

### `.env` Configuration File

```env
# HTTP Basic Auth credentials for Dashboard UI
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=your_secure_password

# Master Admin Key for API Key management & dispatch
SMS_SENDER_API_KEY=master_admin_api_key_12345

# Hardware Reset GPIO Pin (Default: 17 for physical Pin 11)
RESET_GPIO_PIN=17

# Optional Real-Time Webhook Target for Incoming SMS
WEBHOOK_URL=https://your-backend.abas.ph/api/sms-received
```
