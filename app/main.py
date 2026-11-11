from __future__ import annotations
import json, os, sqlite3, urllib.parse, urllib.request, xml.etree.ElementTree as ET, hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator

DATA = Path(os.getenv("DATA_DIR", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "scanner.sqlite3"
app = FastAPI(title="Asian Money Scanner", version="0.5")
TAIL_RSS = os.getenv("TAILBET_RSS_URL", "https://tail.bet/feed.xml")
DEFAULT = {"min_stake_eur":1000,"min_bet_share_pct":20,"min_market_share_pct":85,"min_market_pool_eur":5000,"region":"all","market":"all"}

class Bet(BaseModel):
    source: str = Field(min_length=2, max_length=100)
    source_type: str
    pool_complete: bool = False
    currency: str = Field(min_length=3, max_length=3)
    event: str = Field(min_length=3, max_length=200)
    competition: Optional[str] = None
    region: Optional[str] = None
    market: str = Field(min_length=2, max_length=120)
    selection: str = Field(min_length=1, max_length=120)
    opposing_selection: Optional[str] = None
    accepted_at: datetime
    source_bet_id: str = Field(min_length=1, max_length=200)
    stake: float = Field(gt=0)
    odds: Optional[float] = Field(default=None, gt=1)
    market_pool: Optional[float] = Field(default=None, ge=0)
    selection_pool: Optional[float] = Field(default=None, ge=0)
    coverage_description: Optional[str] = None
    @field_validator("source_type")
    @classmethod
    def only_accepted(cls,v):
        if v != "bookmaker_accepted_bets": raise ValueError("Only validated accepted bookmaker bets are eligible.")
        return v
    @field_validator("currency")
    @classmethod
    def upper_currency(cls,v): return v.upper()

def db():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
    with db() as c:
        c.execute("CREATE TABLE IF NOT EXISTS bets(id INTEGER PRIMARY KEY, source TEXT, bet_id TEXT, payload TEXT, created TEXT, UNIQUE(source,bet_id))")
        c.execute("CREATE TABLE IF NOT EXISTS public_observations(id TEXT PRIMARY KEY, source TEXT, title TEXT, link TEXT, published TEXT, summary TEXT, fetched TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS config(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT)")
        c.execute("INSERT OR IGNORE INTO config VALUES(1,?)",(json.dumps(DEFAULT),))
init()

def cfg():
    with db() as c: return json.loads(c.execute("SELECT payload FROM config WHERE id=1").fetchone()["payload"])

def assess(b, c):
    reasons=[]
    if b.currency!="EUR": reasons.append("Only EUR thresholds are currently evaluated.")
    elif b.stake<c["min_stake_eur"]: reasons.append("Stake below minimum.")
    share=side=None
    if b.pool_complete and b.market_pool and b.market_pool>0 and b.selection_pool is not None:
        share=b.stake/b.market_pool*100; side=b.selection_pool/b.market_pool*100
    else: reasons.append("Market pool completeness not verified; percentage alerts blocked.")
    if share is not None and share<c["min_bet_share_pct"]: reasons.append("Single bet share below threshold.")
    if side is not None and side<c["min_market_share_pct"]: reasons.append("Selection share below threshold.")
    if b.currency!="EUR" or b.market_pool is None or b.market_pool<c["min_market_pool_eur"]: reasons.append("Market pool unavailable or below threshold.")
    if not b.coverage_description: reasons.append("Source coverage description missing.")
    if c["region"]!="all" and (not b.region or c["region"].lower() not in b.region.lower()): reasons.append("Region filter mismatch.")
    if c["market"]!="all" and c["market"].lower() not in b.market.lower(): reasons.append("Market filter mismatch.")
    return {"qualified":not reasons,"bet_share_pct":round(share,2) if share is not None else None,"market_share_pct":round(side,2) if side is not None else None,"reasons":reasons}

def notify(msg):
    token=os.getenv("TELEGRAM_BOT_TOKEN"); chat=os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat: return False,"Telegram not configured"
    data=urllib.parse.urlencode({"chat_id":chat,"text":msg}).encode()
    try:
        urllib.request.urlopen("https://api.telegram.org/bot"+token+"/sendMessage",data=data,timeout=8).read()
        return True,"sent"
    except Exception as e: return False,str(e)


@app.on_event("startup")
def telegram_startup_test():
    # Send one real connection test after the user explicitly requested it.
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS telegram_test (id INTEGER PRIMARY KEY CHECK(id=1), sent_at TEXT)")
        row = conn.execute("SELECT sent_at FROM telegram_test WHERE id=1").fetchone()
    if row:
        return
    sent, status = notify("Asian Money Scanner: Telegram connection test successful. Bot is connected.")
    if sent:
        with db() as conn:
            conn.execute("INSERT OR IGNORE INTO telegram_test(id, sent_at) VALUES(1, ?)", (datetime.now(timezone.utc).isoformat(),))
    else:
        print("TELEGRAM_STARTUP_TEST_FAILED: " + status)



def refresh_tailbet():
    req = urllib.request.Request(TAIL_RSS, headers={"User-Agent":"AsianMoneyScanner/0.5 (+public RSS reader)"})
    with urllib.request.urlopen(req, timeout=15) as response:
        raw = response.read(2_000_000)
    root = ET.fromstring(raw)
    entries = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
    added = 0
    fetched = datetime.now(timezone.utc).isoformat()
    def val(node, tag):
        el = node.find(tag)
        if el is None:
            return ""
        return (el.text or el.attrib.get("href") or "").strip()
    with db() as conn:
        for item in entries[:100]:
            title = val(item, "title") or val(item, "{http://www.w3.org/2005/Atom}title")
            link = val(item, "link") or val(item, "{http://www.w3.org/2005/Atom}link")
            if not link:
                link_el = item.find("{http://www.w3.org/2005/Atom}link")
                link = link_el.attrib.get("href", "") if link_el is not None else ""
            guid = val(item, "guid") or val(item, "{http://www.w3.org/2005/Atom}id") or link or title
            published = val(item, "pubDate") or val(item, "{http://www.w3.org/2005/Atom}published") or val(item, "{http://www.w3.org/2005/Atom}updated")
            summary = val(item, "description") or val(item, "{http://www.w3.org/2005/Atom}summary") or val(item, "{http://www.w3.org/2005/Atom}content")
            if not title:
                continue
            ident = hashlib.sha256(guid.encode("utf-8")).hexdigest()
            cur = conn.execute("INSERT OR IGNORE INTO public_observations(id,source,title,link,published,summary,fetched) VALUES(?,?,?,?,?,?,?)",
                (ident,"tail.bet / Stake public high-roller feed",title,link,published,summary[:4000],fetched))
            added += cur.rowcount
    return {"source":"tail.bet / Stake public high-roller feed","fetched_entries":len(entries),"new_entries":added,"fetched_at":fetched,"classification":"public high-roller observations; not Asian bookmaker market totals"}

@app.post("/api/observations/tailbet/refresh")
def refresh_tailbet_endpoint():
    try:
        return refresh_tailbet()
    except Exception as exc:
        raise HTTPException(502, "tail.bet RSS fetch failed: " + str(exc)[:300])

@app.get("/api/observations")
def public_observations(limit: int = 50):
    limit = max(1, min(limit, 100))
    with db() as conn:
        rows = conn.execute("SELECT source,title,link,published,summary,fetched FROM public_observations ORDER BY fetched DESC LIMIT ?", (limit,)).fetchall()
    return {"classification":"Public observations only; excluded from accepted-bet market totals and percentage alerts.","items":[dict(r) for r in rows]}


@app.get("/",response_class=HTMLResponse)
def home(): return HTMLResponse(Path("app/index.html").read_text())

@app.get("/api/health")
def health():
    with db() as c: n=c.execute("SELECT COUNT(*) FROM bets").fetchone()[0]
    return {"status":"ok","version":"0.5","connected_live_sources":0,"telegram_configured":bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),"validated_bet_records":n,"message":"No full Asian bookmaker accepted-bets feed is connected."}

@app.get("/api/config")
def get_config(): return cfg()

@app.post("/api/config")
def set_config(p:dict):
    c=cfg()
    for k in DEFAULT:
        if k in p: c[k]=p[k]
    if c["min_stake_eur"]<0 or not 0<=c["min_bet_share_pct"]<=100 or not 50<=c["min_market_share_pct"]<=100 or c["min_market_pool_eur"]<0: raise HTTPException(400,"Invalid thresholds")
    with db() as x: x.execute("UPDATE config SET payload=? WHERE id=1",(json.dumps(c),))
    return c

@app.post("/api/ingest")
def ingest(b:Bet):
    result=assess(b,cfg())
    try:
        with db() as c: c.execute("INSERT INTO bets(source,bet_id,payload,created) VALUES(?,?,?,?)",(b.source,b.source_bet_id,b.model_dump_json(),datetime.now(timezone.utc).isoformat()))
    except sqlite3.IntegrityError: raise HTTPException(409,"Duplicate source bet ID")
    sent=False; status="Blocked by validation/threshold rules"
    if result["qualified"]:
        sent,status=notify(f"VALIDATED ACCEPTED BET\n{b.event}\n{b.market} — {b.selection}\n{b.stake} {b.currency}\nBet share: {result['bet_share_pct']}%\nSelection share: {result['market_share_pct']}%\nSource: {b.source}\nAccepted: {b.accepted_at.isoformat()}")
    return {"recorded":True,"assessment":result,"telegram_sent":sent,"telegram_status":status}

@app.get("/api/records")
def records():
    with db() as c: rows=c.execute("SELECT payload,created FROM bets ORDER BY id DESC LIMIT 100").fetchall()
    return [{"record":json.loads(x["payload"]),"received_at":x["created"],"assessment":assess(Bet(**json.loads(x["payload"])),cfg())} for x in rows]

@app.get("/api/metrics/volume")
def volume(window_seconds: int = 60):
    if window_seconds < 1 or window_seconds > 3600:
        raise HTTPException(400, "window_seconds must be between 1 and 3600")
    now = datetime.now(timezone.utc)
    current_start = datetime.fromtimestamp(now.timestamp() - window_seconds, timezone.utc).isoformat()
    previous_start = datetime.fromtimestamp(now.timestamp() - 2 * window_seconds, timezone.utc).isoformat()
    with db() as c:
        current = c.execute("SELECT payload FROM bets WHERE created >= ?", (current_start,)).fetchall()
        previous = c.execute("SELECT payload FROM bets WHERE created >= ? AND created < ?", (previous_start, current_start)).fetchall()
    def aggregate(rows):
        totals = {}
        for row in rows:
            record = json.loads(row["payload"])
            currency = record.get("currency", "UNKNOWN")
            bucket = totals.setdefault(currency, {"currency": currency, "stake_volume": 0.0, "bets": 0})
            bucket["stake_volume"] += float(record.get("stake", 0))
            bucket["bets"] += 1
        return {k: {"currency": v["currency"], "stake_volume": round(v["stake_volume"], 2), "bets": v["bets"]} for k, v in totals.items()}
    cur, prev = aggregate(current), aggregate(previous)
    comparison = []
    for currency in sorted(set(cur) | set(prev)):
        a = cur.get(currency, {"stake_volume": 0.0, "bets": 0})
        b = prev.get(currency, {"stake_volume": 0.0, "bets": 0})
        delta = round(a["stake_volume"] - b["stake_volume"], 2)
        pct = round((delta / b["stake_volume"]) * 100, 2) if b["stake_volume"] else None
        comparison.append({"currency": currency, "current_volume": a["stake_volume"], "previous_volume": b["stake_volume"], "delta": delta, "change_pct": pct, "current_bets": a["bets"], "previous_bets": b["bets"]})
    return {
        "window_seconds": window_seconds,
        "basis": "records_received_by_scanner",
        "definition": "Unique accepted-bet records received during the current rolling window compared with the immediately preceding equal-length window; currencies kept separate.",
        "bets_count": len(current),
        "by_currency": sorted(cur.values(), key=lambda x: x["currency"]),
        "previous_bets_count": len(previous),
        "comparison": comparison,
        "as_of": now.isoformat()
    }
