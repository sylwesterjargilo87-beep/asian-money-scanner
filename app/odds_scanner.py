from __future__ import annotations
import gzip, json, os, sqlite3, urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB = Path(os.getenv("DATA_DIR", "/data")) / "scanner.sqlite3"
URL = os.getenv("MOVIODDS_URL", "https://moviodds.com/odds?sport=soccer")
SOURCE = "MoviOdds (bet365 odds feed)"
MARKETS = ("asian handicap", "handicap", "over/under", "over under", "goal line", "totals")

def connect():
    c=sqlite3.connect(DB); c.row_factory=sqlite3.Row; return c

def init():
    with connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS odds_current(
          fixture_id TEXT, group_id TEXT, sel_id TEXT, event TEXT, league TEXT,
          kickoff TEXT, in_play INTEGER, market TEXT, side TEXT, line TEXT,
          label TEXT, odds REAL, polled_at TEXT, changed_at TEXT,
          PRIMARY KEY(fixture_id,group_id,sel_id))""")
        c.execute("""CREATE TABLE IF NOT EXISTS odds_alerts(
          id INTEGER PRIMARY KEY AUTOINCREMENT, fixture_id TEXT, group_id TEXT,
          sel_id TEXT, event TEXT, league TEXT, market TEXT, side TEXT, line TEXT,
          old_odds REAL, new_odds REAL, move_pct REAL, created TEXT)""")
        c.execute("CREATE TABLE IF NOT EXISTS odds_scanner_state(id INTEGER PRIMARY KEY CHECK(id=1), last_poll TEXT, last_ok TEXT, matches INTEGER DEFAULT 0, quotes INTEGER DEFAULT 0, error TEXT)")
        c.execute("INSERT OR IGNORE INTO odds_scanner_state(id) VALUES(1)")

def is_target(q):
    s=" ".join(str(q.get(k,"")) for k in ("group_name","market","line","label","name")).lower()
    return any(x in s for x in MARKETS)

def poll(send_alert):
    init()
    req=urllib.request.Request(URL, headers={"User-Agent":"AsianMoneyScanner/1.0","Accept":"application/json","Accept-Encoding":"gzip"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            raw=r.read()
            if r.headers.get("Content-Encoding","").lower()=="gzip": raw=gzip.decompress(raw)
        data=json.loads(raw.decode("utf-8"))
        matches=data.get("matches",[]) if isinstance(data,dict) else []
        now=datetime.now(timezone.utc)
        stamp=now.isoformat()
        seen=0; quote_count=0; alerts=[]
        with connect() as c:
            for m in matches:
                fixture=str(m.get("id") or "")
                if not fixture: continue
                event=(str(m.get("home",""))+" vs "+str(m.get("away",""))).strip()
                for q in m.get("quotes",[]):
                    if not is_target(q): continue
                    price=q.get("odds_dec")
                    try: price=float(price)
                    except (TypeError,ValueError): continue
                    if price<=1 or q.get("suspended"): continue
                    gid=str(q.get("group_id") or q.get("market_id") or q.get("group_name") or "market")
                    sid=str(q.get("sel_id") or (str(q.get("side"))+"|"+str(q.get("line"))+"|"+str(q.get("label"))))
                    old=c.execute("SELECT * FROM odds_current WHERE fixture_id=? AND group_id=? AND sel_id=?",(fixture,gid,sid)).fetchone()
                    market=str(q.get("group_name") or q.get("market") or "Asian Handicap / Totals")
                    side=str(q.get("side") or q.get("outcome") or q.get("label") or "")
                    line=str(q.get("line") or q.get("handicap") or q.get("label") or "")
                    previous=float(old["odds"]) if old else None
                    changed=old["changed_at"] if old else stamp
                    if previous is not None and abs(price-previous)>0.00001: changed=stamp
                    if previous and previous>1.01 and price<previous:
                        move=(price/previous-1)*100
                        try: last=datetime.fromisoformat(old["changed_at"])
                        except Exception: last=now-timedelta(days=1)
                        # A shortening of at least 7.5% over a short observation window is an odds signal, never proof of money accepted.
                        if move<=-7.5 and now-last<=timedelta(minutes=10):
                            recent=c.execute("SELECT created FROM odds_alerts WHERE fixture_id=? AND group_id=? AND sel_id=? ORDER BY id DESC LIMIT 1",(fixture,gid,sid)).fetchone()
                            if not recent or now-datetime.fromisoformat(recent["created"])>timedelta(minutes=30):
                                c.execute("""INSERT INTO odds_alerts(fixture_id,group_id,sel_id,event,league,market,side,line,old_odds,new_odds,move_pct,created)
                                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",(fixture,gid,sid,event,str(m.get("league","")),market,side,line,previous,price,round(move,2),stamp))
                                alerts.append({"event":event,"league":m.get("league",""),"market":market,"side":side,"line":line,"old_odds":previous,"new_odds":price,"move_pct":round(move,2),"created":stamp})
                    c.execute("""INSERT INTO odds_current(fixture_id,group_id,sel_id,event,league,kickoff,in_play,market,side,line,label,odds,polled_at,changed_at)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                      ON CONFLICT(fixture_id,group_id,sel_id) DO UPDATE SET event=excluded.event,league=excluded.league,kickoff=excluded.kickoff,in_play=excluded.in_play,market=excluded.market,side=excluded.side,line=excluded.line,label=excluded.label,odds=excluded.odds,polled_at=excluded.polled_at,changed_at=excluded.changed_at""",
                      (fixture,gid,sid,event,str(m.get("league","")),str(m.get("kickoff_utc","")),int(bool(m.get("in_play"))),market,side,line,str(q.get("label","")),price,stamp,changed))
                    quote_count+=1
                seen+=1
            c.execute("UPDATE odds_scanner_state SET last_poll=?,last_ok=?,matches=?,quotes=?,error=NULL WHERE id=1",(stamp,stamp,seen,quote_count))
        for a in alerts:
            msg=("RUCH KURSU — bet365 reference feed (nie potwierdzenie przyjętej stawki)\n"
                 f"{a['event']} · {a['league']}\n{a['market']} | {a['side']} | {a['line']}\n"
                 f"Kurs: {a['old_odds']:.3f} → {a['new_odds']:.3f} ({a['move_pct']}%)\n{a['created']}")
            send_alert(msg)
        return {"ok":True,"matches":seen,"quotes":quote_count,"alerts":len(alerts),"polled_at":stamp,"source":SOURCE}
    except Exception as exc:
        stamp=datetime.now(timezone.utc).isoformat()
        with connect() as c: c.execute("UPDATE odds_scanner_state SET last_poll=?,error=? WHERE id=1",(stamp,str(exc)[:500]))
        return {"ok":False,"error":str(exc)[:500],"polled_at":stamp,"source":SOURCE}

def status():
    init()
    with connect() as c:
        row=c.execute("SELECT * FROM odds_scanner_state WHERE id=1").fetchone()
        return {"provider":SOURCE,"source_scope":"bet365 soccer odds; Asian Handicap and Over/Under market quotes","classification":"Odds movements only. Not accepted sportsbook stakes, not Asian bookmaker-wide flow.","last_poll":row["last_poll"],"last_success":row["last_ok"],"matches":row["matches"],"quotes":row["quotes"],"error":row["error"],"configured":True}

def matches(limit=100):
    init()
    with connect() as c:
        rows=c.execute("""SELECT fixture_id,event,league,kickoff,in_play,market,side,line,label,odds,polled_at,changed_at
          FROM odds_current ORDER BY in_play DESC,kickoff ASC LIMIT ?""",(max(1,min(int(limit),300)),)).fetchall()
    return [dict(r) for r in rows]

def alerts(limit=50):
    init()
    with connect() as c:
        rows=c.execute("SELECT * FROM odds_alerts ORDER BY id DESC LIMIT ?",(max(1,min(int(limit),100)),)).fetchall()
    return [dict(r) for r in rows]
