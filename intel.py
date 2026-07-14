"""
intel.py — batch 2 온체인 인텔리전스 (DB 필요)
#2 홀더 분포 추적 · #3 자금줄 역추적 · #5 스마트머니 발굴
Etherscan V2 + db.py 조합. 키 없으면 조용히 스킵.
"""
import os
import time

ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
ES = "https://api.etherscan.io/v2/api"


def _get(params):
    import requests
    params["apikey"] = ETHERSCAN_KEY
    r = requests.get(ES, params=params, timeout=20)
    r.raise_for_status()
    return r.json()


# ── #2 홀더 분포 ──
def snapshot_holders(chainid, contract, deployer=""):
    """상위 홀더 조회 → top10/top50 집중도 계산 후 DB 저장 + 변화 알림"""
    import db
    alerts = []
    try:
        d = _get({"chainid": chainid, "module": "token",
                  "action": "tokenholderlist",  # Pro 전용일 수 있음
                  "contractaddress": contract, "page": "1", "offset": "100"})
        holders = d.get("result")
        if not isinstance(holders, list) or not holders:
            return []  # 무료플랜 미지원 → 조용히 스킵
        bals = sorted((float(h.get("TokenHolderQuantity", 0)) for h in holders),
                      reverse=True)
        total = sum(bals) or 1e-9
        top10 = sum(bals[:10]) / total * 100
        top50 = sum(bals[:50]) / total * 100
        n = len(holders)
        prev = db.prev_holder_snap(hours_ago=24)
        db.save_holder_snap(contract, top10, top50, n)
        if prev:
            d10 = top10 - prev["top10_pct"]
            if abs(d10) >= 3:
                arrow = "분산 중(집중도↓)" if d10 < 0 else "집중 중(집중도↑)"
                alerts.append(f"📊 홀더 분포 변화: 상위10 {prev['top10_pct']:.0f}%→{top10:.0f}% "
                              f"({d10:+.0f}%p, {arrow})")
            dn = n - prev["n_holders"]
            if prev["n_holders"] and abs(dn) / prev["n_holders"] > 0.15:
                alerts.append(f"👥 홀더 수 급변: {prev['n_holders']}→{n} ({dn:+d})")
    except Exception as e:
        print(f"[intel holders] {e}")
    return alerts


# ── #3 자금줄 역추적 ──
def trace_funding(chainid, addr, deployer, team_set):
    """지갑의 첫 유입(네이티브 토큰) 출처 추적 → 배포자/팀發이면 팀 분류"""
    import db
    known = db.known_funding(addr)
    if known:
        return known["is_team"]
    try:
        d = _get({"chainid": chainid, "module": "account", "action": "txlist",
                  "address": addr, "page": "1", "offset": "5",
                  "sort": "asc"})
        txs = d.get("result")
        if not isinstance(txs, list) or not txs:
            return False
        first_funder = txs[0].get("from", "").lower()
        is_team = (first_funder == deployer.lower()
                   or first_funder in {a.lower() for a in team_set})
        db.set_wallet_funding(addr, first_funder, is_team)
        return is_team
    except Exception as e:
        print(f"[intel funding] {e}")
        return False


# ── #5 스마트머니 ──
def update_smart_money(contract, txs, pair_addr, price):
    """전송 스트림에서 DEX 매수/매도 시점의 가격 기록 → 지갑별 타이밍 점수.
    저점 매수 + 고점 매도 = 높은 점수. 누적해서 상위 지갑 발굴."""
    import db
    if not price or not pair_addr:
        return []
    for tx in txs:
        f, t, amt = tx["from"], tx["to"], tx["amount"]
        if f == pair_addr and t != pair_addr:      # DEX에서 매수
            _bump_pnl(db, contract, t, price, "buy", amt)
        elif t == pair_addr and f != pair_addr:    # DEX로 매도
            _bump_pnl(db, contract, f, price, "sell", amt)
    return []


# 지갑별 러닝 점수: 매수는 저가일수록 +, 매도는 고가일수록 +
_PRICE_WINDOW = {}  # contract -> [prices] 최근 가격으로 상대적 고저 판단


def _bump_pnl(db, contract, addr, price, side, amt):
    w = _PRICE_WINDOW.setdefault(contract, [])
    w.append(price)
    if len(w) > 200:
        w.pop(0)
    lo, hi = min(w), max(w)
    rng = (hi - lo) or 1e-9
    pos = (price - lo) / rng   # 0=바닥, 1=천장
    # 매수는 바닥일수록 좋음(1-pos), 매도는 천장일수록 좋음(pos)
    quality = (1 - pos) if side == "buy" else pos
    weight = min(amt / 100_000, 5)  # 큰 거래일수록 가중(상한)
    delta = (quality - 0.5) * 2 * weight  # -weight ~ +weight
    cur = _read_score(db, addr)
    cur["score"] += delta
    cur[side + "s"] += 1
    db.update_wallet_pnl(addr, contract, round(cur["score"], 2),
                         cur["buys"], cur["sells"])


_score_cache = {}


def _read_score(db, addr):
    existing = db.known_pnl(addr)
    if existing:
        return {"score": existing["score"], "buys": existing["buys"],
                "sells": existing["sells"]}
    return {"score": 0.0, "buys": 0, "sells": 0}


def smart_money_report():
    import db
    top = db.top_smart_wallets(10)
    if not top:
        return "스마트머니 데이터 축적 중 — 며칠 운용 후 확인하세요."
    lines = ["🧠 스마트머니 (이 토큰 매매 타이밍 상위 지갑):"]
    for i, w in enumerate(top, 1):
        if w["score"] > 0:
            lines.append(f"{i}. {w['addr'][:10]}… 점수 {w['score']:+.1f}")
    lines.append("\n※ 저점매수+고점매도 이력 기반. 워치리스트 승격 후보.")
    return "\n".join(lines)
