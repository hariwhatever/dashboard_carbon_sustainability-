"""Retail energy monitor backend: receives ESP32 readings, stores in SQLite, serves dashboard + API."""
import csv, io, os, sqlite3, time
from datetime import datetime, timezone
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.environ.get("API_KEY", "demo-key")
db = sqlite3.connect(os.environ.get("DB", os.path.join(HERE, "energy.db")), check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
create table if not exists readings(node text, ts int, ch int, v real, i real, p real, e real, pf real, f real, primary key(node, ts, ch));
create table if not exists interventions(id integer primary key, ts int, name text, note text);
create table if not exists settings(k text primary key, v text);""")
DEFAULTS = {"tariff": "8.0", "factor": "0.71", "tz": "19800"}  # Rs/kWh, kgCO2e/kWh (ILLUSTRATIVE: use latest CEA), IST offset
CH_NAMES = {1: "Lighting", 2: "Air conditioning", 3: "Refrigeration"}
app = FastAPI(title="Retail Energy Monitor")

def cfg():
    s = dict(DEFAULTS); s.update({r["k"]: r["v"] for r in db.execute("select k,v from settings")})
    return {"tariff": float(s["tariff"]), "factor": float(s["factor"]), "tz": int(s["tz"])}

def need_key(k):
    if k != KEY: raise HTTPException(401, "bad api key")

class R(BaseModel):
    ts: int; ch: int; v: float = 0; i: float = 0; p: float = 0; e: float = 0; pf: float = 0; f: float = 0
class Batch(BaseModel):
    node: str = "store1"; readings: list[R]
class Interv(BaseModel):
    name: str; note: str = ""; ts: int | None = None
class Settings(BaseModel):
    tariff: float | None = None; factor: float | None = None

@app.post("/ingest")
def ingest(b: Batch, x_api_key: str = Header(default="")):
    need_key(x_api_key)
    db.executemany("insert or ignore into readings values(?,?,?,?,?,?,?,?,?)",
                   [(b.node, r.ts, r.ch, r.v, r.i, r.p, r.e, r.pf, r.f) for r in b.readings])
    db.commit()
    return {"stored": len(b.readings)}

@app.post("/api/intervention")
def add_interv(i: Interv):
    db.execute("insert into interventions(ts,name,note) values(?,?,?)", (i.ts or int(time.time()), i.name, i.note)); db.commit(); return {"ok": True}

@app.delete("/api/intervention/{iid}")
def del_interv(iid: int):
    db.execute("delete from interventions where id=?", (iid,)); db.commit(); return {"ok": True}

@app.post("/api/settings")
def set_settings(s: Settings):
    for k, v in s.model_dump().items():
        if v is not None: db.execute("insert or replace into settings values(?,?)", (k, str(v)))
    db.commit(); return cfg()

@app.post("/api/reset")
def reset(x_api_key: str = Header(default="")):
    need_key(x_api_key); db.executescript("delete from readings; delete from interventions;"); return {"ok": True}

def daily_energy(tz):
    """kWh per local day per channel, from the meter's cumulative kWh counter (reset-safe)."""
    days, last = {}, {}
    for r in db.execute("select ts,ch,e from readings order by ch,ts"):
        ch, e = r["ch"], r["e"]
        if ch in last:
            de = e - last[ch]
            if de < 0: de = 0  # meter reset
            d = (r["ts"] + tz) // 86400
            days.setdefault(d, {"kwh": {}, "first": r["ts"], "last": r["ts"]})
            x = days[d]; x["kwh"][ch] = x["kwh"].get(ch, 0) + de
            x["first"] = min(x["first"], r["ts"]); x["last"] = max(x["last"], r["ts"])
        last[ch] = e
    return days

def day_str(d): return datetime.fromtimestamp(d * 86400, timezone.utc).strftime("%Y-%m-%d")

@app.get("/api/summary")
def summary():
    c, now = cfg(), int(time.time()); tz = c["tz"]
    days = daily_energy(tz); today = (now + tz) // 86400
    daily = []
    for d in sorted(days)[-30:]:
        x = days[d]; tot = sum(x["kwh"].values())
        full = (x["last"] - x["first"]) >= 20 * 3600 and d < today
        daily.append({"day": day_str(d), "d": d, "kwh": tot, "ch": {str(k): v for k, v in x["kwh"].items()}, "co2": tot * c["factor"], "cost": tot * c["tariff"], "complete": full})
    live = [dict(r) for r in db.execute("select * from readings where ts=(select max(ts) from readings) order by ch")]
    last_seen = live[0]["ts"] if live else None
    series = [dict(r) for r in db.execute("select ts,ch,p from readings where ts>? order by ts", (now - 7200,))]
    hourly = [dict(r) for r in db.execute("select ((ts+?)/3600)%24 h, sum(p)*1.0/count(distinct ts) w from readings where ts>? group by h order by h", (tz, now - 7 * 86400))]
    ivs = [dict(r) for r in db.execute("select * from interventions order by ts")]
    cmp_ = None
    if ivs:
        t = ivs[0]["ts"]
        base = [x for x in daily if x["complete"] and (x["d"] + 1) * 86400 - tz <= t]
        cur = [x for x in daily if x["complete"] and x["d"] * 86400 - tz >= t]
        if base and cur:
            avg = lambda L, k: sum(x[k] for x in L) / len(L)
            b, u = avg(base, "kwh"), avg(cur, "kwh")
            cmp_ = {"base_days": len(base), "cur_days": len(cur), "base_kwh": b, "cur_kwh": u, "saved_kwh": b - u,
                    "pct": (b - u) / b * 100 if b else 0, "saved_co2": (b - u) * c["factor"], "saved_cost": (b - u) * c["tariff"],
                    "month_cost": (b - u) * c["tariff"] * 30, "month_co2": (b - u) * c["factor"] * 30,
                    "ch": {str(k): {"base": sum(x["ch"].get(str(k), 0) for x in base) / len(base), "cur": sum(x["ch"].get(str(k), 0) for x in cur) / len(cur)} for k in CH_NAMES}}
    td = next((x for x in daily if x["d"] == today), None)
    return {"cfg": c, "names": CH_NAMES, "now": now, "last_seen": last_seen, "online": bool(last_seen and now - last_seen < 180),
            "live": live, "series": series, "hourly": hourly, "daily": daily, "today": td, "interventions": ivs, "compare": cmp_}

@app.get("/api/daily.csv")
def daily_csv():
    c = cfg(); out = io.StringIO(); w = csv.writer(out)
    w.writerow(["date", "kWh", "tCO2e_kg", "cost_rs"] + [CH_NAMES[k] + "_kWh" for k in CH_NAMES])
    for d, x in sorted(daily_energy(c["tz"]).items()):
        t = sum(x["kwh"].values()); w.writerow([day_str(d), round(t, 3), round(t * c["factor"], 3), round(t * c["tariff"], 2)] + [round(x["kwh"].get(k, 0), 3) for k in CH_NAMES])
    out.seek(0); return StreamingResponse(iter([out.getvalue()]), media_type="text/csv", headers={"Content-Disposition": "attachment; filename=daily_energy.csv"})

@app.get("/")
def index(): return FileResponse(os.path.join(HERE, "static", "index.html"))
