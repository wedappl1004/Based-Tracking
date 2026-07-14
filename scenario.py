"""
scenario.py — #9 복합 시나리오 엔진
개별 신호들을 '플레이북'으로 엮어서, 지금 어떤 각본의 몇 막이 진행 중인지 판정.
20개 알림 대신 하나의 서사 알림.
"""

# 각 플레이북: 단계별로 어떤 시그널 키가 있으면 '충족'인지
PLAYBOOKS = {
    "덤핑준비 (분배→투척)": {
        "emoji": "🔴",
        "stages": [
            ("팀/물량 축적", ["quiet_dist", "🏗️", "🧲"]),
            ("거래소 유입", ["📥"]),
            ("대량 이동", ["🌊", "🐳"]),
            ("실제 매도", ["🚨", "📉", "dump_15m", "dump_1h"]),
        ],
        "warn": "덤핑 시퀀스 진행 — 각 단계 진행 시 이탈 대비",
    },
    "펌프준비 (매집→점화)": {
        "emoji": "🟢",
        "stages": [
            ("조용한 매집", ["quiet_accum", "🧺"]),
            ("변동성 압축+OI", ["pre_squeeze", "🧨"]),
            ("숏 과밀(연료)", ["squeeze", "fund_neg"]),
            ("점화", ["⚡", "pump_15m", "pump_1h", "liq_short"]),
        ],
        "warn": "펌프 시퀀스 진행 — 스퀴즈 연료 축적 중",
    },
    "러그 (팀 이탈)": {
        "emoji": "💀",
        "stages": [
            ("팀 물량 분배", ["🏗️"]),
            ("신규 지갑 확산", ["👥", "🪓", "🔗"]),
            ("유동성/팀 매도", ["🩸", "🚨"]),
        ],
        "warn": "러그 패턴 — 최우선 경계",
    },
}


def evaluate_scenarios(active_keys_and_msgs):
    """active: [(key, msg), ...] → 진행 중인 플레이북 서사 리스트"""
    # 매칭용: 키 문자열 + 메시지 이모지 둘 다 검사
    tokens = set()
    for k, m in active_keys_and_msgs:
        tokens.add(k)
        if m:
            tokens.add(m[:2])  # 앞 이모지
            tokens.add(m[:1])
    out = []
    for name, pb in PLAYBOOKS.items():
        done = []
        for label, keys in pb["stages"]:
            hit = any(t in tokens for key in keys for t in (key, key[:2], key[:1]))
            # 정확 매칭: 키가 이모지면 이모지로, 텍스트면 텍스트로
            hit = False
            for key in keys:
                if key in tokens:
                    hit = True
                    break
                # 이모지 프리픽스 매칭
                for _, m in active_keys_and_msgs:
                    if m and m.startswith(key):
                        hit = True
                        break
                if hit:
                    break
            done.append((label, hit))
        n_done = sum(1 for _, h in done if h)
        if n_done >= 2:  # 2단계 이상 충족해야 서사로 인정
            stage_str = " → ".join(
                f"{'✅' if h else '⬜'}{label}" for label, h in done)
            out.append({
                "name": name, "emoji": pb["emoji"],
                "done": n_done, "total": len(done),
                "stage_str": stage_str, "warn": pb["warn"],
            })
    out.sort(key=lambda x: -x["done"])
    return out


def format_scenario_alert(scenarios):
    if not scenarios:
        return None
    lines = ["🎬 시나리오 감지 — 진행 중인 플레이북:"]
    for s in scenarios:
        lines.append(f"{s['emoji']} {s['name']} [{s['done']}/{s['total']}막]")
        lines.append(f"   {s['stage_str']}")
        lines.append(f"   → {s['warn']}")
    return "\n".join(lines)
