"""Virtual ESP32 node: posts realistic 3-circuit store readings to the server (no hardware needed).
  python sim_node.py --backfill 10      # 7 days 'before' + 3 days 'after' energy-saving changes, then live
  python sim_node.py --live              # only live readings every --interval seconds"""
import argparse, random, time, requests
ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://localhost:8000"); ap.add_argument("--key", default="demo-key")
ap.add_argument("--backfill", type=int, default=0, help="days of history (intervention at day 7)")
ap.add_argument("--live", action="store_true"); ap.add_argument("--interval", type=int, default=5)
ap.add_argument("--reset", action="store_true"); ap.add_argument("--tz", type=int, default=19800)
a = ap.parse_args(); H = {"X-API-Key": a.key}

def power(ch, h, after):
    if ch == 1:  # lighting: old bulbs on 8-22; after: LED + off outside 9-21
        return (130 if 9 <= h < 21 else 0) if after else (420 if 8 <= h < 22 else 25)
    if ch == 2:  # AC: after: higher setpoint (lower duty) + off at 20:30
        return (1500 * .48 if 9 <= h < 20.5 else 0) if after else (1500 * .65 if 8 <= h < 22 else 0)
    return 260  # refrigeration (unchanged)

E = {1: 0.0, 2: 0.0, 3: 0.0}
def reading(ts, after, dt):
    h = ((ts + a.tz) % 86400) / 3600; out = []
    for ch in (1, 2, 3):
        p = max(0, power(ch, h, after) * random.uniform(.88, 1.12)); E[ch] += p * dt / 3.6e6
        v = random.uniform(226, 234); pf = .95 if ch != 2 else .88
        out.append({"ts": ts, "ch": ch, "v": round(v, 1), "i": round(p / (v * pf), 3), "p": round(p, 1), "e": round(E[ch], 4), "pf": pf, "f": 50.0})
    return out

def post(rs): requests.post(a.url + "/ingest", json={"node": "store1", "readings": rs}, headers=H, timeout=30).raise_for_status()

if a.reset: requests.post(a.url + "/api/reset", headers=H); print("data cleared")
after = False
if a.backfill:
    mid = (int(time.time()) + a.tz) // 86400 * 86400 - a.tz          # local midnight today
    start = mid - a.backfill * 86400; t_int = start + 7 * 86400; buf = []
    for ts in range(start, int(time.time()) - 60, 60):
        buf += reading(ts, ts >= t_int, 60)
        if len(buf) >= 3000: post(buf); buf = []
    if buf: post(buf)
    requests.post(a.url + "/api/intervention", json={"ts": t_int, "name": "LED lighting + AC schedule", "note": "Simulated: LEDs, AC setpoint raised, AC and lights off outside store hours"})
    after = True; print(f"backfilled {a.backfill} days; intervention logged at day 7")
if a.live or a.backfill:
    print("live mode, Ctrl+C to stop"); last = time.time()
    while True:
        time.sleep(a.interval); now = time.time(); post(reading(int(now), after, now - last)); last = now
