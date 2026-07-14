"""
backtest.py — BASED 상장 이후 전체 히스토리 학습/보정
1) OKX에서 상장 시점부터 전체 캔들 수집 (1H)
2) 각 신호 규칙의 과거 성적 평가 (발동 후 6h/24h/72h 수익률, 승률 vs 기준선)
3) BASED 전용 임계값 산출 → params.json 저장 (봇이 자동 반영)
실행: 텔레그램에서 'train' / python3 backtest.py
"""
import json
import statistics as st
import time

import requests

from check import (COIN, OKX, rsi, macd_hist, cvd_series, avg,
                   usd_short, send)

BAR = "1H"
PARAMS_FILE = "params.json"


def fetch_full_history():
    """상장 시점까지 페이지네이션으로 전체 캔들 수집"""
    all_rows, after = [], ""
    for inst in (f"{COIN}-USDT-SWAP", f"{COIN}-USDT"):
        all_rows, after = [], ""
        for _ in range(60):  # 최대 60페이지 × 100 = 6000캔들 (~8개월 1H)
            try:
                params = {"instId": inst, "bar": BAR, "limit": "100"}
                if after:
                    params["after"] = after
                r = requests.get(f"{OKX}/api/v5/market/history-candles",
                                 params=params, timeout=15)
                rows = r.json().get("data") or []
                if not rows:
                    break
                all_rows += rows
                after = rows[-1][0]
                time.sleep(0.15)
            except Exception as e:
                print(f"[hist] {e}")
                break
        if len(all_rows) >= 200:
            break
    all_rows.sort(key=lambda r: int(r[0]))
    return [{"ts": int(r[0]), "o": float(r[1]), "h": float(r[2]),
             "l": float(r[3]), "c": float(r[4]), "v": float(r[5])}
            for r in all_rows]


def forward_returns(closes, idx, horizons=(6, 24, 72)):
    out = {}
    for h in horizons:
        if idx + h < len(closes):
            out[h] = (closes[idx + h] / closes[idx] - 1) * 100
    return out


def evaluate_signal(name, fire_indices, closes):
    """신호 발동 후 수익률 통계"""
    if not fire_indices:
        return None
    stats = {}
    for h in (6, 24, 72):
        rets = [forward_returns(closes, i).get(h) for i in fire_indices]
        rets = [r for r in rets if r is not None]
        if not rets:
            continue
        stats[h] = {"n": len(rets),
                    "median": st.median(rets),
                    "win": sum(1 for r in rets if r > 0) / len(rets) * 100}
    return {"name": name, "fires": len(fire_indices), "stats": stats}


def run_backtest():
    cs = fetch_full_history()
    if len(cs) < 200:
        return None, f"히스토리 부족 ({len(cs)}캔들) — OKX 상장 데이터 확인 필요"

    closes = [c["c"] for c in cs]
    lows = [c["l"] for c in cs]
    vols = [c["v"] for c in cs]
    n = len(cs)
    days = (cs[-1]["ts"] - cs[0]["ts"]) / 86400000

    # ── 지표 시계열 ──
    rsi_series = []
    for i in range(n):
        rsi_series.append(rsi(closes[:i + 1]) if i >= 15 else None)
    mh = macd_hist(closes)
    cvd = cvd_series(cs)

    # ── 기준선: 아무 때나 진입했을 때의 평균 성적 ──
    base = evaluate_signal("기준선(무작위)", list(range(50, n - 72, 24)), closes)

    # ── 신호별 발동 시점 수집 ──
    results = [base]
    sig_defs = {}

    # RSI 과매도/과열 (제네릭 30/70)
    sig_defs["RSI<30 (과매도)"] = [i for i in range(30, n - 72)
                                   if rsi_series[i] and rsi_series[i] < 30
                                   and (not rsi_series[i-1] or rsi_series[i-1] >= 30)]
    sig_defs["RSI>70 (과열)→하락베팅"] = [i for i in range(30, n - 72)
                                          if rsi_series[i] and rsi_series[i] > 70
                                          and (not rsi_series[i-1] or rsi_series[i-1] <= 70)]
    # MACD 양전환
    sig_defs["MACD 양전환"] = [i for i in range(30, n - 72)
                               if mh[i] > 0 >= mh[i - 1]]
    # 거래량 스파이크
    sig_defs["거래량 4배 스파이크"] = [
        i for i in range(30, n - 72)
        if vols[i] > 4 * (avg(vols[max(0, i-20):i]) or 1e-9)]
    # CVD 둔화 (30봉 음수인데 10봉이 크게 완화)
    def cvd_slope(i, k):
        seg = [cvd[j] - cvd[j-1] for j in range(max(1, i-k+1), i+1)]
        return avg(seg)
    sig_defs["CVD 매도세 둔화"] = [
        i for i in range(40, n - 72)
        if cvd_slope(i, 30) < 0 and cvd_slope(i, 10) > cvd_slope(i, 30) * 0.3
        and not (cvd_slope(i-1, 30) < 0 and cvd_slope(i-1, 10) > cvd_slope(i-1, 30) * 0.3)]

    for name, idxs in sig_defs.items():
        r = evaluate_signal(name, idxs, closes)
        if r:
            results.append(r)

    # ── BASED 전용 임계값 보정 ──
    rsi_vals = sorted(v for v in rsi_series if v is not None)
    q = lambda arr, p: arr[int(len(arr) * p)]
    recent_lows = sorted(lows[-24 * 30:] if n > 24 * 30 else lows)
    params = {
        "RSI_LOW": round(q(rsi_vals, 0.10), 0),    # 이 토큰 기준 하위 10%
        "RSI_HIGH": round(q(rsi_vals, 0.90), 0),
        "SUPPORT_LEVEL": round(q(recent_lows, 0.10), 5),  # 최근 30일 저가 하위 10%
        "STOP_LEVEL": round(q(recent_lows, 0.02), 5),     # 하위 2% (극단 이탈)
        "trained_at": int(time.time()),
        "candles": n, "days": round(days),
    }
    with open(PARAMS_FILE, "w") as f:
        json.dump(params, f)

    # ── 리포트 생성 ──
    lines = [f"🎓 학습 완료 — {n}캔들 / 약 {days:.0f}일 히스토리 분석"]
    lines.append(f"기간 수익률: {(closes[-1]/closes[0]-1)*100:+.0f}% "
                 f"(고점 대비 {(closes[-1]/max(closes)-1)*100:+.0f}%)")
    lines.append("")
    lines.append("📋 신호 성적표 (발동→24h 후, 승률/중앙값):")
    for r in results:
        s24 = r["stats"].get(24)
        if not s24:
            continue
        lines.append(f"· {r['name']}: {r['fires']}회 발동 → "
                     f"승률 {s24['win']:.0f}% / 중앙값 {s24['median']:+.1f}%")
    lines.append("")
    lines.append("🔧 BASED 전용 보정값 (자동 적용됨):")
    lines.append(f"· RSI 과매도 {params['RSI_LOW']:.0f} / 과열 {params['RSI_HIGH']:.0f} "
                 f"(제네릭 30/70 대체)")
    lines.append(f"· 지지선 {params['SUPPORT_LEVEL']} / 손절선 {params['STOP_LEVEL']} "
                 f"(최근 30일 분포 기준)")
    lines.append("")
    lines.append("※ 과거 성적 ≠ 미래 보장. 승률이 기준선보다 높은 신호에 가중치를 두되, "
                 "표본이 적은 신호(10회 미만)는 참고만.")
    return params, "\n".join(lines)


if __name__ == "__main__":
    params, report = run_backtest()
    print(report)
    if params:
        send(report)
