# Retail Energy & Carbon Monitor - run guide

## A. Run the whole system with NO hardware (5 minutes)
1. Install Python 3.10+.
2. `cd server` then `pip install -r requirements.txt` (also `pip install requests` for the simulator)
3. Start the server: `uvicorn app:app --host 0.0.0.0 --port 8000`
4. In a second terminal: `cd simulator` then `python sim_node.py --backfill 10 --reset`
   (creates 7 "before" days + 3 "after" days with a logged intervention, then keeps sending live readings)
5. Open http://localhost:8000 - the dashboard shows live power, daily kWh, CO2e, cost and the baseline-vs-current reduction.

## B. Test the ESP32 firmware without sensors
1. Arduino IDE: install the ESP32 board package, and libraries "PZEM004Tv30" (mandl1) and "RTClib" (Adafruit).
2. Open firmware/energy_node/energy_node.ino. Set WIFI_SSID, WIFI_PASS and SERVER_URL (your PC's LAN IP, e.g. http://192.168.1.50:8000/ingest). Keep SIMULATE 1.
3. Upload. Serial Monitor (115200) shows readings; the dashboard shows the node online within about a minute.
4. Offline test: turn off the Wi-Fi/hotspot for a few minutes, then turn it back on. Buffered readings upload automatically and nothing is lost.
   (Allow your PC firewall to accept port 8000.)

## C. Real hardware
Wiring (power off while wiring; a licensed electrician must do all mains work and approve the enclosure):
- PZEM-004T: 5 V and GND from the supply; TX -> ESP32 GPIO16 (RX2) through a level shifter or resistor divider (5 V -> 3.3 V); RX <- ESP32 GPIO17 (TX2).
- Mains voltage terminals of each PZEM on the monitored circuit, through a fuse/MCB. Split-core CT clamped around the live wire only (not live + neutral together).
- DS3231 RTC: SDA GPIO21, SCL GPIO22, 3.3 V, GND.
Steps:
1. Give each PZEM a unique address: connect ONE module, set SET_ADDRESS 1 and NEW_ADDR 0x02, upload, read the Serial Monitor, label the module, repeat with 0x03. The first module can stay at its default; if it does not answer at 0x01, set it too. Then set SET_ADDRESS 0.
2. Set SIMULATE 0, upload, check the Serial Monitor. If several modules on one serial line misbehave, use a second hardware serial port for one of them.
3. Validate against your reference meter on a steady load and record the % difference.
4. Reset a PZEM's energy counter only if needed; the server handles counter resets.

## D. Before real use
- Replace the illustrative grid factor (0.71) with the latest CEA value (dashboard Settings) and enter the real tariff.
- Change API_KEY (server env var API_KEY and the firmware constant).
- For a cloud server use HTTPS, and replace setInsecure() with the server's root certificate.
- The dashboard loads Chart.js from a CDN, so the viewing browser needs internet.
- Results are indicative, not certified measurement.
