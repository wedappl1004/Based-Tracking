"""
realtime.py — 상주형 봇 (내 컴퓨터에서 24시간 실행)
- 텔레그램 명령(update 등)에 2초 내 즉답
- 5분마다 자동 감시 사이클 (GitHub 버전과 동일한 로직 재사용)
실행: python3 realtime.py   (또는 start.command 더블클릭)
종료: Ctrl + C
"""
import json
import os
import sys
import time
import traceback

import requests

# ── 1. secrets.json 읽어서 환경변수로 (check.py가 import 시점에 읽음) ──
HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
try:
    with open("secrets.json", encoding="utf-8") as f:
        SECRETS = json.load(f)
    for k, v in SECRETS.items():
        if v and "여기에" not in str(v):
            os.environ[k] = str(v)
    print("[설정] secrets.json 사용")
except FileNotFoundError:
    print("[설정] secrets.json 없음 → 환경변수 사용 (클라우드 모드)")
except json.JSONDecodeError as e:
    print(f"secrets.json 형식 오류: {e}")
    sys.exit(1)

import check  # noqa: E402  (환경변수 설정 후에 import해야 함)

# realtime이 getUpdates를 직접 소유 → check 내부 명령 폴링은 끔 (offset 충돌 방지)
check.check_commands = lambda state: False

TG = f"https://api.telegram.org/bot{os.environ.get('TELEGRAM_BOT_TOKEN', '')}"
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
MONITOR_EVERY = 300  # 5분
COMMANDS = ("update", "/update", "status", "/status", "업데이트", "/start")

OFFSET_FILE = "tg_offset.json"


def load_offset():
    try:
        return json.load(open(OFFSET_FILE))["offset"]
    except Exception:
        return 0


def save_offset(v):
    json.dump({"offset": v}, open(OFFSET_FILE, "w"))


def send(text):
    try:
        requests.post(f"{TG}/sendMessage",
                      json={"chat_id": CHAT, "text": text}, timeout=10)
    except Exception as e:
        print(f"[send] {e}")


def run_cycle(force=False):
    """감시 1회. force=True면 종합 리포트 무조건 전송"""
    if force:
        os.environ["FORCE_DIGEST"] = "1"
    try:
        check.main()
    except Exception:
        traceback.print_exc()
    finally:
        os.environ.pop("FORCE_DIGEST", None)


def main():
    if not os.environ.get("TELEGRAM_BOT_TOKEN") or not CHAT:
        print("secrets.json에 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID를 채워주세요.")
        sys.exit(1)

    print("=" * 46)
    print(" BASED 상주 봇 시작 — 명령 즉답 + 5분 감시")
    print(" 종료: Ctrl+C  (창을 닫으면 봇도 꺼집니다)")
    print("=" * 46)
    send("🟢 상주 봇 시작 — 이제 update 명령에 즉시 답합니다")

    offset = load_offset()
    last_monitor = 0.0
    _last_weight_week = [""]

    while True:
        # ── 명령 대기 (long-poll 20초: 메시지 오면 그 즉시 반환) ──
        try:
            r = requests.get(f"{TG}/getUpdates",
                             params={"offset": offset + 1, "timeout": 20},
                             timeout=30)
            for u in r.json().get("result", []):
                offset = max(offset, u["update_id"])
                save_offset(offset)
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != str(CHAT):
                    continue
                text = (msg.get("text") or "").strip().lower()
                if text in COMMANDS:
                    print(f"[명령] {text} → 즉시 리포트")
                    send("⏳ 리포트 생성 중… (10~20초)")
                    run_cycle(force=True)
                    last_monitor = time.time()
                elif text in ("spike", "/spike", "조사"):
                    print("[명령] spike → 스파이크 분석")
                    send("🔬 스파이크 분석 시작 — 히스토리 수집 중 (1~2분)…")
                    try:
                        import spike_study
                        send(spike_study.run_spike_study())
                    except Exception as e:
                        send(f"분석 실패: {e}")
                elif text in ("wallets", "/wallets", "지갑"):
                    print("[명령] wallets → 지갑 요약")
                    try:
                        import json as _j
                        st = _j.load(open("state.json"))
                        ws = st.get("wallet_summary_cache", {})
                        if not ws:
                            send("👁 최근 집계된 관찰지갑 움직임 없음 (조용함)")
                        else:
                            lines = ["👁 관찰지갑 활동 요약 (누적)"]
                            ranked = sorted(ws.items(), key=lambda x: -(x[1]["out"]+x[1]["in"]))
                            for lbl, w in ranked[:12]:
                                bits=[]
                                if w["out_n"]: bits.append(f"발신 {w['out_n']}건 {w['out']:,.0f}")
                                if w["in_n"]: bits.append(f"수신 {w['in_n']}건 {w['in']:,.0f}")
                                bits.append(f"순 {w['in']-w['out']:+,.0f}")
                                lines.append(f"· {lbl}: " + " / ".join(bits))
                            send("\n".join(lines))
                    except Exception as e:
                        send(f"지갑 요약 실패: {e}")
                elif text in ("score", "/score", "성적", "성적표"):
                    print("[명령] score → 성적표")
                    try:
                        import db as _db
                        sc = _db.scorecard(days=30)
                        if not sc:
                            send("📋 아직 채점된 알림이 없어요 (알림 발생 후 24시간 지나야 집계). "
                                 f"저장소: {'Postgres' if _db.is_postgres() else '파일'}")
                        else:
                            base = sc.get("_baseline", {}).get("avg", 0)
                            lines = [f"📋 신호 성적표 (30일) · 기준선 {base:+.1f}%/24h"]
                            ranked = sorted([(k,v) for k,v in sc.items() if k!="_baseline"],
                                            key=lambda x:-x[1].get("lift",0))
                            for kind, d in ranked:
                                if d["n"] >= 1:
                                    tag = "🟢" if d.get("lift",0)>0 else "🔴"
                                    lines.append(f"{tag} {kind}: {d['n']}회, 적중 {d.get('acc',0):.0f}%, "
                                                 f"평균 {d.get('avg',0):+.1f}%, lift {d.get('lift',0):+.1f}%, "
                                                 f"가중 {d.get('weight',1):.1f}x")
                            # 가중치 저장 (자가학습)
                            w = _db.save_weights(sc)
                            lines.append(f"\n✅ 가중치 {len(w)}개 갱신 — 다음 리포트부터 반영")
                            lines.append(f"저장소: {'Postgres ✓' if _db.is_postgres() else '파일(임시)'}")
                            send("\n".join(lines))
                    except Exception as e:
                        send(f"성적표 실패: {e}")
                elif text in ("premove", "/premove", "전조분석", "전조"):
                    print("[명령] premove → 전조 부검")
                    send("🔍 전조 부검 시작 — 과거 급등/급락 전 패턴 분석 중 (1~2분)…")
                    try:
                        import premove_study
                        send(premove_study.run_premove_study())
                    except Exception as e:
                        send(f"분석 실패: {e}")
                elif text in ("train", "/train", "학습"):
                    print("[명령] train → 학습")
                    send("🎓 학습 시작 — 전체 히스토리 수집 중 (1~2분)…")
                    try:
                        import backtest
                        _, rep = backtest.run_backtest()
                        send(rep)
                    except Exception as e:
                        send(f"학습 실패: {e}")
                elif text:
                    send("아는 명령:\nupdate — 종합 리포트\nspike — 0.32 고점 조작 분석\ntrain — 전체 히스토리 학습\npremove — 과거 급등/급락 전조 부검\nscore — 신호 성적표 (자동 채점)\nwallets — 지갑 움직임 요약 (원할 때만)")
        except requests.exceptions.RequestException as e:
            print(f"[poll] 네트워크 문제, 10초 후 재시도: {e}")
            time.sleep(10)
        except Exception:
            traceback.print_exc()
            time.sleep(5)

        # ── 5분 주기 자동 감시 ──
        if time.time() - last_monitor >= MONITOR_EVERY:
            print(f"[감시] 정기 사이클 실행 {time.strftime('%H:%M:%S')}")
            run_cycle()
            last_monitor = time.time()
            # 주 1회 가중치 자동 재계산 (일요일)
            try:
                import datetime as _dt, db as _db
                _n = _dt.datetime.utcnow()
                if _n.weekday() == 6 and _n.hour == 1:
                    _wk = _n.strftime("%Y-%W")
                    if _last_weight_week[0] != _wk:
                        sc = _db.scorecard(30)
                        if sc:
                            w = _db.save_weights(sc)
                            send(f"🔄 주간 자가학습 완료 — 신호 가중치 {len(w)}개 갱신. "
                                 f"성적 나쁜 신호는 자동 감쇠됨. (score로 확인)")
                        _last_weight_week[0] = _wk
            except Exception as _e:
                print(f"[weekly] {_e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n봇 종료. 다시 켜려면 start.command 더블클릭 또는 python3 realtime.py")
