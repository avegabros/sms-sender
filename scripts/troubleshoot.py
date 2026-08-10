#!/usr/bin/env python3
"""
SMS Sender Gateway - Hardware & Network Diagnostic Tool
Executes serial checks, SIM status, signal quality (CSQ), SMSC number, and USSD balance queries.
"""

import sys
import os
import time
import serial

SERIAL_PORT = os.getenv("SERIAL_PORT", "/dev/ttyAMA0")
BAUD_RATE = int(os.getenv("BAUD_RATE", "9600"))

def send_cmd(ser, cmd, timeout=3):
    ser.reset_input_buffer()
    ser.write((cmd + "\r\n").encode())
    lines = []
    start = time.time()
    while time.time() - start < timeout:
        raw = ser.readline()
        if not raw:
            break
        line = raw.decode(errors="ignore").strip()
        if line:
            lines.append(line)
            if "OK" in line or "ERROR" in line or "+CME ERROR:" in line or "+CMS ERROR:" in line:
                break
    return "\n".join(lines)

def run_diagnostics():
    print("=" * 60)
    print("      SMS SENDER GATEWAY - RASPBERRY PI 5 DIAGNOSTICS      ")
    print("=" * 60)
    print(f"Target Serial Port: {SERIAL_PORT} @ {BAUD_RATE} baud\n")

    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=3)
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        ser.write(b'\x1b\r\n')
        time.sleep(0.2)
        ser.read_all()

        # 1. AT Test
        at_res = send_cmd(ser, "AT")
        print(f"[1] Basic Communication (AT): {'✅ OK' if 'OK' in at_res else '❌ FAILED'}")

        # 2. SIM Card Status
        cpin_res = send_cmd(ser, "AT+CPIN?")
        print(f"[2] SIM Card Status (AT+CPIN?): {cpin_res.replace(chr(10), ' ').replace(chr(13), ' ')}")

        # 3. Network Registration
        creg_res = send_cmd(ser, "AT+CREG?")
        print(f"[3] Network Registration (AT+CREG?): {creg_res.replace(chr(10), ' ').replace(chr(13), ' ')}")

        # 4. Signal Quality
        csq_res = send_cmd(ser, "AT+CSQ")
        print(f"[4] Signal Quality (AT+CSQ): {csq_res.replace(chr(10), ' ').replace(chr(13), ' ')}")

        # 5. SMS Service Center
        csca_res = send_cmd(ser, "AT+CSCA?")
        print(f"[5] SMS Service Center (AT+CSCA?): {csca_res.replace(chr(10), ' ').replace(chr(13), ' ')}")

        # 6. SIM Storage Capacity
        send_cmd(ser, "AT+CMGF=1")
        cpms_res = send_cmd(ser, 'AT+CPMS="SM","SM","SM"')
        print(f"[6] SIM Card Message Storage (AT+CPMS): {cpms_res.replace(chr(10), ' ').replace(chr(13), ' ')}")

        # 7. USSD Carrier Balance Test
        print("\n[7] Querying Carrier USSD Balance (*143#)...")
        send_cmd(ser, "AT+CUSD=1")
        send_cmd(ser, 'AT+CUSD=1,"*143#",15', timeout=5)
        time.sleep(2)
        ussd_extra = ser.read_all().decode(errors="ignore").strip()
        if ussd_extra:
            print(f"    USSD Output: {ussd_extra}")
            if "+CUSD: 2" in ussd_extra:
                print("    ⚠️ WARNING (+CUSD: 2): Network terminated USSD. Ensure SIM has active load/promo.")
            else:
                print("    ✅ Carrier USSD responded successfully.")
        else:
            print("    ℹ️ No extra USSD payload received.")

        ser.close()
        print("\n" + "=" * 60)
        print("Diagnostic Complete.")
        print("=" * 60)

    except Exception as e:
        print(f"❌ Error communicating with serial port {SERIAL_PORT}: {e}")

if __name__ == "__main__":
    run_diagnostics()
