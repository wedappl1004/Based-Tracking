"""
netflow.py — #1 CEX 입출금 넷플로우 + #7 거래소간 가격 괴리
- 알려진 거래소 핫월렛으로의 순유입(입금-출금) 추적: 입금 급증 = 매도 압력
- OKX(중앙화) vs DexScreener(DEX) 가격 괴리: 차익/유동성 붕괴 조기 신호
키 불필요분(DEX)은 항상, Etherscan분은 키 있으면 작동.
"""
from check import WHALE_THRESHOLD
short = lambda a: a[:8] + "…"

# 알려진 거래소 핫월렛 (소문자). 실제 운영시 계속 확충.
# BNB/ETH 공통으로 쓰이는 대형 CEX 입금 주소들.
KNOWN_CEX = {
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance",
    "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance",
    "0x56eddb7aa87536c09ccc2793473599fd21a8b17f": "Binance",
    "0x9696f59e4d72e237be84ffd425dcad154bf96976": "Binance",
    "0x5a52e96bacdabb82fd05763e25335261b270efcb": "Binance",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit",
    "0xa7a93fd0a276fc1c0197a5b5623ed117786eed06": "Bybit",
    "0x1ab4973a48dc892cd9971ece8e01dcc7688f8f23": "OKX",
    "0x98ec059dc3adfbdd63429454aeb0c990fba4a128": "OKX",
    "0x5041ed759dd4afc3a72b8192c143f72f4724081a": "OKX",
    "0x6cc5f688a315f3dc28a7781717a9a798a59fda7b": "OKX",
    "0x0d0707963952f2fba59dd06f2b425ace40b492fe": "Gate",
    "0x1c4b70a3968436b9a0a9cf5205c787eb81bb558c": "Gate",
}


def analyze_netflow(txs, chain_state):
    """전송 스트림에서 CEX 순유입 계산 + 급증 판정"""
    alerts = []
    inflow = outflow = 0.0
    by_cex = {}
    for tx in txs:
        f, t, amt = tx["from"], tx["to"], tx["amount"]
        if t in KNOWN_CEX:                 # 거래소로 입금 = 매도 대기
            inflow += amt
            by_cex[KNOWN_CEX[t]] = by_cex.get(KNOWN_CEX[t], 0) + amt
        if f in KNOWN_CEX:                 # 거래소에서 출금 = 반출/보관
            outflow += amt
    net = inflow - outflow

    hist = chain_state.setdefault("netflow_hist", [])
    if len(hist) >= 6:
        base = sorted(abs(x) for x in hist)[len(hist)//2] or 1e-9
        if net > WHALE_THRESHOLD and net > base * 3:
            top = max(by_cex, key=by_cex.get) if by_cex else "?"
            alerts.append(f"📥 CEX 순입금 급증: +{net:,.0f} (주로 {top}) — "
                          f"매도 압력 유입 신호 (평소의 {net/base:.0f}배)")
        elif net < -WHALE_THRESHOLD and abs(net) > base * 3:
            alerts.append(f"📤 CEX 순출금 급증: {net:,.0f} — "
                          f"거래소서 반출(보관/스테이킹), 매도압 완화 신호")
    hist.append(net)
    chain_state["netflow_hist"] = hist[-48:]
    return alerts


def check_spread(cex_price, dex_price):
    """#7 CEX vs DEX 가격 괴리"""
    if not cex_price or not dex_price:
        return None
    diff = (dex_price / cex_price - 1) * 100
    if abs(diff) >= 2:
        where = "DEX가 비쌈(현물 매수세/유동성 얕음)" if diff > 0 else \
                "DEX가 쌈(DEX 매도세/CEX 펌프)"
        return (f"🔀 거래소간 괴리: DEX vs CEX {diff:+.1f}% — {where}")
    return None
