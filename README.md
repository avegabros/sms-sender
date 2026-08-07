# SMS Sender (`sms-sender`)

A Dockerized RESTful SMS API Gateway microservice running on Raspberry Pi 5 using a SIM800L cellular module.

---

## Hardware Wiring & Configuration

Connecting the SIM800L module to the Raspberry Pi 5 requires a stable power supply and correct serial wiring. The SIM800 module itself operates at **3.4V to 4.4V** (ideally **4.0V**) and draws transient current surges up to **2.0 Amps** during cellular transmission.

---

### Option A: Standard SIM800L Breakout Board (Red Board)
This version does **not** have an onboard 5V regulator. Connecting it directly to the Pi's 5V or 3.3V pins will damage the module or trigger Pi brownouts. You **must** use an **LM2596 DC-DC Buck Converter** to step down the voltage safely to 4.0V.

#### 1. Power Setup (LM2596)
1. **Input Power**: Connect a DC power source (5V-12V external power supply, or the Pi 5's 5V Pin 2/4 if using the official 27W 5A Pi adapter) to `IN+` and `IN-` on the LM2596.
2. **Calibration**: Before connecting to the SIM800L, power on the LM2596 and measure `OUT+` / `OUT-` with a multimeter. Adjust the brass screw until it reads **exactly 4.0V DC**.
3. **Output**: Connect `OUT+` to SIM800L `VCC`, and `OUT-` to SIM800L `GND`.

#### 2. Pin Connections Table (Option A)

| Component A | Component B | Connection Type / Details |
| :--- | :--- | :--- |
| **LM2596 OUT+** | **SIM800L VCC** | Power Input (Regulated 4.0V) |
| **LM2596 OUT-** | **SIM800L GND** | Power Ground |
| **Raspberry Pi 5 GND (Pin 6)** | **SIM800L GND** | **Common Ground** (CRITICAL for serial comms) |
| **Raspberry Pi 5 TXD (GPIO 14, Pin 8)** | **SIM800L RXD** | Serial Data (Pi TX -> SIM800 RX) |
| **Raspberry Pi 5 RXD (GPIO 15, Pin 10)** | **SIM800L TXD** | Serial Data (Pi RX <- SIM800 TX) |

> [!WARNING]
> You must connect the **GND** from the Raspberry Pi 5, the **GND** of the SIM800L, and the **OUT-** of the LM2596 together (**Common Ground**). Without a shared reference ground, serial data signals will corrupt.

---

### Option B: SIM800L EVB (Evaluation Board)
The SIM800L EVB board includes an onboard 5V-to-4V regulator and transistor level shifters. It can be powered directly from the Raspberry Pi 5's 5V GPIO rail.

#### Pin Connections Table (Option B)

| **Raspberry Pi 5 Pin** | **SIM800L EVB Pin** | Function / Details |
| :--- | :--- | :--- |
| **Pin 2 or Pin 4 (5V)** | **`5V`** | 5V Power Input |
| **Pin 6 (GND)** | **`GND`** | Power & Signal Ground |
| **Pin 1 (3.3V)** | **`VDD`** | **Level Shifter Reference** (CRITICAL to prevent floating logic resets) |
| **Pin 8 (GPIO 14 / TXD)** | **`RXD`** | Serial Data (Pi TX -> EVB RX) |
| **Pin 10 (GPIO 15 / RXD)** | **`TXD`** | Serial Data (Pi RX <- EVB TX) |
| **Pin 11 (GPIO 17)** | **`RST`** | **Hardware Reset Pin** (Active LOW 200ms pulse for module reboot) |

> [!TIP]
> **Power Spike Protection (1000µF Capacitor)**:
> Solder a **1000µF or 2200µF (6.3V/10V) electrolytic capacitor** directly across `VCC`/`5V` and `GND` pins on the SIM800L board. This acts as an energy buffer for 2.0A RF transmit spikes, preventing module brownout resets (`Call Ready` errors).

---

### Raspberry Pi 5 Serial Port Configuration

Before launching the Docker container, enable the physical GPIO serial UART on your Raspberry Pi:

1. SSH into your Raspberry Pi 5.
2. Run `sudo raspi-config`.
3. Navigate to **Interface Options** -> **Serial Port**.
4. Prompts:
   - *Would you like a login shell to be accessible over serial?* -> Select **No**.
   - *Would you like the serial port hardware to be enabled?* -> Select **Yes**.
5. Save, exit, and reboot:
   ```bash
   sudo reboot
   ```
6. Verify the serial interface exists (`/dev/ttyAMA0`):
   ```bash
   ls -la /dev/ttyAMA0
   ```

---

## Deployment (via Docker Compose)

1. Create a `.env` file in the root of the project to configure authentication:
   ```env
   # Web Dashboard HTTP Basic Auth (for public IP hosting)
   DASHBOARD_USERNAME=admin
   DASHBOARD_PASSWORD=your_secure_dashboard_password

   # Master Admin API Key for API Key management and SMS dispatch
   SMS_SENDER_API_KEY=your_secure_master_api_key
   ```

2. Start the API gateway inside the `sms-sender` directory:
   ```bash
   docker compose up --build -d
   ```

> [!TIP]
> **Public IP Security Best Practices**:
> - Set `DASHBOARD_USERNAME` and `DASHBOARD_PASSWORD` to protect `/`, `/history`, `/integration`, and `/docs` when exposing the service to a public IP.
> - Deploy behind a reverse proxy (Nginx, Caddy, Cloudflare Tunnel) to enable HTTPS encryption.

---

## Gateway Web UI & Features

The API service runs on port `8080`:

* **Web Dashboard**: `http://[YOUR_PI_IP]:8080/`
  * Graphical interface for configuring API keys, testing SMS dispatch, and viewing real-time terminal logs.
* **SMS History & Credit Tracker**: `http://[YOUR_PI_IP]:8080/history`
  * Displays dispatch history, credit usage metrics, search/filtering, request origin tracking (`Dashboard` vs `App Name`), and CSV export.
* **Multi-Language Integration Guide**: `http://[YOUR_PI_IP]:8080/integration`
  * Interactive code customizers and pre-built code snippets for **Node.js**, **TypeScript**, **PHP (cURL & Guzzle)**, **Laravel**, **CodeIgniter 3 & 4**, **Python**, and **cURL CLI**.

---

## API Reference

### 1. Service Health Check & Diagnostics
Performs automated 6-part hardware diagnostic tests on the SIM800L module (UART, firmware, supply voltage, SIM status, signal strength, network registration).

```http
GET http://[YOUR_PI_IP]:8080/health
```

#### Response (Success - Healthy)
```json
{
  "status": "healthy",
  "hardware": "SIM800L module fully functional",
  "error": null,
  "details": {
    "module_info": "SIM800 R14.18 OK",
    "power_supply": "+CBC: 0,81,4049 OK",
    "sim_card": "+CPIN: READY OK",
    "signal_quality": "+CSQ: 27,0 OK",
    "network_registration": "+CREG: 0,1 OK"
  }
}
```

---

### 2. Send SMS
Sends a text message to a specified recipient number using international format (e.g. `+639171234567`).

```http
POST http://[YOUR_PI_IP]:8080/send-sms
Content-Type: application/json
X-API-Key: your_client_or_master_api_key

{
  "phone_number": "+639171234567",
  "message": "Hello from Raspberry Pi 5 SMS Gateway!"
}
```

#### Response (Success)
```json
{
  "success": true,
  "phone_number": "+639171234567",
  "message": "Hello from Raspberry Pi 5 SMS Gateway!",
  "raw_response": "+CMGS: 42 OK"
}
```

---

### 3. SMS History & Metrics
Retrieves dispatch history logs and credit tracking summary counts.

```http
GET http://[YOUR_PI_IP]:8080/api/history
X-API-Key: your_client_or_master_api_key
```

#### Response (Success)
```json
{
  "stats": {
    "total": 45,
    "success": 42,
    "failed": 3
  },
  "history": [
    {
      "id": "sms_1721748500_a1b2",
      "timestamp": "2026-07-23T15:30:00+08:00",
      "phone_number": "+639171234567",
      "message": "Sample message",
      "status": "success",
      "raw_response": "+CMGS: 42 OK",
      "app_name": "trace-app"
    }
  ]
}
```

---

### 4. Admin API Key Management
Generate, list, and revoke application API keys. Requires the Master Admin Key (`SMS_SENDER_API_KEY`).

* **List API Keys**: `GET /api/keys`
* **Create Client Key**: `POST /api/keys` (`{"app_name": "trace-app"}`)
* **Revoke Client Key**: `DELETE /api/keys/{app_name}`
