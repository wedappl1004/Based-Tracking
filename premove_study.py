"""
premove_study.py — 급등/급락 '직전' 전조 패턴 부검
전체 히스토리에서 ±15%/24h 이벤트를 찾아, 직전 24시간에 어떤 전조가 있었는지
기저율(평상시 출현률) 대비 몇 배였는지(리프트)까지 계산.
텔레그램 명령: premove / 전조분석
"""
import json
import time

from check import COIN, rsi, cvd_series, avg, send
from backtest import fetch_full_history

PREMOVE_FILE = "premove_params.json"

MOVE_PCT = 15.0     # 이벤트 기준: 24캔들(1H) 내 ±15%
PRE_WIN = 24        # 직전 관찰 창


def detect_events(cs):
    """급등/급락 시작점 찾기 (겹침 제거)"""
    closes = [c["c"] for c in cs]
    pumps, dumps, i = [], [], PRE_WIN + 40
    while i < len(cs) - 24:
        fwd_max = max(closes[i:i + 24])
        fwd_min = min(closes[i:i + 24])
        up = (fwd_max / closes[i] - 1) * 100
        dn = (fwd_min / closes[i] - 1) * 100
        if up >= MOVE_PCT:
            pumps.append(i)
            i += 24
        elif dn <= -MOVE_PCT:
            dumps.append(i)
            i += 24
        else:
            i += 1
    return pumps, dumps


def precursors_at(cs, i):
    """i 시점 직전 24캔들의 전조 상태 반환"""
    pre = cs[i - PRE_WIN:i]
    base = cs[i - PRE_WIN - 36:i - PRE_WIN]
    closes = [c["c"] for c in cs[:i]]
    out = {}

    # 1) 변동성 압축: 직전 12캔들 평균 폭 < 그 앞 기준의 55%
    r_recent = avg([(c["h"] - c["l"]) / c["c"] for c in pre[-12:]])
    r_base = avg([(c["h"] - c["l"]) / c["c"] for c in base]) if base else 0
    out["압축"] = bool(r_base and r_recent < r_base * 0.55)

    # 2) 조용한 매집/분산: CVD 근사 기울기 (직전 24캔들)
    cvd = cvd_series(pre)
    slope = (cvd[-1] - cvd[0]) / max(len(cvd), 1)
    vol_avg = avg([c["v"] for c in pre]) or 1e-9
    out["매집"] = slope > vol_avg * 0.05
    out["분산"] = slope < -vol_avg * 0.05

    # 3) 거래량 고갈: 직전 12캔들 볼륨 < 기준의 60%
    v_recent = avg([c["v"] for c in pre[-12:]])
    v_base = avg([c["v"] for c in base]) if base else 0
    out["거래량고갈"] = bool(v_base and v_recent < v_base * 0.6)

    # 4) RSI 상태
    r = rsi(closes)
    out["RSI과매도"] = r is not None and r < 35
    out["RSI과열"] = r is not None and r > 65
    return out


def run_premove_study():
    cs = fetch_full_history()
    if len(cs) < 300:
        return f"히스토리 부족 ({len(cs)}캔들)"

    pumps, dumps = detect_events(cs)
    keys = ["압축", "매집", "분산", "거래량고갈", "RSI과매도", "RSI과열"]

    def rate(indices):
        cnt = {k: 0 for k in keys}
        for i in indices:
            pc = precursors_at(cs, i)
            for k in keys:
                cnt[k] += pc[k]
        n = max(len(indices), 1)
        return {k: cnt[k] / n * 100 for k in cnt}

    # 기저율: 이벤트가 아닌 무작위 시점들
    import random
    random.seed(7)
    event_set = set()
    for i in pumps + dumps:
        event_set.update(range(i - 6, i + 24))
    normals = [i for i in range(PRE_WIN + 60, len(cs) - 24, 12)
               if i not in event_set]
    random.shuffle(normals)
    base_rate = rate(normals[:80])
    pump_rate = rate(pumps)
    dump_rate = rate(dumps)

    def lift_str(ev, k):
        b = base_rate[k]
        return f"{ev[k]:.0f}%" + (f" (평시의 {ev[k]/b:.1f}배)" if b > 1 else " (평시 거의 없음)")

    lines = [f"🔍 전조 부검 — {COIN} 전체 히스토리 {len(cs)}캔들",
             f"발견: 급등(+{MOVE_PCT:.0f}%/24h) {len(pumps)}회 · "
             f"급락(-{MOVE_PCT:.0f}%) {len(dumps)}회", ""]

    if pumps:
        lines.append(f"🚀 급등 직전 24h에 나타난 전조 (n={len(pumps)}):")
        for k in keys:
            if pump_rate[k] >= 20:
                lines.append(f"  · {k}: {lift_str(pump_rate, k)}")
        lines.append("")
    if dumps:
        lines.append(f"📉 급락 직전 24h에 나타난 전조 (n={len(dumps)}):")
        for k in keys:
            if dump_rate[k] >= 20:
                lines.append(f"  · {k}: {lift_str(dump_rate, k)}")
        lines.append("")

    # 핵심 결론: 리프트 2배 이상인 전조만 추림 + 실시간 감시용 저장
    strong = []
    learned = {"pump": {}, "dump": {}, "trained_at": int(time.time()),
               "n_pumps": len(pumps), "n_dumps": len(dumps)}
    for name, ev, bucket in (("급등", pump_rate, "pump"), ("급락", dump_rate, "dump")):
        for k in keys:
            b = base_rate[k]
            if ev[k] >= 30 and b > 1 and ev[k] / b >= 2:
                strong.append(f"{k}→{name} (출현 {ev[k]:.0f}%, 평시의 {ev[k]/b:.1f}배)")
                learned[bucket][k] = {"rate": round(ev[k]), "lift": round(ev[k]/b, 1)}
    with open(PREMOVE_FILE, "w") as f:
        json.dump(learned, f)
    if strong:
        lines.append("💡 통계적으로 유의미한 전조:")
        lines += [f"  ★ {s}" for s in strong]
    else:
        lines.append("💡 리프트 2배 이상의 강한 전조는 발견 안 됨 — "
                     "이 코인의 급변동은 전조 없이(뉴스/고래 단발성) 오는 비중이 큼")
    lines.append("")
    lines.append("📡 이 결과는 자동 저장됨 — 이제 같은 패턴이 실시간으로 나타나면 알림이 갑니다.")
    lines.append("※ 과거 패턴 ≠ 미래 보장. 전조 알림은 '확률 우위 구간 진입' 신호로만.")
    return "\n".join(lines)


if __name__ == "__main__":
    report = run_premove_study()
    print(report)
    send(report)
