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
                    send("아는 명령:\nupdate — 종합 리포트\nspike — 0.32 고점 조작 분석\ntrain — 전체 히스토리 학습\npremove — 과거 급등/급락 전조 부검")
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


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n봇 종료. 다시 켜려면 start.command 더블클릭 또는 python3 realtime.py")
