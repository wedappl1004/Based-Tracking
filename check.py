"""
check.py — BASED 감시 1회 실행 (GitHub Actions가 15분마다 자동 실행)
OKX(캔들/펀딩/OI) + Hyperliquid + DexScreener → 신호 감지 → 텔레그램 전송
의존성: requests 하나뿐
"""
import json
import os
import time
import requests
try:
    import db as _db
    _db.setup()
    _DB = True
    _WEIGHTS = _db.load_weights()
    if _WEIGHTS:
        print(f"[db] 학습 가중치 {len(_WEIGHTS)}개 로드")
except Exception as _e:
    print(f"[db] 비활성: {_e}")
    _DB = False
    _WEIGHTS = {}

COIN = "BASED"
OKX = "https://www.okx.com"
# ── 학습된 보정값 자동 적용 (backtest.py가 생성, 'train' 명령으로 갱신) ──
RSI_LOW, RSI_HIGH = 27, 73
SUPPORT_LEVEL, STOP_LEVEL = 0.075, 0.06
_TRAINED = False
try:
    with open("params.json") as _f:
        _p = json.load(_f)
    SUPPORT_LEVEL = _p.get("SUPPORT_LEVEL", SUPPORT_LEVEL)
    STOP_LEVEL = _p.get("STOP_LEVEL", STOP_LEVEL)
    RSI_LOW = _p.get("RSI_LOW", RSI_LOW)
    RSI_HIGH = _p.get("RSI_HIGH", RSI_HIGH)
    _TRAINED = True
    print(f"[params] 학습값 적용: 지지 {SUPPORT_LEVEL} 손절 {STOP_LEVEL} RSI {RSI_LOW}/{RSI_HIGH}")
except Exception:
    pass
REALERT_HOURS = 6       # 같은 신호 재알림 간격
# 급등/급락 감지 (15분봉 기준)
MOVE_15M_PCT = 3.0      # 15분 ±3% 이상이면 알림
MOVE_1H_PCT = 8.0       # 1시간 ±8% 이상이면 알림
MOVE_ATR_MULT = 3.0     # 평소 변동성(ATR)의 3배 이상 캔들
STATE_FILE = "state.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")

# 코인글래스 (선택) — 있으면 집계 OI/펀딩/청산/진짜 CVD로 업그레이드
COINGLASS_KEY = os.environ.get("COINGLASS_API_KEY", "")
CG = "https://open-api-v4.coinglass.com"

# 고래 추적 (선택) — Etherscan V2 키 하나로 BNB체인 조회
ETHERSCAN_KEY = os.environ.get("ETHERSCAN_API_KEY", "")
# 쉼표로 여러 개 가능. 기본 체인은 BNB(56), 다른 체인이면 "체인ID:주소" 형식
# 예: "0xAAA...,0xBBB...,1:0xCCC..."  (1=이더리움, 56=BNB, 8453=Base)
BASED_CONTRACT = os.environ.get("BASED_CONTRACT", "")

import re

def parse_contracts():
    """쉼표/줄바꿈/공백/붙여쓰기 전부 허용. "체인ID:0x..." 형식이면 체인 고정,
    아니면 None으로 두고 실행 시 DexScreener로 자동 판별."""
    out, used = [], set()
    for m in re.finditer(r"(?:(\d+)\s*:\s*)?(0x[a-fA-F0-9]{40})", BASED_CONTRACT):
        chain, addr = m.group(1), m.group(2).lower()
        if addr not in used:
            used.add(addr)
            out.append((chain, addr))
    return out

CONTRACTS = parse_contracts()

# DexScreener 체인 이름 → Etherscan V2 체인 ID
CHAIN_IDS = {"bsc": "56", "ethereum": "1", "base": "8453",
             "arbitrum": "42161", "polygon": "137", "optimism": "10",
             "avalanche": "43114", "hyperevm": "999", "linea": "59144",
             "scroll": "534352", "blast": "81457", "sonic": "146"}
WHALE_THRESHOLD = 100_000  # 이 수량 이상 이동 시 고래로 간주

# ── 관찰 지갑 (워치리스트) ────────────────────────────
# 이 지갑들은 금액 무관, 움직이면 무조건 알림. DEX 매도는 최상급 경보.
# 추가/삭제: 아래 목록 수정 or 시크릿 WATCH_WALLETS에 "주소=라벨,주소" 형식으로
WATCHLIST = {
    "0x78a0cddf1e0c966d505181b0dfaf505e398d053a": "0x78a0",
    "0x3e5dcdbded6ca3d2a78eb14c307c9ed7a9638c52": "0x3e5d",
    "0x40c0e5f38fecacd5d7dbea41cd1c34c8917a25b0": "0x40c0",
    "0x774922fbb5a9e6d52c14fa9dfa25c16219f91c90": "0x7749",
    "0x6908518e91b83a05a51b8961d1c5667b2e9bc4a3": "0x6908",
    "0xc3526ad0a5fa2d7bac0963904036d8604b13470e": "0xc352",
    "0x42a99d7dc78415ea0995edd8d5e718495a07a7c1": "0x42a9",
    "0xc4be9808281709d47489ce1e2a5422da29e5506a": "0xc4be",
    "0x4bdaa8005233f251375212efab1d2ce938d62b78": "0x4bda",
    "0xfd09a9cc989cd9d7ff0a1cab6af28c677267a2b9": "0xfd09",
    "0x738a2c8dd840831b9bb9e4103c68d98c41e20abe": "Liquid_Token_Fund_2",
    "0x16f187b493b2f4380dcb2ec156df6f7fa46cd2e4": "0x16f1",
    "0xa5c515d8e2ff6b54de12c5fa5f0bb64d341b7f56": "0xa5c5",
    "0x82bf8528979e64a0b9467caf4a2f0a37aae7e44d": "고점 판매자",
    "0x36420bbdb37db0aa8e999855d41642ba13f18334": "0x3642",
    "0xf4767bf23d8a7d24aeac0a8c3993e1519b42698b": "0xf476",
    "0x2c2b2ab6dce8db3fe75f330d8a6da94b1d11f8c5": "0x2c2b",
    "0xedd0a6c1889be3a05aba0c3abfb80f409909b7e6": "0xedd0",
    "0x78b8aecf9b138bb409a51d3111f2db7dc57ddda0": "0x78b8",
    "0xbd47565f3b67e81968f39ad034d340da54c32e5b": "0xbd47",
    "0xe584349166881881651f350265c3190632d60c29": "0xe584",
    "0xb055c5f69c7250cf5dc6ccb5cfd72a55dd8c662e": "0xb055",
    "0x7142e1a36e6f41d30f9b1dd5dd82014d0be184f7": "0x7142",
    "0x6196cbf7b0c0cd3f620dbbc8c1e36992d795c223": "0x6196",
    "0x24761589719dfd062ffdde6f046cbdd96ca1fd95": "0x2476",
    "0xc9f2335bc08f1e908b2b5340921c3eec983d9245": "0xc9f2",
    "0x300b88fe6350dba2513206b4cf15b9eb2c9c4a90": "0x300b",
    "0x44115753c8d053707845dabe45c90b9ccc6fbc51": "0x4411",
    "0xa7cbbce0c927929642c03f9f1a81ab605ec6ce8a": "0xa7cb",
    "0x3279890e9d96de71712b1dfb09adafb4c4d9e831": "0x3279",
    "0x87180fee757025fe489cb73b7c9e5b8d0a15b6e1": "0x8718",
    "0xa97111aac0732c41b99da656c0860f7a482493d6": "0xa971",
    "0x46e21fa3a6b5d2ec25dd756d445891e3ef9fbb51": "0x46e2",
    "0x116e92ff3a89d6c8951358ca21888863bfd3ba4e": "0x116e",
    "0x7eee7d761cd46f9e430c9d55b40ef3de63de6ee4": "0x7eee",
    "0x8f01a3ecfa602650ac920ae93fad4168b0b34a6c": "0x8f01",
    "0x0055f25f3b7a71d2796c45151f9b8d161547727f": "0x0055",
    "0xd3cb1823da2ff584dec3f49ef6a3eea51471e5bc": "제네시스 95M 홀더",
    "0xa0acf83f7ef684a75533a6635fb6d6075320625b": "0xa0ac",
    "0x5cbf6ae887f20ef3bd28276c744cc119f47f9c87": "0x5cbf",
    "0x8cb03f7e632bec6f149239715e60778f3c550a22": "0x8cb0",
    "0xcb65303778fcfeeb45d8be1139987fb2534cefb0": "0xcb65",
}
for _item in os.environ.get("WATCH_WALLETS", "").split(","):
    _item = _item.strip()
    if _item.startswith("0x"):
        _a, _, _l = _item.partition("=")
        WATCHLIST[_a.strip().lower()] = _l.strip() or _a[:8]
WATCH_MIN = 25_000   # 워치리스트 알림 최소 수량 (더스트 스팸 방지)
# True = 온체인 알림은 내 워치리스트 지갑만 (조용함)
# False = 모든 지갑의 수상한 움직임도 알림 (매집/고래/쪼개기 등)
WATCH_ONLY = True
# 온체인 푸시 알림 수준: "off"=안 받음 / "critical"=관찰지갑·팀 DEX매도만 / "all"=전부
ONCHAIN_PUSH = "critical"
MEGA_THRESHOLD = 1_000_000   # 이 이상 단일 이동은 지갑 무관 무조건 알림
SURGE_MULT = 5               # 사이클 총 이동량이 평소의 N배면 서지 경보
NEWWALLET_MULT = 3           # 신규 지갑 등장이 평소의 N배(그리고 10개+)면 경보
# WATCH_ONLY여도 팀 클러스터의 대형 DEX 매도(러그 경보)는 항상 알림
TEAM_SELL_MIN = 50_000



# ── 데이터 수집 ──────────────────────────────────────
def get(url, **kw):
    r = requests.get(url, timeout=15, **kw)
    r.raise_for_status()
    return r.json()


def fetch_candles():
    for inst in (f"{COIN}-USDT-SWAP", f"{COIN}-USDT"):
        try:
            d = get(f"{OKX}/api/v5/market/candles",
                    params={"instId": inst, "bar": "15m", "limit": "300"})
            rows = d.get("data") or []
            if rows:
                rows.reverse()
                return [{"o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
                         "c": float(r[4]), "v": float(r[5])} for r in rows]
        except Exception as e:
            print(f"[candles {inst}] {e}")
    raise RuntimeError("캔들 수신 실패")


def fetch_funding():
    try:
        d = get(f"{OKX}/api/v5/public/funding-rate",
                params={"instId": f"{COIN}-USDT-SWAP"})
        return float(d["data"][0]["fundingRate"])
    except Exception:
        return None


def fetch_oi():
    try:
        d = get(f"{OKX}/api/v5/public/open-interest",
                params={"instId": f"{COIN}-USDT-SWAP"})
        row = d["data"][0]
        return float(row.get("oiUsd") or 0) or float(row.get("oiCcy") or 0)
    except Exception:
        return None


def fetch_hl():
    try:
        r = requests.post("https://api.hyperliquid.xyz/info",
                          json={"type": "metaAndAssetCtxs"}, timeout=15)
        meta, ctxs = r.json()
        for i, u in enumerate(meta["universe"]):
            if u["name"].upper() == COIN:
                c = ctxs[i]
                return {"funding": float(c["funding"]), "oi": float(c["openInterest"])}
    except Exception as e:
        print(f"[HL] {e}")
    return None


def fetch_dex_liquidity():
    try:
        d = get("https://api.dexscreener.com/latest/dex/search", params={"q": COIN})
        pairs = [p for p in d.get("pairs") or []
                 if (p.get("baseToken") or {}).get("symbol", "").upper() == COIN]
        if pairs:
            p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
            return {"liq": (p.get("liquidity") or {}).get("usd"),
                    "pair": (p.get("pairAddress") or "").lower()}
    except Exception as e:
        print(f"[dex] {e}")
    return None


def cg(path, **params):
    """코인글래스 V4 호출. 키 없거나 플랜 미지원이면 None (자동 폴백)"""
    if not COINGLASS_KEY:
        return None
    try:
        r = requests.get(CG + path, params=params, timeout=15,
                         headers={"CG-API-KEY": COINGLASS_KEY,
                                  "Accept": "application/json"})
        d = r.json()
        if str(d.get("code")) != "0":
            print(f"[CG {path}] {d.get('msg')}")
            return None
        return d.get("data")
    except Exception as e:
        print(f"[CG {path}] {e}")
        return None


# BASED가 상장된 거래소 후보 (롱숏/페어 조회용, 순서대로 시도)
CG_EXCHANGES = ["OKX", "Bybit", "Gate", "Binance", "Bitget", "HTX"]
# 집계 엔드포인트용 exchange_list (필수 파라미터)
CG_EXLIST = "OKX,Bybit,Gate,Binance,Bitget"

def cg_try(paths, intervals=None, exchanges=None, **params):
    """경로 × 인터벌 × (필요시)거래소 조합을 순서대로 시도. 첫 성공 반환."""
    iv_list = intervals or [params.pop("interval", "4h")]
    params.pop("interval", None)
    ex_list = exchanges or [None]
    for iv in iv_list:
        for ex in ex_list:
            p2 = dict(params)
            if ex is not None:
                p2["exchange"] = ex
            for p in paths:
                d = cg(p, interval=iv, **p2)
                if d:
                    return d
    return None


def fetch_cg_bundle():
    """집계 OI + 펀딩 + 청산 + 볼륨 + 롱숏 (Hobbyist: 4h 인터벌)"""
    out = {}

    IVS = ["4h", "12h", "1d"]
    oi = cg_try([
        "/api/futures/open-interest/aggregated-history",
        "/api/futures/openInterest/aggregated-history",
        "/api/futures/open-interest/aggregated-ohlc-history",
    ], intervals=IVS, symbol=COIN, limit=60)
    if oi:
        closes = [float(x.get("close") or x.get("c") or 0) for x in oi]
        if len(closes) >= 2 and closes[-1]:
            out["oi_now"] = closes[-1]
            # 24h 전 인덱스는 인터벌 모르니 대략 마지막에서 되돌아봄
            back = min(6, len(closes)-1)
            base = closes[-1-back]
            out["oi_chg_24h"] = (closes[-1]/base - 1) * 100 if base else 0

    fr = cg_try([
        "/api/futures/funding-rate/oi-weight-history",
        "/api/futures/funding-rate/oi-weight-ohlc-history",
    ], intervals=["8h","1d"], symbol=COIN, limit=6)
    if fr:
        out["funding_w"] = float(fr[-1].get("close") or fr[-1].get("c")
                                 or fr[-1].get("fundingRate") or 0)

    liq = cg_try([
        "/api/futures/liquidation/aggregated-history",
    ], intervals=IVS, symbol=COIN, limit=12, exchange_list=CG_EXLIST)
    if liq:
        L = lambda x,*k: next((float(x[key]) for key in k if x.get(key) is not None), 0)
        longs = [L(x,"longLiquidationUsd","long_liquidation_usd","aggregated_long_liquidation_usd") for x in liq]
        shorts = [L(x,"shortLiquidationUsd","short_liquidation_usd","aggregated_short_liquidation_usd") for x in liq]
        out["liq_long_now"], out["liq_short_now"] = longs[-1], shorts[-1]
        out["liq_long_avg"] = avg(longs[:-1]) or 1e-9
        out["liq_short_avg"] = avg(shorts[:-1]) or 1e-9

    fv = cg_try([
        "/api/futures/aggregated-taker-buy-sell-volume/history",
    ], intervals=IVS, symbol=COIN, limit=12, exchange_list=CG_EXLIST)
    if fv:
        vol = 0
        for x in fv:
            vol += float(x.get("buy") or x.get("taker_buy_volume_usd") or x.get("aggregated_buy_volume_usd") or 0)
            vol += float(x.get("sell") or x.get("taker_sell_volume_usd") or x.get("aggregated_sell_volume_usd") or 0)
        if vol: out["fut_vol_24h"] = vol

    sv = cg_try([
        "/api/spot/aggregated-taker-buy-sell-volume/history",
    ], intervals=IVS, symbol=COIN, limit=12, exchange_list=CG_EXLIST)
    if sv:
        buys = sells = 0
        for x in sv:
            buys += float(x.get("buy") or x.get("taker_buy_volume_usd") or x.get("aggregated_buy_volume_usd") or 0)
            sells += float(x.get("sell") or x.get("taker_sell_volume_usd") or x.get("aggregated_sell_volume_usd") or 0)
        if buys+sells:
            out["spot_vol_24h"] = buys + sells
            out["spot_delta_24h"] = buys - sells

    gls = cg_try([
        "/api/futures/global-long-short-account-ratio/history",
    ], intervals=IVS, exchanges=CG_EXCHANGES, symbol=COIN, limit=4)
    if gls:
        out["ls_global"] = float(gls[-1].get("longShortRatio")
                                 or gls[-1].get("global_account_long_short_ratio")
                                 or gls[-1].get("long_short_ratio") or 0)

    tls = cg_try([
        "/api/futures/top-long-short-position-ratio/history",
    ], intervals=IVS, exchanges=CG_EXCHANGES, symbol=COIN, limit=4)
    if tls:
        out["ls_top"] = float(tls[-1].get("longShortRatio")
                              or tls[-1].get("top_position_long_short_ratio")
                              or tls[-1].get("long_short_ratio") or 0)

    tk = cg_try([
        "/api/futures/aggregated-taker-buy-sell-volume/history",
    ], intervals=IVS, symbol=COIN, limit=12, exchange_list=CG_EXLIST)
    if tk:
        deltas = []
        for x in tk:
            b = float(x.get("buy") or x.get("aggregated_buy_volume_usd") or 0)
            s = float(x.get("sell") or x.get("aggregated_sell_volume_usd") or 0)
            deltas.append(b - s)
        if deltas:
            out["taker_d10"] = avg(deltas[-3:])
            out["taker_d30"] = avg(deltas[-6:])
    return out


def fetch_transfers(chainid, contract, start_block):
    """start_block 이후의 해당 토큰 전송 (최대 500건)"""
    if not ETHERSCAN_KEY:
        return []
    try:
        d = get("https://api.etherscan.io/v2/api", params={
            "chainid": chainid, "module": "account", "action": "tokentx",
            "contractaddress": contract,
            "startblock": str(start_block), "endblock": "latest",
            "page": "1", "offset": "500", "sort": "asc",
            "apikey": ETHERSCAN_KEY})
        txs = d.get("result") or []
        if isinstance(txs, str):
            print(f"[chain] API: {txs}")
            return []
        out = []
        for tx in txs:
            dec = int(tx.get("tokenDecimal") or 18)
            out.append({"hash": tx["hash"], "block": int(tx["blockNumber"]),
                        "from": tx["from"].lower(), "to": tx["to"].lower(),
                        "amount": int(tx["value"]) / (10 ** dec),
                        "symbol": tx.get("tokenSymbol", "")})
        return out
    except Exception as e:
        print(f"[chain] {e}")
        return []


def fetch_deployer(chainid, contract):
    """컨트랙트 배포자(팀 추정) 주소 — 최초 1회만 조회"""
    try:
        d = get("https://api.etherscan.io/v2/api", params={
            "chainid": chainid, "module": "contract",
            "action": "getcontractcreation",
            "contractaddresses": contract,
            "apikey": ETHERSCAN_KEY})
        r = d.get("result")
        if isinstance(r, list) and r:
            return r[0].get("contractCreator", "").lower()
    except Exception as e:
        print(f"[deployer] {e}")
    return ""


def fetch_pair_for(contract):
    """토큰 주소로 최대 유동성 페어 + 심볼 + 체인 자동 판별"""
    try:
        d = get(f"https://api.dexscreener.com/latest/dex/tokens/{contract}")
        pairs = d.get("pairs") or []
        if pairs:
            p = max(pairs, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
            return ((p.get("pairAddress") or "").lower(),
                    (p.get("baseToken") or {}).get("symbol", ""),
                    (p.get("chainId") or "").lower())
    except Exception as e:
        print(f"[pair {contract[:8]}] {e}")
    return "", "", ""


def analyze_chain(txs, chain_state, pair_addr=""):
    """러그/덤핑 플레이북 대응 탐지기.
    핵심 관점: 던지는 쪽은 탐지를 피하려고 쪼개고, 경유시키고, 새 지갑을 쓴다.
    chain_state: {ledger, recv_count, deployer, seen_wallets, team}"""
    alerts = []
    ledger = chain_state.setdefault("ledger", {})
    recv_count = chain_state.setdefault("recv_count", {})
    seen = set(chain_state.setdefault("seen_wallets", []))
    seen_before = set(seen)  # 이번 배치 전 기준 (신규 지갑 판정용)
    team = set(chain_state.setdefault("team", []))       # 팀 클러스터 (전염 추적)
    deployer = chain_state.get("deployer", "")
    if deployer:
        team.add(deployer)
    short = lambda a: a[:8] + "…"

    def looks_exchange(addr):
        return len(recv_count.get(addr, [])) >= 20

    by_sender, by_recipient, by_pairkey = {}, {}, {}
    received_now, sent_now = {}, {}
    pair_inflow_total = 0.0
    pair_inflow_team = 0.0

    for tx in txs:
        f, t, amt = tx["from"], tx["to"], tx["amount"]
        ledger[f] = ledger.get(f, 0) - amt
        ledger[t] = ledger.get(t, 0) + amt
        senders = recv_count.setdefault(t, [])
        if f not in senders:
            senders.append(f); recv_count[t] = senders[-30:]
        by_sender.setdefault(f, []).append(tx)
        by_recipient.setdefault(t, []).append(tx)
        by_pairkey.setdefault((f, t), []).append(amt)
        received_now[t] = received_now.get(t, 0) + amt
        sent_now[f] = sent_now.get(f, 0) + amt

        # ── 매집 추적: 페어에서 받음 = DEX 매수 ──
        acc = chain_state.setdefault("accum", {})
        if pair_addr and f == pair_addr and t != pair_addr:
            rec = acc.setdefault(t, {"buy_amt": 0, "buy_n": 0,
                                     "in_amt": 0, "in_n": 0, "alerted": 0})
            rec["buy_amt"] += amt
            rec["buy_n"] += 1
        elif t != pair_addr and f != pair_addr and amt >= WATCH_MIN:
            rec = acc.setdefault(t, {"buy_amt": 0, "buy_n": 0,
                                     "in_amt": 0, "in_n": 0, "alerted": 0})
            rec["in_amt"] += amt
            rec["in_n"] += 1

        # ── 관찰 지갑: 즉시 알림은 DEX 매도만, 나머지는 시간별 요약으로 집계 ──
        if amt >= WATCH_MIN:
            wsum = chain_state.setdefault("watch_summary", {})
            if f in WATCHLIST:
                lbl = WATCHLIST[f]
                if pair_addr and t == pair_addr:
                    alerts.append(f"🚨👁 관찰지갑 [{lbl}] DEX 매도: {amt:,.0f} — 최우선 확인")
                elif looks_exchange(t):
                    alerts.append(f"🚨👁 관찰지갑 [{lbl}] 거래소 입금: {amt:,.0f} (매도 준비 가능)")
                else:
                    w = wsum.setdefault(lbl, {"out": 0.0, "out_n": 0, "in": 0.0, "in_n": 0})
                    w["out"] += amt
                    w["out_n"] += 1
            if t in WATCHLIST:
                lbl = WATCHLIST[t]
                w = wsum.setdefault(lbl, {"out": 0.0, "out_n": 0, "in": 0.0, "in_n": 0})
                w["in"] += amt
                w["in_n"] += 1

        # ── 팀 클러스터 전염: 팀 지갑에서 물량 받으면 팀 취급 ──
        if f in team and amt > 0:
            if t not in team and t != pair_addr and not looks_exchange(t):
                team.add(t)
                alerts.append(f"🏗️ 팀 물량 분배: {short(f)}(팀) → {short(t)} {amt:,.0f} — 이 지갑도 팀 클러스터로 추적 시작")

        # ── DEX 페어로 유입 = 실제 매도 (유동성 감소보다 빠른 선행 신호) ──
        if pair_addr and t == pair_addr:
            pair_inflow_total += amt
            if f in team:
                pair_inflow_team += amt
                if amt >= TEAM_SELL_MIN:
                    alerts.append(f"🚨 팀 클러스터가 DEX에 매도: {short(f)} → 페어 {amt:,.0f} — 러그 경보 최상급")
            elif amt >= WHALE_THRESHOLD:
                alerts.append(f"📉 대형 DEX 매도: {short(f)} → 페어 {amt:,.0f}")

        # 기본 패턴
        if amt >= WHALE_THRESHOLD and t != pair_addr:
            tag = " (거래소 추정 ⚠️)" if looks_exchange(t) else ""
            alerts.append(f"🐋 대형 이동: {amt:,.0f} → {short(t)}{tag}")
        if t not in seen and amt >= WHALE_THRESHOLD and t != pair_addr:
            alerts.append(f"👶 신규 지갑에 대형 수령: {short(t)} +{amt:,.0f}")
        seen.add(f); seen.add(t)

    # ── 임계값 회피 쪼개기: 같은 발신→수신, 개별은 작지만 합산 초과 ──
    for (f, t), amts in by_pairkey.items():
        if len(amts) >= 3 and max(amts) < WHALE_THRESHOLD and sum(amts) >= WHALE_THRESHOLD:
            alerts.append(f"🔪 쪼개기 전송 감지: {short(f)} → {short(t)} {len(amts)}회 합계 {sum(amts):,.0f} (임계값 회피 패턴)")

    # ── 경유 세탁 (peel chain): 받자마자 90%+ 즉시 재발송 ──
    for w in received_now:
        r, s = received_now[w], sent_now.get(w, 0)
        if r >= WHALE_THRESHOLD / 2 and s >= r * 0.9 and w != pair_addr:
            alerts.append(f"🔗 경유 지갑 감지: {short(w)} 수령 {r:,.0f} → 즉시 {s:,.0f} 재발송 (출처 세탁 패턴)")
            if any(x["from"] in team for x in by_recipient.get(w, [])):
                team.add(w)

    # ── 워시 루프: A→B와 B→A가 같은 배치에 ──
    for (f, t) in list(by_pairkey.keys()):
        if f < t and (t, f) in by_pairkey:
            tot = sum(by_pairkey[(f, t)]) + sum(by_pairkey[(t, f)])
            if tot >= WHALE_THRESHOLD:
                alerts.append(f"🔄 왕복 이동 감지: {short(f)} ↔ {short(t)} 합계 {tot:,.0f} (워시/교란 가능)")

    # ── 분산 이동 / 집결 (기존) ──
    for f, lst in by_sender.items():
        recips = {x["to"] for x in lst} - {pair_addr}
        total = sum(x["amount"] for x in lst if x["to"] != pair_addr)
        if len(recips) >= 5 and total >= WHALE_THRESHOLD:
            tag = " [팀!]" if f in team else ""
            alerts.append(f"🪓 분산 이동: {short(f)}{tag} → {len(recips)}개 지갑 합계 {total:,.0f}")
    for t, lst in by_recipient.items():
        sndrs = {x["from"] for x in lst}
        total = sum(x["amount"] for x in lst)
        if len(sndrs) >= 5 and total >= WHALE_THRESHOLD and not looks_exchange(t) and t != pair_addr:
            alerts.append(f"🧲 물량 집결: {len(sndrs)}개 지갑 → {short(t)} 합계 {total:,.0f}")

    # ── 상위 지갑 대량 유출 (기존) ──
    top_set = {a for a, _ in sorted(ledger.items(), key=lambda x: -x[1])[:10]}
    for f, lst in by_sender.items():
        if f in top_set:
            out_amt = sum(x["amount"] for x in lst)
            bal_before = ledger.get(f, 0) + out_amt
            if bal_before > 0 and out_amt / bal_before > 0.5 and out_amt >= WHALE_THRESHOLD / 3:
                alerts.append(f"📤 상위 지갑 대량 유출: {short(f)} 관찰 보유량의 {out_amt/bal_before:.0%} 발신")

    # ── 매집 판정 ──
    acc = chain_state.setdefault("accum", {})
    for addr, rec in acc.items():
        if addr in WATCHLIST or addr in team or looks_exchange(addr):
            continue
        bal = ledger.get(addr, 0)
        # DEX 매집: 3회+ 매수 & 누적 5만+ & 안 팔고 보유 중
        dex_hit = (rec["buy_n"] >= 3 and rec["buy_amt"] >= WHALE_THRESHOLD / 2
                   and bal >= rec["buy_amt"] * 0.7)
        # 이체 매집: 3회+ 수신 & 누적 10만+ & 유출 거의 없음
        otc_hit = (rec["in_n"] >= 3 and rec["in_amt"] >= WHALE_THRESHOLD
                   and bal >= rec["in_amt"] * 0.7)
        total = rec["buy_amt"] + rec["in_amt"]
        if (dex_hit or otc_hit) and total >= max(rec["alerted"] * 2, 1):
            kind = "DEX 매수" if dex_hit else "이체 수신"
            n = rec["buy_n"] if dex_hit else rec["in_n"]
            alerts.append(f"🧺 매집 감지: {short(addr)} {kind} {n}회 누적 "
                          f"{total:,.0f} 보유 중 — 새 관찰 후보")
            rec["alerted"] = total
    # accum 용량 관리
    chain_state["accum"] = dict(sorted(
        acc.items(), key=lambda x: -(x[1]["buy_amt"] + x[1]["in_amt"]))[:200])

    # ── #1 CEX 넷플로우 (거래소 입출금 순유입) ──
    try:
        import netflow
        alerts += netflow.analyze_netflow(txs, chain_state)
    except Exception as _e:
        print(f"[netflow] {_e}")

    # ── 시스템 레벨 이상 징후 (지갑 무관, 워치리스트 모드에서도 알림) ──
    cycle_vol = sum(tx["amount"] for tx in txs)
    new_wallets_n = sum(1 for tx in txs
                        for a in (tx["from"], tx["to"])
                        if a not in seen_before)
    vh = chain_state.setdefault("vol_hist", [])
    nh = chain_state.setdefault("neww_hist", [])
    if len(vh) >= 6:  # 기준선 확보 후부터 판정 (약 30분치)
        base_v = sorted(vh)[len(vh)//2] or 1e-9   # 중앙값
        base_n = sorted(nh)[len(nh)//2]
        if cycle_vol > base_v * SURGE_MULT and cycle_vol >= WHALE_THRESHOLD:
            alerts.append(f"🌊 온체인 물량 서지: 이번 구간 {cycle_vol:,.0f} 이동 "
                          f"(평소의 {cycle_vol/base_v:.0f}배) — 대규모 재배치/덤핑 가능")
        if new_wallets_n >= 10 and new_wallets_n > max(base_n, 1) * NEWWALLET_MULT:
            alerts.append(f"👥 신규 지갑 급증: 이번 구간 {new_wallets_n}개 첫 등장 "
                          f"(평소 {base_n}개) — 분배 준비/시빌 활동 가능")
    vh.append(cycle_vol); nh.append(new_wallets_n)
    chain_state["vol_hist"] = vh[-48:]
    chain_state["neww_hist"] = nh[-48:]

    # 메가 단일 이동 (지갑 무관) — 체인홉(같은 물량 연쇄 이동)은 경로 1건으로 병합
    megas = [tx for tx in txs if tx["amount"] >= MEGA_THRESHOLD
             and tx["to"] != pair_addr]
    used = set()
    for i, tx in enumerate(megas):
        if i in used:
            continue
        path = [tx["from"], tx["to"]]
        amt = tx["amount"]
        # 이어지는 홉 찾기: 도착지가 다음 출발지 + 금액 유사(±1%)
        changed = True
        while changed:
            changed = False
            for j, nx in enumerate(megas):
                if j in used or j == i:
                    continue
                if nx["from"] == path[-1] and abs(nx["amount"]-amt)/amt < 0.01:
                    path.append(nx["to"])
                    used.add(j)
                    changed = True
        used.add(i)
        if len(path) > 2:
            route = " → ".join(short(a) for a in path)
            alerts.append(f"🐳 메가 이동(경유 {len(path)-1}홉): {amt:,.0f} {route}")
        else:
            alerts.append(f"🐳 메가 이동: {amt:,.0f} "
                          f"{short(path[0])} → {short(path[1])}")

    # ── 팀 클러스터 총 매도 요약 ──
    if pair_inflow_team >= TEAM_SELL_MIN:
        alerts.append(f"🚨 이번 구간 팀 클러스터 총 DEX 매도: {pair_inflow_team:,.0f} (전체 매도 유입의 {pair_inflow_team/max(pair_inflow_total,1e-9):.0%})")

    # 상태 용량 관리
    chain_state["ledger"] = dict(sorted(ledger.items(), key=lambda x: -abs(x[1]))[:300])
    chain_state["recv_count"] = {k: v for k, v in sorted(recv_count.items(), key=lambda x: -len(x[1]))[:200]}
    chain_state["seen_wallets"] = list(seen)[-2000:]
    chain_state["team"] = list(team)[:100]
    if WATCH_ONLY:
        _keep = ("👁","🚨","🌊","👥","🐳","📥","📤","🔀")
        alerts = [a for a in alerts if "👁" in a or any(a.startswith(e) for e in _keep)]
    return alerts


# ── 지표 (순수 파이썬) ────────────────────────────────
def ema(arr, n):
    k, out, p = 2 / (n + 1), [], None
    for i, v in enumerate(arr):
        p = v if i == 0 else v * k + p * (1 - k)
        out.append(p)
    return out


def rsi(cl, n=14):
    g = l = 0.0
    val = None
    for i in range(1, len(cl)):
        d = cl[i] - cl[i - 1]
        up, dn = max(d, 0), max(-d, 0)
        if i <= n:
            g += up; l += dn
            if i == n:
                g /= n; l /= n
                val = 100 - 100 / (1 + g / (l or 1e-12))
        else:
            g = (g * (n - 1) + up) / n
            l = (l * (n - 1) + dn) / n
            val = 100 - 100 / (1 + g / (l or 1e-12))
    return val


def macd_hist(cl):
    f, s = ema(cl, 12), ema(cl, 26)
    line = [f[i] - s[i] for i in range(len(cl))]
    sig = ema(line, 9)
    return [line[i] - sig[i] for i in range(len(cl))]


def cvd_series(cs):
    s, out = 0.0, []
    for c in cs:
        s += c["v"] * (1 if c["c"] >= c["o"] else -1)
        out.append(s)
    return out


avg = lambda a: sum(a) / (len(a) or 1)


# ── 신호 규칙 ────────────────────────────────────────
def build_signals(cs, funding, oi, hl, liq, prev, cgd=None):
    cgd = cgd or {}
    cl = [c["c"] for c in cs]
    p = cl[-1]
    cvd = cvd_series(cs)
    diffs = [cvd[i] - cvd[i - 1] for i in range(1, len(cvd))]
    s10, s30 = avg(diffs[-10:]), avg(diffs[-30:])
    mh = macd_hist(cl)
    r = rsi(cl)
    vol_ratio = cs[-1]["v"] / (avg([c["v"] for c in cs[-20:]]) or 1e-9)

    # 코인글래스 데이터 있으면 우선 사용 (집계 = 더 정확)
    if cgd.get("funding_w") is not None:
        funding = cgd["funding_w"]
    if cgd.get("taker_d30") is not None:
        s10, s30 = cgd["taker_d10"], cgd["taker_d30"]

    S = []
    if p < STOP_LEVEL:
        S.append(("stop", f"🚨 손절선 이탈: {p:.5f} < {STOP_LEVEL} — 시나리오 무효"))
    elif p < SUPPORT_LEVEL:
        S.append(("support", f"⚠️ 박스 하단 테스트: {p:.5f} (지지 {SUPPORT_LEVEL})"))

    # ── 전조 신호: 급변동 "전"의 판 짜임 감지 ──
    if len(cl) >= 60:
        # 최근 24캔들(6시간) 가격 변화 & 변동성 상태
        chg_6h = abs(cl[-1] / cl[-24] - 1) * 100
        rng_recent = avg([abs(cs[i]["h"] - cs[i]["l"]) / cs[i]["c"]
                          for i in range(len(cs) - 12, len(cs))])
        rng_base = avg([abs(cs[i]["h"] - cs[i]["l"]) / cs[i]["c"]
                        for i in range(len(cs) - 60, len(cs) - 12)])
        compressed = rng_base > 0 and rng_recent < rng_base * 0.55
        oi_up = cgd.get("oi_chg_24h") is not None and cgd["oi_chg_24h"] > 8
        flat = chg_6h < 2.0

        # 1) 변동성 압축 + OI 축적 = 스프링 감기는 중
        if compressed and oi_up:
            hint = ""
            if funding is not None:
                hint = " · 펀딩 음수→상방 스퀴즈 우세" if funding < -0.0002 else                        (" · 펀딩 과열→하방 청산 우세" if funding > 0.0005 else " · 방향 미정")
            S.append(("pre_squeeze",
                      f"🧨 전조: 변동성 압축 + OI +{cgd['oi_chg_24h']:.0f}% — 큰 움직임 준비 중{hint}"))

        soft_comp = rng_base > 0 and rng_recent < rng_base * 0.75

        # 2) 조용한 매집: 횡보 + 순매수 지속 + (OI 축적 or 변동성 압축) 동반 필수
        if flat and s10 > 0 and s30 > 0 and (oi_up or soft_comp):
            extra = f" + OI {cgd['oi_chg_24h']:+.0f}%" if oi_up else " (변동성 압축 동반)"
            S.append(("quiet_accum",
                      f"🤫 전조: 조용한 매집 — 가격 횡보 중 테이커 순매수 지속{extra} (급등 전 패턴)"))

        # 3) 조용한 분산: 횡보 + 순매도 지속 + (압축 or OI 축적) 동반 필수
        if flat and s10 < 0 and s30 < 0 and (soft_comp or oi_up):
            S.append(("quiet_dist",
                      f"🫗 전조: 조용한 분산 — 가격 횡보 중 테이커 순매도 지속 (급락 전 패턴)"))

        # ── 학습된 전조 매칭 (premove 부검 결과와 실시간 대조) ──
        try:
            with open("premove_params.json") as _pf:
                _learned = json.load(_pf)
        except Exception:
            _learned = None
        if _learned:
            # 현재 켜진 전조 집합 (부검과 동일 기준)
            v_recent = avg([c["v"] for c in cs[-12:]])
            v_base = avg([c["v"] for c in cs[-48:-12]]) or 1e-9
            active = {
                "압축": compressed,
                "매집": flat and s10 > 0 and s30 > 0,
                "분산": flat and s10 < 0 and s30 < 0,
                "거래량고갈": v_recent < v_base * 0.6,
                "RSI과매도": r is not None and r < 35,
                "RSI과열": r is not None and r > 65,
            }
            for bucket, emoji, label in (("pump", "📡🚀", "급등"), ("dump", "📡📉", "급락")):
                hits = [(k, _learned[bucket][k]) for k in _learned.get(bucket, {})
                        if active.get(k)]
                if hits:
                    names = "+".join(k for k, _ in hits)
                    stats = ", ".join(f"{k}: 과거 {v['rate']}%/평시 {v['lift']}배"
                                      for k, v in hits)
                    strength = "⚠️ 복합" if len(hits) >= 2 else ""
                    S.append((f"learned_{bucket}",
                              f"{emoji} 학습된 {label} 전조 감지 {strength}[{names}] — {stats}"))

    # ── 급등/급락 감지 (15분봉 ±3% / 1시간 ±8% / ATR 3배) ──
    if len(cl) >= 5:
        chg_15m = (cl[-1] / cl[-2] - 1) * 100
        chg_1h = (cl[-1] / cl[-5] - 1) * 100
        why = []
        if funding is not None:
            why.append(f"펀딩 {funding:.3%}")
        if s30 < 0:
            why.append("매도세 진행중")
        elif s10 > 0:
            why.append("테이커 매수 우위")
        why_str = (" | " + ", ".join(why)) if why else ""
        if chg_15m >= MOVE_15M_PCT:
            S.append(("pump_15m", f"🚀 급등 감지: 15분 {chg_15m:+.1f}% (가격 {p:.5f}){why_str}"))
        elif chg_15m <= -MOVE_15M_PCT:
            S.append(("dump_15m", f"📉 급락 감지: 15분 {chg_15m:+.1f}% (가격 {p:.5f}){why_str}"))
        if chg_1h >= MOVE_1H_PCT:
            S.append(("pump_1h", f"🚀🚀 강한 급등: 1시간 {chg_1h:+.1f}%{why_str}"))
        elif chg_1h <= -MOVE_1H_PCT:
            S.append(("dump_1h", f"📉📉 강한 급락: 1시간 {chg_1h:+.1f}%{why_str}"))
        rng = [abs(cs[i]["h"] - cs[i]["l"]) for i in range(max(0, len(cs)-15), len(cs)-1)]
        if rng:
            avg_rng = avg(rng)
            last_rng = abs(cs[-1]["h"] - cs[-1]["l"])
            if (avg_rng > 0 and last_rng > avg_rng * MOVE_ATR_MULT
                    and last_rng > p * 0.015):  # 최소 절대폭 1.5% (초저변동 오탐 방지)
                direction = "상방" if cs[-1]["c"] >= cs[-1]["o"] else "하방"
                S.append(("atr_spike", f"⚡ 변동성 폭발: 평소의 {last_rng/avg_rng:.1f}배 캔들 ({direction})"))

    if s30 < 0 and s10 > s30 * 0.3:
        S.append(("cvd_ease", f"📉→😐 매도세 둔화: CVD 기울기 30봉 {s30:,.0f} → 10봉 {s10:,.0f}"))

    if funding is not None and funding < 0:
        S.append(("fund_neg", f"🔻 펀딩비 음수: {funding:.4%} — 숏 우세"))
    if cgd.get("oi_chg_24h") is not None:
        oi_chg = cgd["oi_chg_24h"]
        if funding is not None and funding < -0.0005 and oi_chg > 15:
            S.append(("squeeze", f"🔥 숏 과밀: 펀딩 {funding:.4%} + 집계OI +{oi_chg:.0f}% — 스퀴즈 조건"))
        if oi_chg > 15:
            S.append(("oi_surge", f"📈 집계 OI 급증: 24h +{oi_chg:.0f}%"))
    elif funding is not None and oi and prev.get("oi"):
        oi_chg = (oi / prev["oi"] - 1) * 100
        if funding < -0.0005 and oi_chg > 15:
            S.append(("squeeze", f"🔥 숏 과밀: 펀딩 {funding:.4%} + OI +{oi_chg:.0f}% — 스퀴즈 조건"))
        if oi_chg > 15:
            S.append(("oi_surge", f"📈 OI 급증: +{oi_chg:.0f}%"))

    if r is not None:
        if r < RSI_LOW:
            S.append(("rsi_low", f"🧊 RSI 과매도: {r:.0f} (보정 기준 {RSI_LOW:.0f})"))
        elif r > RSI_HIGH:
            S.append(("rsi_high", f"♨️ RSI 과열: {r:.0f} (보정 기준 {RSI_HIGH:.0f})"))

    if mh[-2] <= 0 < mh[-1]:
        S.append(("macd_flip", "🟢 MACD 히스토그램 양전환"))
    if vol_ratio > 4:
        S.append(("vol_spike", f"💥 거래량 스파이크: 평균의 {vol_ratio:.1f}배"))

    if liq and prev.get("liq"):
        liq_chg = (liq / prev["liq"] - 1) * 100
        if liq_chg < -20:
            S.append(("liq_drop", f"🩸 DEX 유동성 급감: {liq_chg:.0f}%"))

    # #8 펀딩 극단 카운트다운 (8h마다 정산)
    _fc = funding if funding is not None else cgd.get("funding_w")
    if _fc is not None and abs(_fc) > 0.001:
        import datetime as _dt
        _u = _dt.datetime.utcnow()
        _mins = (8 - _u.hour % 8) * 60 - _u.minute
        if 0 <= _mins <= 30:
            _side = "숏→롱 지급" if _fc > 0 else "롱→숏 지급"
            S.append(("funding_countdown",
                      f"⏰ {_mins}분 후 펀딩 정산: {_fc:.3%} ({_side}, "
                      f"{'상방 스퀴즈' if _fc<0 else '하방 압력'} 창구)"))

    # 청산 스파이크 (코인글래스)
    if cgd.get("liq_long_now") is not None:
        if cgd["liq_long_now"] > cgd["liq_long_avg"] * 5 and cgd["liq_long_now"] > 50_000:
            S.append(("liq_long", f"💀 롱 대량청산: 1h {usd_short(cgd['liq_long_now'])} (평균의 {cgd['liq_long_now']/cgd['liq_long_avg']:.0f}배) — 바닥 근접 신호일 수 있음"))
        if cgd["liq_short_now"] > cgd["liq_short_avg"] * 5 and cgd["liq_short_now"] > 50_000:
            S.append(("liq_short", f"⚡ 숏 대량청산: 1h {usd_short(cgd['liq_short_now'])} — 스퀴즈 진행 중"))

    if hl and hl["funding"] < -0.0005:
        S.append(("hl_fund", f"🌊 HL 펀딩 깊은 음수: {hl['funding']:.4%}"))

    status = (f"가격 {p:.5f} | RSI {r:.0f} | 펀딩 "
              f"{funding:.4%}" if funding is not None else f"가격 {p:.5f}")
    metrics = {"p": p, "rsi": r, "s10": s10, "s30": s30,
               "mh": mh[-1], "mh_prev": mh[-2], "funding": funding}
    return S, status, {"oi": oi, "liq": liq, "price": p}, metrics


def usd_short(v):
    """$1,234,567 → $1.2M / $720K / $950"""
    v = float(v)
    if abs(v) >= 1e9:
        return f"${v/1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v/1e6:.2f}M"
    if abs(v) >= 1e3:
        return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


# ── 종합 편향 점수 ───────────────────────────────────
def compute_bias(m, cgd, team_sold, liq_chg):
    """모든 증거를 -100~+100 점수로 합산. 예측이 아니라 증거의 저울."""
    score, pos, neg = 0, [], []

    def add(v, why):
        nonlocal score
        score += v
        (pos if v > 0 else neg).append(why)

    # 가격 위치
    if m["p"] < STOP_LEVEL:
        add(-30, "손절선 이탈")
    elif m["p"] < SUPPORT_LEVEL:
        add(-12, "박스 하단 테스트")

    # 테이커 흐름
    if m["s30"] < 0 and m["s10"] > m["s30"] * 0.3:
        add(15, "매도세 둔화")
    elif m["s10"] < m["s30"] < 0:
        add(-10, "매도세 가속")
    elif m["s30"] > 0 and m["s10"] > 0:
        add(8, "테이커 순매수 지속")

    # 펀딩 × OI
    f, oc = m.get("funding"), cgd.get("oi_chg_24h")
    if f is not None and oc is not None:
        if f < -0.0005 and oc > 15:
            add(18, "숏 과밀(스퀴즈 연료)")
        elif f > 0 and oc > 15:
            add(10, "신규 롱 유입")
        elif oc < -15:
            add(-8, "포지션 이탈")
    if f is not None and f > 0.001:
        add(-8, "롱 과열 펀딩")

    # 청산
    if cgd.get("liq_short_now", 0) > cgd.get("liq_short_avg", 1e9) * 5:
        add(14, "숏 대량청산(스퀴즈 진행)")
    if cgd.get("liq_long_now", 0) > cgd.get("liq_long_avg", 1e9) * 5:
        add(-12, "롱 투매 진행")

    # 롱/숏 비율 (역발상: 쏠림의 반대)
    g = cgd.get("ls_global")
    if g:
        if g < 0.8:
            add(8, f"개미 숏 쏠림({g:.2f})")
        elif g > 1.6:
            add(-8, f"개미 롱 쏠림({g:.2f})")
    t = cgd.get("ls_top")
    if t:
        if t > 1.2:
            add(10, f"탑트레이더 롱 우위({t:.2f})")
        elif t < 0.8:
            add(-10, f"탑트레이더 숏 우위({t:.2f})")

    # 선물/현물 구조
    fv, sv = cgd.get("fut_vol_24h"), cgd.get("spot_vol_24h")
    if fv and sv and sv > 0:
        fs = fv / sv
        if fs > 8:
            add(-8, f"선물 과열(F/S {fs:.0f}배)")
        elif fs < 2:
            add(6, f"현물 주도(F/S {fs:.1f}배)")
    sd = cgd.get("spot_delta_24h")
    if sd is not None and sv:
        if sd > sv * 0.1:
            add(10, "현물 순매수 우세(실수요)")
        elif sd < -sv * 0.1:
            add(-10, "현물 순매도 우세")

    # TA
    if m["rsi"] < RSI_LOW:
        add(8, f"RSI 과매도({m['rsi']:.0f})")
    elif m["rsi"] > RSI_HIGH:
        add(-8, f"RSI 과열({m['rsi']:.0f})")
    if m["mh"] > 0 and m["mh"] > m["mh_prev"]:
        add(8, "MACD 상승 확대")
    elif m["mh"] < 0 and m["mh"] < m["mh_prev"]:
        add(-8, "MACD 하락 확대")

    # 온체인
    if team_sold:
        add(-25, "팀 클러스터 매도 감지")
    if liq_chg is not None:
        if liq_chg < -20:
            add(-25, f"DEX 유동성 급감({liq_chg:.0f}%)")
        elif liq_chg > 10:
            add(6, "DEX 유동성 증가")

    score = max(-100, min(100, score))
    verdict = ("상승 우위 📈" if score >= 30 else
               "하락 우위 📉" if score <= -30 else "중립/혼조 ⚖️")
    return score, verdict, pos, neg


# ── 텔레그램 / 상태 ──────────────────────────────────
def send(text):
    if not TG_TOKEN or not TG_CHAT:
        print(f"[미전송] {text}")
        return
    requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                  json={"chat_id": TG_CHAT, "text": text}, timeout=10)


def check_commands(state):
    """텔레그램에서 'update' 명령이 왔는지 확인 (다음 5분 주기에 응답)"""
    if not TG_TOKEN:
        return False
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
            params={"offset": state.get("tg_offset", 0) + 1, "timeout": 0},
            timeout=10)
        updates = r.json().get("result", [])
    except Exception as e:
        print(f"[cmd] {e}")
        return False
    want = False
    for u in updates:
        state["tg_offset"] = max(state.get("tg_offset", 0), u["update_id"])
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(TG_CHAT):
            continue
        text = (msg.get("text") or "").strip().lower()
        if text in ("update", "/update", "업데이트", "/start", "status", "/status"):
            want = True
        elif text in ("spike", "/spike", "조사"):
            send("🔬 스파이크 분석 시작 — 히스토리 수집 중 (1~2분)…")
            try:
                import spike_study
                send(spike_study.run_spike_study())
            except Exception as e:
                send(f"분석 실패: {e}")
        elif text in ("train", "/train", "학습"):
            send("🎓 학습 시작 — 전체 히스토리 수집 중 (1~2분)…")
            try:
                import backtest
                _, report = backtest.run_backtest()
                send(report)
            except Exception as e:
                send(f"학습 실패: {e}")
    return want


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    with open(STATE_FILE, "w") as f:
        json.dump(st, f)


def main():
    state = load_state()
    force_digest = check_commands(state) or os.environ.get("FORCE_DIGEST") == "1"
    prev = state.get("prev", {})
    sent = state.get("sent", {})   # key → 마지막 전송 unixtime

    cs = fetch_candles()
    funding, oi = fetch_funding(), fetch_oi()
    hl = fetch_hl()
    dex = fetch_dex_liquidity()
    liq = dex["liq"] if dex else None
    pair_addr = (dex or {}).get("pair", "")
    chain_alerts = []
    if ETHERSCAN_KEY and CONTRACTS:
        all_chain = state.setdefault("chains", {})
        for _ci, (chainid, contract) in enumerate(CONTRACTS):
            if _ci > 0:
                time.sleep(0.4)  # Etherscan 초당 제한(3/sec) 회피
            cst = all_chain.setdefault(contract, {})
            if not cst.get("pair"):
                cst["pair"], cst["symbol"], chain_name = fetch_pair_for(contract)
                cst["chain_name"] = chain_name
            if not chainid:  # 시크릿에 체인 미지정 → 자동 판별값 사용
                chainid = cst.get("chainid") or CHAIN_IDS.get(cst.get("chain_name", ""), "")
                if not chainid:
                    print(f"[chain {contract[:10]}] 체인 판별 실패({cst.get('chain_name')}) — "
                          f"시크릿에 '체인ID:{contract}' 형식으로 지정 필요, 이번엔 스킵")
                    continue
            cst["chainid"] = chainid
            if not cst.get("deployer"):
                cst["deployer"] = fetch_deployer(chainid, contract)
            txs = fetch_transfers(chainid, contract, cst.get("last_block", 0) + 1)
            if not txs:
                continue
            cst["last_block"] = max(t["block"] for t in txs)
            label = cst.get("symbol") or txs[0].get("symbol") or contract[:8]
            found = analyze_chain(txs, cst, cst.get("pair", ""))
            chain_alerts += [f"[{label}] {m}" for m in found]
            # batch2 인텔 수집 (DB 있을 때만)
            if _DB:
                try:
                    import intel
                    cur_p = metrics.get("p") if metrics else None
                    intel.update_smart_money(contract, txs, cst.get("pair",""), cur_p)
                    # 자금줄: 새 매집 지갑을 팀으로 자동 분류
                    dep = cst.get("deployer","")
                    team = set(cst.get("team", []))
                    for tx in txs:
                        buyer = tx["to"]
                        if buyer != cst.get("pair","") and tx["amount"] >= 50_000:
                            if intel.trace_funding(chainid, buyer, dep, team):
                                if buyer not in team:
                                    chain_alerts.append(f"[{label}] 🕵️ 팀 알트 발견: "
                                        f"{buyer[:10]}… (자금 출처=배포자) — 팀 클러스터 편입")
                    # 홀더 분포 스냅샷 (하루 1회)
                    import time as _t
                    if _t.time() - cst.get("last_holder_snap",0) > 86400:
                        halerts = intel.snapshot_holders(chainid, contract, dep)
                        chain_alerts += [f"[{label}] {m}" for m in halerts]
                        cst["last_holder_snap"] = _t.time()
                except Exception as _e:
                    print(f"[intel] {_e}")
            print(f"[chain {label}] 전송 {len(txs)}건, 이상 {len(found)}건")
    cgd = fetch_cg_bundle()
    if cgd:
        print(f"[CG] {list(cgd.keys())}")

    signals, status, new_prev, metrics = build_signals(cs, funding, oi, hl, liq, prev, cgd)

    # ── 종합 리포트: 판정 바뀌거나 6시간마다 ──
    team_sold = any("🚨" in m for m in chain_alerts)
    liq_chg = ((liq / prev["liq"] - 1) * 100
               if liq and prev.get("liq") else None)
    score, verdict, pos_f, neg_f = compute_bias(metrics, cgd, team_sold, liq_chg)
    now0 = int(time.time())
    if (force_digest or verdict != state.get("last_verdict")
            or now0 - state.get("last_digest", 0) > 6 * 3600):
        lines = [f"📊 종합 리포트 — {verdict} (점수 {score:+d}/100)"]
        # 가격
        lines.append(f"💲 가격 {metrics['p']:.5f} | RSI {metrics['rsi']:.0f}")

        # OI + 변화 + 서지 판독
        if cgd.get("oi_now") is not None:
            oi_line = f"📊 OI {usd_short(cgd['oi_now'])}"
            oc = cgd.get("oi_chg_24h")
            if oc is not None:
                oi_line += f" ({oc:+.0f}% 24h)"
            lines.append(oi_line)
            # 서지 가능성 판독: OI 방향 + 펀딩 + 청산으로 추론
            f = cgd.get("funding_w")
            surge = None
            if oc is not None and oc > 20 and f is not None:
                if f < -0.0003:
                    surge = "⚡ 서지 가능성↑: OI 급증 + 펀딩 음수 → 숏 쌓임, 상방 스퀴즈 연료"
                elif f > 0.0005:
                    surge = "⚡ 변동성↑ 주의: OI 급증 + 펀딩 과열 → 롱 과밀, 하방 청산 위험"
                else:
                    surge = "⚡ 변동성 확대 조짐: OI 급증(새 포지션 유입) — 방향은 아직 중립"
            elif oc is not None and oc < -20:
                surge = "💤 서지 가능성↓: OI 감소(포지션 이탈) — 관망 국면"
            if surge:
                lines.append(surge)

        # 거래량 (현물/선물)
        vol_bits = []
        if cgd.get("spot_vol_24h"):
            vol_bits.append(f"현물 {usd_short(cgd['spot_vol_24h'])}")
        if cgd.get("fut_vol_24h"):
            vol_bits.append(f"선물 {usd_short(cgd['fut_vol_24h'])}")
        if cgd.get("fut_vol_24h") and cgd.get("spot_vol_24h"):
            vol_bits.append(f"F/S {cgd['fut_vol_24h']/max(cgd['spot_vol_24h'],1):.1f}배")
        if vol_bits:
            lines.append("📈 거래량 " + " | ".join(vol_bits))

        # 롱/숏
        ls_bits = []
        if cgd.get("ls_global"):
            g = cgd["ls_global"]
            ls_bits.append(f"전체 {g:.2f} ({'롱' if g>1 else '숏'} 우세)")
        if cgd.get("ls_top"):
            t = cgd["ls_top"]
            ls_bits.append(f"탑트레이더 {t:.2f} ({'롱' if t>1 else '숏'})")
        if ls_bits:
            lines.append("⚖️ 롱/숏 " + " | ".join(ls_bits))

        # 청산 (텍스트 요약 + 히트맵 링크)
        if cgd.get("liq_long_now") is not None:
            ll, ls = cgd["liq_long_now"], cgd["liq_short_now"]
            lines.append(f"💥 청산 1h: 롱 {usd_short(ll)} / 숏 {usd_short(ls)}")
        lines.append(f"🗺 청산 히트맵: coinglass.com/pro/futures/LiquidationHeatMap ({COIN})")

        # 펀딩(가중)
        if cgd.get("funding_w") is not None:
            lines.append(f"💸 펀딩(가중) {cgd['funding_w']:.4%}")

        if pos_f:
            lines.append("▲ " + ", ".join(pos_f[:5]))
        if neg_f:
            lines.append("▼ " + ", ".join(neg_f[:5]))
        lines.append("※ 예측 아님 — 현재 증거의 저울. 판단·책임은 본인에게.")
        send("\n".join(lines))
        state["last_verdict"], state["last_digest"] = verdict, now0
        print(f"[digest] {verdict} {score:+d}")
    print("상태:", status)

    now = int(time.time())
    FAST_KEYS = ("pump_15m", "dump_15m", "pump_1h", "dump_1h", "atr_spike")
    PRE_KEYS = ("pre_squeeze", "quiet_accum", "quiet_dist",
                "learned_pump", "learned_dump")
    fresh = [(k, msg) for k, msg in signals
             if now - sent.get(k, 0) > (3600 if k in FAST_KEYS
                                        else 7200 if k in PRE_KEYS
                                        else REALERT_HOURS * 3600)]
    for k, msg in fresh:
        print("ALERT:", msg)
        send(f"[BASED] {msg}\n{status}")
        sent[k] = now

    # ── #9 시나리오 엔진: 개별 신호를 플레이북 서사로 ──
    try:
        import scenario
        all_active = [(k, m) for k, m in signals]
        all_active += [(a[:12], a) for a in chain_alerts]
        scen = scenario.evaluate_scenarios(all_active)
        scen_msg = scenario.format_scenario_alert(scen)
        if scen_msg:
            # 최고 진행도 플레이북이 3막+ 진행이면 즉시, 아니면 다이제스트에만
            top = scen[0]
            skey = f"scenario_{top['name']}_{top['done']}"
            if top["done"] >= 3 and now0 - state.get("sent", {}).get(skey, 0) > 3600:
                send(scen_msg)
                state.setdefault("sent", {})[skey] = now0
                print(f"[시나리오] {top['name']} {top['done']}/{top['total']}")
    except Exception as _e:
        print(f"[scenario] {_e}")

    # ── #12 일일 브리핑 (하루 1회, UTC 0시경) ──
    try:
        import datetime as _dt
        _h = _dt.datetime.utcnow().hour
        _today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        if _h == 0 and state.get("last_brief") != _today:
            brief = [f"☀️ BASED 일일 브리핑 {_today}"]
            brief.append(status)
            if cgd.get("oi_now"):
                brief.append(f"OI {usd_short(cgd['oi_now'])} ({cgd.get('oi_chg_24h',0):+.0f}%)")
            nf = chain_state.get("netflow_hist", [])
            if nf:
                day_net = sum(nf[-min(len(nf),288):])
                brief.append(f"온체인 CEX 순유입(24h 누적): {day_net:+,.0f} "
                             f"({'매도압↑' if day_net>0 else '완화'})")
            brief.append(f"종합: {verdict} ({score:+d})")
            if scen:
                brief.append(f"주의 플레이북: {scen[0]['emoji']} {scen[0]['name']} "
                             f"{scen[0]['done']}/{scen[0]['total']}막")
            send("\n".join(brief))
            state["last_brief"] = _today
    except Exception as _e:
        print(f"[brief] {_e}")

    # 위험도순 정렬 후 상위 10건만 전송 (전체는 로그에 남음)
    PRIORITY = ["🚨", "📥", "🌊", "🐳", "👥", "👁", "🔀", "⚠️", "🏗️", "🧺", "🔪", "🔗", "📤", "🧲", "🪓", "🔄", "📉", "🐋", "👶"]
    rank = lambda m: next((i for i, e in enumerate(PRIORITY) if e in m[:14]), 99)
    for msg in chain_alerts:
        print("CHAIN:", msg)
    # #10 알림 로깅 (성적표용)
    cur_price = metrics.get("p") if metrics else None
    if _DB and cur_price:
        for k, m in signals:
            _db.log_alert(k, m, cur_price)
        for msg in chain_alerts:
            _db.log_alert(msg[:12], msg, cur_price)
        _db.grade_pending(cur_price)

    # ── 관찰지갑 시간별 요약 (1시간마다 한 방) ──
    try:
        wsum_all = {}
        for _c, _cst in all_chain.items():
            for lbl, w in _cst.get("watch_summary", {}).items():
                agg = wsum_all.setdefault(lbl, {"out":0.0,"out_n":0,"in":0.0,"in_n":0})
                for k2 in ("out","out_n","in","in_n"):
                    agg[k2] += w[k2]
        state["wallet_summary_cache"] = wsum_all  # wallets 명령용
        if ONCHAIN_PUSH == "all" and now0 - state.get("last_wallet_summary", 0) >= 3600:
            if wsum_all:
                lines = ["👁 관찰지갑 시간 요약 (지난 1시간)"]
                # 순유출 큰 순으로 정렬
                ranked = sorted(wsum_all.items(),
                                key=lambda x: -(x[1]["out"] + x[1]["in"]))
                for lbl, w in ranked[:12]:
                    bits = []
                    if w["out_n"]:
                        bits.append(f"발신 {w['out_n']}건 {w['out']:,.0f}")
                    if w["in_n"]:
                        bits.append(f"수신 {w['in_n']}건 {w['in']:,.0f}")
                    net = w["in"] - w["out"]
                    bits.append(f"순 {net:+,.0f}")
                    lines.append(f"· {lbl}: " + " / ".join(bits))
                send("\n".join(lines))
            state["last_wallet_summary"] = now0
            for _c, _cst in all_chain.items():
                _cst["watch_summary"] = {}
    except Exception as _e:
        print(f"[wsum] {_e}")

    if ONCHAIN_PUSH == "off":
        push_list = []
    elif ONCHAIN_PUSH == "critical":
        push_list = [m for m in chain_alerts if "🚨" in m]
    else:
        push_list = chain_alerts
    for msg in sorted(push_list, key=rank)[:10]:
        send(f"[BASED 온체인] {msg}")
    if len(chain_alerts) > len(push_list):
        print(f"[chain] 무음 처리 {len(chain_alerts)-len(push_list)}건 (ONCHAIN_PUSH={ONCHAIN_PUSH})")

    if not prev:  # 최초 실행 인사
        send(f"🤖 BASED 감시 시작\n{status}")

    # prev(기준값)은 12시간마다 갱신 — 변화율 비교 기준점
    if not prev or now - state.get("prev_ts", 0) > 12 * 3600:
        state["prev"] = new_prev
        state["prev_ts"] = now
    state["sent"] = sent
    save_state(state)
    print(f"신호 {len(signals)}개 / 전송 {len(fresh)}개")


if __name__ == "__main__":
    main()
