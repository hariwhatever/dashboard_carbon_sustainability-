/*
  Retail Energy Monitor - ESP32 node
  Reads up to 3 PZEM-004T v3 modules (voltage, current, power, energy kWh, PF, frequency),
  timestamps with a DS3231 RTC (NTP fallback), buffers readings in flash (LittleFS) when Wi-Fi is down,
  and uploads batches to the server over HTTP(S).

  Libraries (Library Manager): PZEM004Tv30 (mandl1), RTClib (Adafruit), ArduinoJson NOT needed.
  Board: "ESP32 Dev Module". Partition: default (LittleFS available).

  SIMULATE 1 = no sensors needed: generates realistic readings so you can test Wi-Fi, buffering and the server.
  SIMULATE 0 = real PZEM modules.
*/
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <LittleFS.h>
#include <Wire.h>
#include <RTClib.h>
#include <PZEM004Tv30.h>

// ================= CONFIG =================
#define SIMULATE     1                       // 1 = fake readings, 0 = real PZEM
#define SET_ADDRESS  0                       // 1 = one-time helper to give a single PZEM a new address (see README)
#define NEW_ADDR     0x02
const char* WIFI_SSID  = "YOUR_WIFI";
const char* WIFI_PASS  = "YOUR_PASSWORD";
const char* SERVER_URL = "http://192.168.1.50:8000/ingest";   // your PC's LAN IP; use https://... for a cloud server
const char* API_KEY    = "demo-key";
const char* NODE_ID    = "store1";
const int   NUM_CH     = 3;                  // circuits (or phases) monitored
const uint8_t PZEM_ADDR[NUM_CH] = {0x01, 0x02, 0x03};
#define PZEM_SERIAL  Serial2
#define PZEM_RX      16                      // ESP32 RX <- PZEM TX (via level shifter / divider, 5V -> 3.3V)
#define PZEM_TX      17                      // ESP32 TX -> PZEM RX
const uint32_t SAMPLE_MS = 10000;            // read meters every 10 s
const uint32_t SEND_MS   = 60000;            // store/send one averaged reading per circuit every 60 s
const size_t   MAX_BUF   = 600000;           // flash buffer cap (~30 h of 3 circuits)
const int      BATCH     = 120;              // readings per upload
const int32_t  IST_OFFSET = 19800;           // only used by the simulator's daily profile
// ==========================================

#define BUF_FILE "/buf.jsonl"
#define TMP_FILE "/tmp.jsonl"
RTC_DS3231 rtc; bool rtcOk = false;
PZEM004Tv30 pz[NUM_CH];
struct Ch { float v, i, p, pf, f, e, pSum; int n; bool ok; } ch[NUM_CH];
uint32_t tSample = 0, tSend = 0, tWifi = 0;

uint32_t nowTs() {                           // UTC epoch seconds, 0 if unknown
  if (rtcOk) { uint32_t t = rtc.now().unixtime(); if (t > 1700000000UL) return t; }
  time_t t = time(nullptr); return t > 1700000000L ? (uint32_t)t : 0;
}

void syncTime() {                            // NTP -> RTC
  configTime(0, 0, "pool.ntp.org", "time.google.com");
  for (int i = 0; i < 20 && time(nullptr) < 1700000000L; i++) delay(500);
  time_t t = time(nullptr);
  if (t > 1700000000L) { Serial.println("NTP ok"); if (rtcOk) rtc.adjust(DateTime((uint32_t)t)); }
}

void connectWifi(bool block) {
  WiFi.mode(WIFI_STA); WiFi.begin(WIFI_SSID, WIFI_PASS);
  if (!block) return;
  for (int i = 0; i < 30 && WiFi.status() != WL_CONNECTED; i++) { delay(500); Serial.print("."); }
  Serial.println(WiFi.status() == WL_CONNECTED ? "\nWiFi ok " + WiFi.localIP().toString() : "\nWiFi failed, running offline");
}

float simPower(int k, uint32_t ts) {         // fake store loads: lighting, AC, refrigeration
  float h = ((ts + IST_OFFSET) % 86400) / 3600.0f, r = 0.88f + random(0, 240) / 1000.0f;
  if (k == 0) return (h >= 8 && h < 22 ? 420 : 25) * r;
  if (k == 1) return (h >= 8 && h < 22 ? 975 : 0) * r;
  return 260 * r;
}

void sampleAll() {
  uint32_t ts = nowTs();
  for (int k = 0; k < NUM_CH; k++) {
    Ch &c = ch[k];
#if SIMULATE
    c.v = 226 + random(0, 80) / 10.0f; c.pf = k == 1 ? 0.88f : 0.95f; c.f = 50.0f;
    c.p = simPower(k, ts ? ts : millis() / 1000); c.i = c.p / (c.v * c.pf);
    c.e += c.p * (SAMPLE_MS / 3.6e6f); c.ok = true;
#else
    float v = pz[k].voltage();
    if (isnan(v)) { c.ok = false; Serial.printf("CH%d: no reply from PZEM 0x%02X\n", k + 1, PZEM_ADDR[k]); continue; }
    c.v = v; c.i = pz[k].current(); c.p = pz[k].power(); c.e = pz[k].energy();
    c.pf = pz[k].pf(); c.f = pz[k].frequency(); c.ok = !isnan(c.p) && !isnan(c.e);
    if (isnan(c.i)) c.i = 0; if (isnan(c.pf)) c.pf = 0; if (isnan(c.f)) c.f = 0;
#endif
    if (c.ok) { c.pSum += c.p; c.n++; }
    Serial.printf("CH%d  %.1fV  %.2fA  %.0fW  %.3fkWh\n", k + 1, c.v, c.i, c.p, c.e);
  }
}

void bufferAppend(const char* line) {
  File f = LittleFS.open(BUF_FILE, FILE_APPEND);
  if (!f) return;
  if (f.size() > MAX_BUF) { Serial.println("Buffer full: dropping newest reading"); f.close(); return; }
  f.println(line); f.close();
}

void storeReadings() {                       // one averaged reading per circuit -> flash buffer
  uint32_t ts = nowTs();
  if (!ts) { Serial.println("No valid time yet, skipping"); return; }
  char line[200];
  for (int k = 0; k < NUM_CH; k++) {
    Ch &c = ch[k]; if (!c.n) continue;
    snprintf(line, sizeof line, "{\"ts\":%lu,\"ch\":%d,\"v\":%.1f,\"i\":%.3f,\"p\":%.1f,\"e\":%.4f,\"pf\":%.2f,\"f\":%.1f}",
             (unsigned long)ts, k + 1, c.v, c.i, c.pSum / c.n, c.e, c.pf, c.f);
    bufferAppend(line); c.pSum = 0; c.n = 0;
  }
}

bool postJson(const String& body) {
  HTTPClient http; WiFiClientSecure sec; bool https = String(SERVER_URL).startsWith("https");
  if (https) { sec.setInsecure(); http.begin(sec, SERVER_URL); }   // TODO production: sec.setCACert(rootCA) instead of setInsecure()
  else http.begin(SERVER_URL);
  http.addHeader("Content-Type", "application/json"); http.addHeader("X-API-Key", API_KEY); http.setTimeout(10000);
  int code = http.POST(body); http.end();
  Serial.printf("POST -> %d\n", code);
  return code == 200;
}

void flushBuffer() {                         // upload oldest readings first; delete only after a 200 OK
  for (int round = 0; round < 10 && WiFi.status() == WL_CONNECTED; round++) {
    File f = LittleFS.open(BUF_FILE, "r");
    if (!f || f.size() == 0) { if (f) f.close(); return; }
    String body = String("{\"node\":\"") + NODE_ID + "\",\"readings\":["; int n = 0;
    while (f.available() && n < BATCH) {
      String l = f.readStringUntil('\n'); l.trim(); if (!l.length()) continue;
      if (n) body += ','; body += l; n++;
    }
    size_t pos = f.position(); f.close(); body += "]}";
    if (!n || !postJson(body)) return;       // keep data, retry next cycle
    File src = LittleFS.open(BUF_FILE, "r"), dst = LittleFS.open(TMP_FILE, "w");
    src.seek(pos); uint8_t b[256]; int r;
    while ((r = src.read(b, sizeof b)) > 0) dst.write(b, r);
    src.close(); dst.close(); LittleFS.remove(BUF_FILE); LittleFS.rename(TMP_FILE, BUF_FILE);
  }
}

void setup() {
  Serial.begin(115200); delay(500);
  if (!LittleFS.begin(true)) Serial.println("LittleFS failed");
  Wire.begin(); rtcOk = rtc.begin(); Serial.println(rtcOk ? "RTC ok" : "RTC not found (using NTP time only)");
#if SET_ADDRESS
  PZEM004Tv30 one(PZEM_SERIAL, PZEM_RX, PZEM_TX);            // connect ONE module only, default address 0xF8
  Serial.println(one.setAddress(NEW_ADDR) ? "Address set. Label this module, set SET_ADDRESS 0." : "Failed to set address");
  while (true) delay(1000);
#endif
#if !SIMULATE
  for (int k = 0; k < NUM_CH; k++) pz[k] = PZEM004Tv30(PZEM_SERIAL, PZEM_RX, PZEM_TX, PZEM_ADDR[k]);
#endif
  connectWifi(true); if (WiFi.status() == WL_CONNECTED) syncTime();
}

void loop() {
  uint32_t ms = millis();
  if (WiFi.status() != WL_CONNECTED && ms - tWifi > 15000) { tWifi = ms; WiFi.disconnect(); WiFi.begin(WIFI_SSID, WIFI_PASS); }
  if (ms - tSample >= SAMPLE_MS) { tSample = ms; sampleAll(); }
  if (ms - tSend >= SEND_MS) {
    tSend = ms; storeReadings();
    if (!nowTs() && WiFi.status() == WL_CONNECTED) syncTime();
    flushBuffer();
  }
}
