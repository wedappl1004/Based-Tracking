"""
spike_study.py — 스파이크 사건 법의학 분석 (예: 0.32 고점)
전체 히스토리에서 최고점 이벤트를 찾아 시세조종 지문 체크리스트를 실데이터로 검증.
텔레그램 명령: spike / 조사
"""
import time

from check import COIN, cg, send, usd_short, avg
from backtest import fetch_full_history


def pct(a, b):
    return (a / b - 1) * 100 if b else 0


def run_spike_study():
    cs = fetch_full_history()
    if len(cs) < 100:
        return "히스토리 부족 — 분석 불가"

    highs = [c["h"] for c in cs]
    closes = [c["c"] for c in cs]
    vols = [c["v"] for c in cs]
    peak_i = highs.index(max(highs))
    peak = cs[peak_i]
    t0 = time.strftime("%Y-%m-%d %H:%M", time.gmtime(peak["ts"] / 1000))

    # 분석 윈도우: 고점 전 72캔들 / 후 72캔들
    pre = cs[max(0, peak_i - 72):peak_i]
    post = cs[peak_i + 1:peak_i + 73]
    base_vol = avg(vols[max(0, peak_i - 240):max(1, peak_i - 72)]) or 1e-9

    checks = []  # (통과여부, 설명)

    # ── 지문 1: 급등 속도 (며칠 만에 몇 %?) ──
    if pre:
        ramp = pct(peak["h"], pre[0]["c"])
        fast = ramp > 80
        checks.append((fast, f"급등 속도: 72시간 내 {ramp:+.0f}% "
                       f"({'비정상적 수직 상승' if fast else '점진적'})"))

    # ── 지문 2: 고점 캔들 윗꼬리 (매수세가 순식간에 증발?) ──
    rng = peak["h"] - peak["l"]
    wick = (peak["h"] - max(peak["o"], peak["c"])) / rng if rng else 0
    checks.append((wick > 0.5,
                   f"고점 윗꼬리 비율: {wick:.0%} "
                   f"({'찌르고 즉시 붕괴 — 유동성 사냥 패턴' if wick > 0.5 else '보통'})"))

    # ── 지문 3: 급등 중 거래량 vs 평상시 ──
    pump_vol = avg([c["v"] for c in pre[-24:]]) if pre else 0
    vol_x = pump_vol / base_vol
    checks.append((vol_x > 5,
                   f"급등 구간 거래량: 평상시의 {vol_x:.1f}배 "
                   f"({'인위적 물량 투입 가능' if vol_x > 5 else '자연 수급 범위'})"))

    # ── 지문 4: 붕괴 속도 (분산 완료 후 지지 포기?) ──
    if post:
        crash_24 = pct(post[min(23, len(post)-1)]["c"], peak["c"])
        checks.append((crash_24 < -30,
                       f"고점 후 24시간: {crash_24:+.0f}% "
                       f"({'지지 없는 자유낙하 — 분산 완료 신호' if crash_24 < -30 else '완만한 조정'})"))

    # ── 지문 5: 고점 이후 회복 여부 (진짜 수요였다면 재도전) ──
    max_after = max((c["h"] for c in cs[peak_i+24:]), default=0)
    retest = pct(max_after, peak["h"])
    checks.append((retest < -40,
                   f"이후 최고 회복: 고점 대비 {retest:+.0f}% "
                   f"({'재도전 없음 — 수요가 가짜였다는 방증' if retest < -40 else '재도전 있었음'})"))

    # ── 지문 6: OI 팽창→붕괴 (코인글래스 일봉, 전 기간) ──
    oi_line = None
    oi = cg("/api/futures/openInterest/aggregated-history",
            symbol=COIN, interval="1d", limit=400)
    if oi:
        day_ms = 86400000
        peak_day = peak["ts"] // day_ms
        idx = None
        for i, x in enumerate(oi):
            ts = int(x.get("time") or x.get("t") or x.get("ts") or 0)
            if ts > 1e12:
                ts //= 1  # ms 그대로
            if ts // day_ms == peak_day or (ts and abs(ts - peak["ts"]) < day_ms):
                idx = i
                break
        if idx is not None and 3 <= idx <= len(oi) - 4:
            v = lambda x: float(x.get("close") or x.get("c") or 0)
            oi_pre = v(oi[idx - 3])
            oi_peak = v(oi[idx])
            oi_post = v(oi[idx + 3])
            build = pct(oi_peak, oi_pre)
            unwind = pct(oi_post, oi_peak)
            checks.append((build > 50,
                           f"OI 팽창: 고점 전 3일 {build:+.0f}% "
                           f"({'레버리지 급유입 — 인위적 연료' if build > 50 else '완만'})"))
            checks.append((unwind < -30,
                           f"OI 붕괴: 고점 후 3일 {unwind:+.0f}% "
                           f"({'포지션 대량 청산/이탈 — 터뜨림의 흔적' if unwind < -30 else '완만'})"))
            oi_line = f"고점 당시 집계 OI ≈ {usd_short(oi_peak)}"

    # ── 리포트 ──
    hits = sum(1 for ok, _ in checks if ok)
    total = len(checks)
    lines = [f"🔬 스파이크 법의학 분석 — 고점 {peak['h']:.5f} ({t0} UTC)"]
    if oi_line:
        lines.append(oi_line)
    lines.append("")
    lines.append(f"조작 지문 체크리스트: {hits}/{total} 일치")
    for ok, desc in checks:
        lines.append(f"{'🔴' if ok else '⚪'} {desc}")
    lines.append("")
    if hits >= max(4, total - 1):
        verdict = ("종합: 조작 지문과 강하게 일치. 자연스러운 수요 랠리가 아니라 "
                   "'띄우고 → 레버리지 유인 → 분산 → 방치' 시퀀스에 부합.")
    elif hits >= total // 2 + 1:
        verdict = "종합: 조작 지문 다수 일치. 최소한 극도로 투기적인 저유동성 펌프."
    else:
        verdict = "종합: 조작 단정 근거 부족. 저유동성 변동성 범위로 볼 여지."
    lines.append(verdict)
    lines.append("※ 데이터는 '무엇이 일어났는지'만 증명. '누가/왜'(의도)는 온체인 지갑 "
                 "추적과 교차해야 함 — 워치리스트가 그 역할.")
    return "\n".join(lines)


if __name__ == "__main__":
    report = run_spike_study()
    print(report)
    send(report)
