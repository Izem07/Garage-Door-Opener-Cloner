# Raspberry Pi 3B+ & CC1101 Garage Door Opener (315 MHz Purple Learn Button)

Emulate a **Chamberlain / LiftMaster / Craftsman** garage door opener remote using a **Raspberry Pi 3B+** and a Texas Instruments **CC1101 Sub-1GHz RF transceiver** module.

This project specifically targets openers with the **Purple "Learn" Button**, which operate on **315.00 MHz** using the **Security+ 1.0** rolling code protocol.

---

## Table of Contents
1. [Overview](#overview)
2. [Hardware Requirements](#hardware-requirements)
3. [Wiring Guide (Pi 3B+ to CC1101)](#wiring-guide-pi-3b-to-cc1101)
4. [Software Setup on Raspberry Pi](#software-setup-on-raspberry-pi)
5. [Pairing with your Garage Door Opener](#pairing-with-your-garage-door-opener)
6. [Daily Usage & Commands](#daily-usage--commands)
7. [How It Works (Security+ 1.0 & Rolling Codes)](#how-it-works-security-10--rolling-codes)
8. [Troubleshooting & Range Optimization](#troubleshooting--range-optimization)

---

## Overview

Chamberlain and LiftMaster garage door openers identify their wireless protocol and frequency by the color of the "Learn" button on the motor unit:

| Learn Button Color | Protocol | Frequency | Years Produced |
| :--- | :--- | :--- | :--- |
| **Purple** | **Security+ 1.0** | **315.00 MHz** | **2005 – 2014** |
| **Orange / Red** | Security+ 1.0 | 390.00 MHz | 1997 – 2005 |
| **Yellow** | Security+ 2.0 | Tri-Band (310/315/390 MHz) | 2011 – Present |
| **Green** | Billion Code | 390.00 MHz | 1993 – 1997 |

This project turns your Raspberry Pi into a genuine virtual remote with its own unique Fixed ID and rolling counter stored locally in `remote_config.json`. Every press increments the rolling code so your door opener stays in sync, eliminating replay attack blocks.

---

## Hardware Requirements

1. **Raspberry Pi 3B+** (or any 40-pin Raspberry Pi model running Raspberry Pi OS).
2. **TI CC1101 Sub-1GHz Transceiver Module** (equipped with an antenna or a 315 MHz wire antenna).
3. **Female-to-Female Jumper Wires** (6 wires required).

> [!CAUTION]
> **Power Supply Warning:** The CC1101 is a **3.3V device**. Connect VCC **ONLY** to a **3.3V pin** on the Raspberry Pi. Connecting it to 5V will permanently destroy the CC1101 module.

---

## Wiring Guide (Pi 3B+ to CC1101)

The CC1101 communicates with the Raspberry Pi over standard **SPI (Bus 0, Chip Select 0)**.

| CC1101 Pin | Raspberry Pi Pin Name | Physical Header Pin | Description |
| :--- | :--- | :--- | :--- |
| **VCC / VDD** | **3.3V Power** | **Pin 1** (or Pin 17) | Power (3.3V ONLY) |
| **GND** | **Ground** | **Pin 6** (or Pin 9, 20, 25) | Ground reference |
| **CSN / CS** | **GPIO 8 (SPI0 CE0)** | **Pin 24** | SPI Chip Select |
| **SCK / SCLK** | **GPIO 11 (SPI0 SCLK)** | **Pin 23** | SPI Clock |
| **MOSI / SI** | **GPIO 10 (SPI0 MOSI)** | **Pin 19** | SPI Master Out Slave In |
| **MISO / SO** | **GPIO 9 (SPI0 MISO)** | **Pin 21** | SPI Master In Slave Out |
| *GDO0* | *GPIO 24 (Optional)* | *Pin 18* | *Optional (not needed for SPI FIFO mode)* |
| *GDO2* | *Unconnected* | — | *Not needed* |

*Note: In SPI FIFO mode used by this driver, hardware buffers handle transmission directly at 2000 baud with zero Linux OS jitter. Only the 6 primary SPI pins are required.*

---

## Software Setup on Raspberry Pi

### 1. Enable the SPI Interface
On your Raspberry Pi terminal:
```bash
sudo raspi-config
```
1. Select **Interface Options** (or **Interfacing Options**).
2. Select **SPI**.
3. Choose **Yes** to enable the SPI interface.
4. Exit and reboot if prompted:
   ```bash
   sudo reboot
   ```

Verify SPI is enabled:
```bash
ls /dev/spidev0.*
# Expected output: /dev/spidev0.0  /dev/spidev0.1
```

### 2. Install Python Dependencies
```bash
sudo apt update
sudo apt install -y python3-pip python3-spidev
pip3 install -r requirements.txt
```

### 3. Verify Hardware Connection
Run the status command to test communication with the CC1101:
```bash
python3 garage_door.py status
```
If properly wired, you will see:
```text
============================================================
 Garage Door Opener: Security+ 1.0 (Purple Button 315MHz)
============================================================
Remote Fixed ID   : 632995987 (0x25BAC093)
Rolling Counter   : 1000 (0x3E8)
...
CC1101 Hardware   : CONNECTED
Chip Version      : 0x14 (Expected 0x14 or 0x04 for CC1101)
Radio State       : 0x01 (0x01 = IDLE)
============================================================
```

---

## Localhost Web UI Dashboard

You can run the built-in mobile-responsive Web UI and REST API server locally on the Pi:

```bash
python3 garage_door.py web
# or: python3 web_server.py --port 8080
```

### Accessing the Web UI:
- **From the Raspberry Pi browser**: [http://localhost:8080](http://localhost:8080)
- **From your smartphone or laptop on the same Wi-Fi**: `http://<pi-ip-address>:8080` (e.g. `http://192.168.1.150:8080`)

### Web UI Features:
- **One-Tap Door Toggle**: Large tactile button with haptic feedback, real-time RF transmission status, and rolling code display.
- **Pair with Purple Button Modal**: Interactive step-by-step guide to pair the virtual remote with your opener.
- **Live Stats Display**: Real-time monitor for Frequency (315.00 MHz), Remote Fixed ID, and Rolling Code Counter.
- **Live Activity Log**: Feed tracking every door toggle, pairing event, and manual sync with exact timestamps.
- **Manual Counter Resync**: Quickly adjust the counter if ever desynchronized.
- **RF Diagnostic Tool**: One-click test burst transmitter to verify signal on an SDR or RF meter.

### Run Web UI Automatically on Boot (Optional systemd service)
To have the web interface start automatically whenever your Raspberry Pi turns on:
```bash
sudo tee /etc/systemd/system/garage-door.service > /dev/null <<EOF
[Unit]
Description=Chamberlain 315MHz Garage Door Opener Web Server
After=network.target

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Garage-Door-Opener-master
ExecStart=/usr/bin/python3 /home/pi/Garage-Door-Opener-master/web_server.py --port 8080
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable garage-door.service
sudo systemctl start garage-door.service
```

---

## Pairing with your Garage Door Opener

To let your opener recognize the Raspberry Pi as an authorized remote:

1. **Locate the Learn Button:**
   - Go to your garage door opener ceiling unit.
   - Look for the square or circular **PURPLE** button labeled "Learn" (usually next to the antenna wire and indicator LED).
2. **Activate Learn Mode:**
   - Press and immediately release the purple learn button.
   - The indicator LED next to the button will illuminate **steady solid ON** for approximately 30 seconds.
3. **Transmit the Pairing Command:**
   - On the Raspberry Pi, execute:
     ```bash
     python3 garage_door.py pair
     ```
   - The script will transmit repeated Security+ bursts at 315.00 MHz.
4. **Confirm Pairing:**
   - When the opener accepts the remote, **the overhead work light bulbs will click or flash twice**.
   - If light bulbs are not installed, you will hear two distinct audible relay clicks inside the unit.

---

## Daily Usage & Commands

### Open or Close the Garage Door (Toggle)
```bash
python3 garage_door.py toggle
# (Aliases 'open' and 'close' work identically)
python3 garage_door.py open
```
This increments the rolling code counter and transmits 4 bursts of the rolling code packet at 315.00 MHz.

### Check Current Remote & Hardware Status
```bash
python3 garage_door.py status
```

### Manually Adjust Rolling Code Counter
If the counter desynchronizes (e.g., if commands were sent while the Pi was out of range):
```bash
python3 garage_door.py set-counter 1050
```

### Generate a Brand New Remote ID
If you want to create a new virtual remote identity (requires re-pairing with the purple button):
```bash
python3 garage_door.py reset-remote
python3 garage_door.py pair
```

### RF Output Diagnostic Test
Transmit continuous test signals for 5 seconds to verify signal presence on an SDR (e.g., RTL-SDR, GQRX, SDR#) tuned to 315.00 MHz:
```bash
python3 garage_door.py test-rf -d 5.0
```

---

## How It Works (Security+ 1.0 & Rolling Codes)

1. **Carrier & Modulation:**
   - Transmits at `315.00 MHz` using Amplitude-Shift Keying / On-Off Keying (`ASK/OOK`).
   - Bit duration is `500 µs` (2000 Baud).
2. **Ternary Symbols:**
   - Security+ 1.0 encodes data into ternary symbols (trits: 0, 1, 2) of 2.0 ms duration:
     - `Symbol 0`: 500 µs ON, 1500 µs OFF
     - `Symbol 1`: 1000 µs ON, 1000 µs OFF
     - `Symbol 2`: 1500 µs ON, 500 µs OFF
3. **Two-Frame Structure:**
   - Every press generates a 40-symbol payload split across two frames:
     - **Frame 1**: Sync Symbol `0` + 20 symbols + 58 ms pause.
     - **Frame 2**: Sync Symbol `2` + 20 symbols + 58 ms pause.
   - Each frame burst is 50 bytes and fits within the CC1101 64-byte hardware TX FIFO.
4. **Rolling Code Synchronization:**
   - The rolling code increments every press. The opener maintains an acceptance window. If you press the remote while out of range fewer than ~256 times, the opener resynchronizes automatically on the next press.

---

## Troubleshooting & Range Optimization

### 1. Module Antenna Tuning (315 MHz vs 433 MHz)
Most cheap CC1101 modules bought online are assembled with inductor/capacitor matching networks tuned for **433 MHz**. While the CC1101 silicon operates down to 300 MHz, range may be limited to a few feet if using a 433 MHz helical spring antenna.
* **Fix for long range (50–100+ feet):**
  - Solder a straight solid copper wire cut to a quarter-wave length for 315 MHz:
    $$\lambda / 4 \approx \frac{300}{315 \times 4} \approx 0.238\text{ m} \approx 23.8\text{ cm (9.37 inches)}$$
  - Or use a dedicated 315 MHz SMA antenna.

### 2. "Could not open hardware SPI"
- Check that SPI is enabled: `ls -l /dev/spidev0.0`
- Make sure your user is in the `spi` and `gpio` groups:
  ```bash
  sudo usermod -a -G spi,gpio $USER
  ```

### 3. Garage Door Not Responding
- Run `python3 garage_door.py pair` again while standing close to the ceiling unit.
- Ensure the purple button was pressed and the LED was illuminated during the pairing transmission.
- Check battery / power supply to your Raspberry Pi (a weak power supply may cause brownouts when transmitting RF).
