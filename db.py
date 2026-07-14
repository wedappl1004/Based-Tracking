"""
db.py — 데이터 계층 (Postgres 우선, 없으면 파일 폴백)
DATABASE_URL 환경변수가 있으면 Postgres, 없으면 로컬 JSON으로 동작.
batch 2 기능(성적표/홀더분포/자금줄/스마트머니)의 저장소.
"""
import json
import os
import time

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_conn = None
_PG = False

try:
    if DATABASE_URL:
        import psycopg2
        import psycopg2.extras
        _conn = psycopg2.connect(DATABASE_URL, sslmode="require"
                                 if "railway" in DATABASE_URL else "prefer")
        _conn.autocommit = True
        _PG = True
        print("[db] Postgres 연결됨")
except Exception as e:
    print(f"[db] Postgres 실패 → 파일 폴백: {e}")
    _PG = False

FALLBACK_DIR = "db_fallback"


def setup():
    """테이블 생성 (Postgres) / 폴더 준비 (파일)"""
    if _PG:
        cur = _conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id SERIAL PRIMARY KEY,
            ts BIGINT, kind TEXT, msg TEXT,
            price REAL,
            price_1h REAL, price_24h REAL,
            graded_1h BOOLEAN DEFAULT FALSE,
            graded_24h BOOLEAN DEFAULT FALSE
        );
        CREATE TABLE IF NOT EXISTS holders (
            ts BIGINT, contract TEXT, addr TEXT, balance REAL,
            PRIMARY KEY (ts, contract, addr)
        );
        CREATE TABLE IF NOT EXISTS holder_snap (
            ts BIGINT PRIMARY KEY, contract TEXT,
            top10_pct REAL, top50_pct REAL, n_holders INTEGER
        );
        CREATE TABLE IF NOT EXISTS wallet_pnl (
            addr TEXT PRIMARY KEY, contract TEXT,
            realized_score REAL, buys INTEGER, sells INTEGER,
            updated BIGINT
        );
        CREATE TABLE IF NOT EXISTS wallet_funding (
            addr TEXT PRIMARY KEY, funded_by TEXT, is_team BOOLEAN,
            ts BIGINT
        );
        """)
        print("[db] 테이블 준비 완료")
    else:
        os.makedirs(FALLBACK_DIR, exist_ok=True)


# ── 알림 로그 & 성적표 (#10) ──
def log_alert(kind, msg, price):
    ts = int(time.time())
    if _PG:
        _conn.cursor().execute(
            "INSERT INTO alerts (ts,kind,msg,price) VALUES (%s,%s,%s,%s)",
            (ts, kind, msg[:300], price))
    else:
        _append_file("alerts", {"ts": ts, "kind": kind, "msg": msg[:300],
                                "price": price, "price_1h": None,
                                "price_24h": None})


def grade_pending(current_price):
    """1h/24h 지난 알림에 결과 가격 기록"""
    now = int(time.time())
    graded = {"1h": 0, "24h": 0}
    if _PG:
        cur = _conn.cursor()
        cur.execute("UPDATE alerts SET price_1h=%s, graded_1h=TRUE "
                    "WHERE graded_1h=FALSE AND ts <= %s",
                    (current_price, now - 3600))
        graded["1h"] = cur.rowcount
        cur.execute("UPDATE alerts SET price_24h=%s, graded_24h=TRUE "
                    "WHERE graded_24h=FALSE AND ts <= %s",
                    (current_price, now - 86400))
        graded["24h"] = cur.rowcount
    else:
        rows = _read_file("alerts")
        for r in rows:
            if r["price_1h"] is None and r["ts"] <= now - 3600:
                r["price_1h"] = current_price
                graded["1h"] += 1
            if r["price_24h"] is None and r["ts"] <= now - 86400:
                r["price_24h"] = current_price
                graded["24h"] += 1
        _write_file("alerts", rows)
    return graded


def scorecard(days=30):
    """신호별 성적: 24h 방향 적중률 + baseline 대비 lift + 가중치 산출.
    baseline = 전체 알림의 평균 24h 변화(무작위 진입 근사)."""
    since = int(time.time()) - days * 86400
    if _PG:
        cur = _conn.cursor()
        cur.execute("""SELECT kind, price, price_24h FROM alerts
                       WHERE ts >= %s AND price_24h IS NOT NULL
                       AND price IS NOT NULL AND price > 0""", (since,))
        rows = cur.fetchall()
    else:
        rows = [(r["kind"], r["price"], r["price_24h"])
                for r in _read_file("alerts")
                if r["ts"] >= since and r.get("price_24h") and r.get("price")]
    if not rows:
        return {}

    # baseline: 전체 24h 변화 평균 (아무 때나 들어갔을 때의 기대값)
    all_chg = [(p24 / p0 - 1) * 100 for _, p0, p24 in rows]
    baseline = sum(all_chg) / len(all_chg)

    stats = {}
    for kind, p0, p24 in rows:
        chg = (p24 / p0 - 1) * 100
        d = stats.setdefault(kind, {"n": 0, "hit": 0, "sum": 0.0})
        d["n"] += 1
        d["sum"] += chg
        bullish = any(t in kind for t in ("pump", "accum", "squeeze", "long", "🚀", "🟢", "📡🚀"))
        bearish = any(t in kind for t in ("dump", "dist", "stop", "liq_long", "netflow", "📉", "🔴", "🚨", "📥"))
        if bullish and chg > 0:
            d["hit"] += 1
        elif bearish and chg < 0:
            d["hit"] += 1
        elif not bullish and not bearish and chg > 0:
            d["hit"] += 1

    # lift + 가중치 계산
    for kind, d in stats.items():
        avg = d["sum"] / d["n"]
        d["avg"] = avg
        d["acc"] = d["hit"] / d["n"] * 100
        # 방향 고려한 lift: 이 신호로 진입했을 때 baseline 대비 초과 성과
        bullish = any(t in kind for t in ("pump","accum","squeeze","long","🚀","🟢","📡🚀"))
        d["lift"] = (avg - baseline) if bullish else (baseline - avg)             if any(t in kind for t in ("dump","dist","stop","netflow","📉","🚨","📥")) else abs(avg - baseline)
        # 가중치: lift 양수 + 표본 충분하면 신뢰, 아니면 감쇠 (0.3~1.5)
        if d["n"] < 3:
            d["weight"] = 1.0  # 표본 부족 → 중립 유지
        elif d["lift"] > 0:
            d["weight"] = min(1.5, 1.0 + d["lift"] / 10)
        else:
            d["weight"] = max(0.3, 1.0 + d["lift"] / 10)
    stats["_baseline"] = {"avg": baseline, "n": len(rows)}
    return stats


def save_weights(stats):
    """산출된 가중치를 signal_weights 테이블/파일에 저장 → check.py가 로드"""
    weights = {k: round(v["weight"], 2) for k, v in stats.items()
               if k != "_baseline" and "weight" in v}
    if _PG:
        cur = _conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS signal_weights "
                    "(kind TEXT PRIMARY KEY, weight REAL, updated BIGINT)")
        for k, w in weights.items():
            cur.execute("INSERT INTO signal_weights VALUES (%s,%s,%s) "
                        "ON CONFLICT (kind) DO UPDATE SET weight=%s, updated=%s",
                        (k, w, int(time.time()), w, int(time.time())))
    else:
        _write_kv("signal_weights", weights)
    return weights


def load_weights():
    if _PG:
        try:
            cur = _conn.cursor()
            cur.execute("SELECT kind, weight FROM signal_weights")
            return {k: w for k, w in cur.fetchall()}
        except Exception:
            return {}
    else:
        return _read_kv("signal_weights")


# ── 홀더 분포 (#2) ──
def save_holder_snap(contract, top10_pct, top50_pct, n_holders):
    ts = int(time.time())
    if _PG:
        _conn.cursor().execute(
            "INSERT INTO holder_snap VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (ts) DO NOTHING",
            (ts, contract, top10_pct, top50_pct, n_holders))
    else:
        _append_file("holder_snap", {"ts": ts, "contract": contract,
                     "top10_pct": top10_pct, "top50_pct": top50_pct,
                     "n_holders": n_holders})


def prev_holder_snap(hours_ago=24):
    cutoff = int(time.time()) - hours_ago * 3600
    if _PG:
        cur = _conn.cursor()
        cur.execute("SELECT top10_pct,top50_pct,n_holders FROM holder_snap "
                    "WHERE ts <= %s ORDER BY ts DESC LIMIT 1", (cutoff,))
        r = cur.fetchone()
        return dict(zip(("top10_pct", "top50_pct", "n_holders"), r)) if r else None
    else:
        rows = [r for r in _read_file("holder_snap") if r["ts"] <= cutoff]
        return rows[-1] if rows else None


# ── 지갑 자금줄 (#3) ──
def set_wallet_funding(addr, funded_by, is_team):
    if _PG:
        _conn.cursor().execute(
            "INSERT INTO wallet_funding VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (addr) DO UPDATE SET funded_by=%s, is_team=%s",
            (addr, funded_by, is_team, int(time.time()), funded_by, is_team))
    else:
        d = _read_kv("wallet_funding")
        d[addr] = {"funded_by": funded_by, "is_team": is_team}
        _write_kv("wallet_funding", d)


def known_funding(addr):
    if _PG:
        cur = _conn.cursor()
        cur.execute("SELECT funded_by,is_team FROM wallet_funding WHERE addr=%s",
                    (addr,))
        r = cur.fetchone()
        return {"funded_by": r[0], "is_team": r[1]} if r else None
    else:
        return _read_kv("wallet_funding").get(addr)


# ── 스마트머니 PnL (#5) ──
def update_wallet_pnl(addr, contract, score, buys, sells):
    if _PG:
        _conn.cursor().execute(
            "INSERT INTO wallet_pnl VALUES (%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (addr) DO UPDATE SET realized_score=%s,buys=%s,sells=%s,updated=%s",
            (addr, contract, score, buys, sells, int(time.time()),
             score, buys, sells, int(time.time())))
    else:
        d = _read_kv("wallet_pnl")
        d[addr] = {"score": score, "buys": buys, "sells": sells}
        _write_kv("wallet_pnl", d)


def known_pnl(addr):
    if _PG:
        cur = _conn.cursor()
        cur.execute("SELECT realized_score,buys,sells FROM wallet_pnl WHERE addr=%s", (addr,))
        r = cur.fetchone()
        return {"score": r[0], "buys": r[1], "sells": r[2]} if r else None
    else:
        return _read_kv("wallet_pnl").get(addr)


def top_smart_wallets(n=10):
    if _PG:
        cur = _conn.cursor()
        cur.execute("SELECT addr,realized_score FROM wallet_pnl "
                    "ORDER BY realized_score DESC LIMIT %s", (n,))
        return [{"addr": a, "score": s} for a, s in cur.fetchall()]
    else:
        d = _read_kv("wallet_pnl")
        items = sorted(d.items(), key=lambda x: -x[1].get("score", 0))[:n]
        return [{"addr": a, "score": v["score"]} for a, v in items]


# ── 파일 폴백 헬퍼 ──
def _fp(name):
    return os.path.join(FALLBACK_DIR, name + ".json")


def _read_file(name):
    try:
        return json.load(open(_fp(name)))
    except Exception:
        return []


def _write_file(name, rows):
    os.makedirs(FALLBACK_DIR, exist_ok=True)
    json.dump(rows[-5000:], open(_fp(name), "w"))


def _append_file(name, row):
    rows = _read_file(name)
    rows.append(row)
    _write_file(name, rows)


def _read_kv(name):
    try:
        return json.load(open(_fp(name)))
    except Exception:
        return {}


def _write_kv(name, d):
    os.makedirs(FALLBACK_DIR, exist_ok=True)
    json.dump(d, open(_fp(name), "w"))


def is_postgres():
    return _PG
