from fastapi import FastAPI, Request
from datetime import datetime, timezone, timedelta

# =========================================================
# [1] 기본 설정
# =========================================================

app = FastAPI()

KST = timezone(timedelta(hours=9))

# SMR 마지막 신호를 기준으로 20분 동안 0선 돌파 대기
WAIT_SECONDS = 20 * 60


# =========================================================
# [2] NAS / BTC 대기 상태
# =========================================================

nas_waiting = {
    "active": False,
    "direction": None,
    "timestamp": None
}

btc_waiting = {
    "active": False,
    "direction": None,
    "timestamp": None
}


# =========================================================
# [3] NAS / BTC 현재 포지션 상태
# =========================================================

nas_position = None
btc_position = None


# =========================================================
# [4] 최종 신호 기록
# =========================================================

signals_history = {
    "NAS": [],
    "BTC": []
}


# =========================================================
# [5] MT5 전송용 최종 매매 신호 생성
# =========================================================

def create_final_signal(symbol, direction):

    global nas_position
    global btc_position

    print(f"🔥 MT5 주문 신호 발생 → {symbol} / {direction}")

    # 포지션 상태 저장
    if symbol == "NAS":
        nas_position = direction
    else:
        btc_position = direction

    # 신호 기록
    signals_history[symbol].append({
        "time": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S"),
        "direction": direction
    })

    # MT5 실행용 명령 반환
    return {
        "status": "execute",
        "command": direction,  # "BUY" 또는 "SELL"
        "symbol": "NAS100" if symbol == "NAS" else "BTC",
        "timestamp": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    }


# =========================================================
# [6] MT5 전송용 청산 처리
# =========================================================

def process_close(symbol):

    global nas_position
    global btc_position

    position = nas_position if symbol == "NAS" else btc_position

    print(f"⚪ MT5 청산 신호 수신 → {symbol} (기존 포지션: {position})")

    # 포지션 초기화
    if symbol == "NAS":
        nas_position = None
    else:
        btc_position = None

    return {
        "status": "execute",
        "command": "CLOSE",
        "symbol": "NAS100" if symbol == "NAS" else "BTC",
        "previous_position": position,
        "timestamp": datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    }


# =========================================================
# [7] SMR 대기 시작 / 갱신
# =========================================================

def start_waiting(symbol, direction):

    check_timeout(symbol)

    now = datetime.now(KST)

    waiting = nas_waiting if symbol == "NAS" else btc_waiting

    if waiting["active"] and waiting["direction"] == direction:
        waiting["timestamp"] = now
        print(f"🔄 {symbol} {direction} SMR 추가 발생 - 20분 대기 갱신")
        return

    waiting["active"] = True
    waiting["direction"] = direction
    waiting["timestamp"] = now

    print(f"⏳ {symbol} {direction} 대기 시작 ({now.strftime('%H:%M:%S')} KST)")


# =========================================================
# [8] 20분 시간 초과 확인
# =========================================================

def check_timeout(symbol):

    waiting = nas_waiting if symbol == "NAS" else btc_waiting

    if not waiting["active"]:
        return False

    now = datetime.now(KST)
    elapsed = (now - waiting["timestamp"]).total_seconds()

    if elapsed >= WAIT_SECONDS:
        print(f"⌛ {symbol} {waiting['direction']} 대기시간 종료")
        waiting["active"] = False
        waiting["direction"] = None
        waiting["timestamp"] = None
        return True

    return False


# =========================================================
# [9] 0선 돌파 처리
# =========================================================

def process_zero_cross(symbol):

    waiting = nas_waiting if symbol == "NAS" else btc_waiting

    if not waiting["active"]:
        print(f"⚪ {symbol} 0선 돌파 → SMR 대기 없음. 무시")
        return {"status": "ignored", "reason": "no_smr_waiting"}

    if check_timeout(symbol):
        print(f"⚪ {symbol} 0선 돌파 → 20분 초과. 무시")
        return {"status": "ignored", "reason": "waiting_expired"}

    direction = waiting["direction"]

    # 대기 상태 초기화
    waiting["active"] = False
    waiting["direction"] = None
    waiting["timestamp"] = None

    # MT5 주문 실행 객체 생성 및 반환
    return create_final_signal(symbol, direction)


# =========================================================
# [10] TradingView 웹훅 (MT5 직접 연동용)
# =========================================================

@app.post("/webhook")
async def webhook(request: Request):

    body = await request.body()
    message = body.decode("utf-8", errors="ignore").strip()

    print("\n==============================")
    print("📩 TradingView 수신")
    print(message)
    print("==============================")

    clean_message = message.replace(" ", "").upper()

    # -----------------------------------------------------
    # NAS
    # -----------------------------------------------------
    if "NAS" in clean_message:

        if "청산" in clean_message:
            return process_close("NAS")

        if "0선돌파" in clean_message or "0선" in clean_message:
            return process_zero_cross("NAS")

        if "지지구간" in clean_message:
            start_waiting("NAS", "BUY")
            return {"status": "waiting", "symbol": "NAS", "direction": "BUY"}

        if "저항구간" in clean_message:
            start_waiting("NAS", "SELL")
            return {"status": "waiting", "symbol": "NAS", "direction": "SELL"}

        return {"status": "ignored", "reason": "NAS_unknown_signal"}

    # -----------------------------------------------------
    # BTC
    # -----------------------------------------------------
    if "BTC" in clean_message:

        if "청산" in clean_message:
            return process_close("BTC")

        if "0선돌파" in clean_message or "0선" in clean_message:
            return process_zero_cross("BTC")

        if "지지구간" in clean_message:
            start_waiting("BTC", "BUY")
            return {"status": "waiting", "symbol": "BTC", "direction": "BUY"}

        if "저항구간" in clean_message:
            start_waiting("BTC", "SELL")
            return {"status": "waiting", "symbol": "BTC", "direction": "SELL"}

        return {"status": "ignored", "reason": "BTC_unknown_signal"}

    return {"status": "ignored", "reason": "no_symbol_tag"}


# =========================================================
# [11] MT5 조회 / 상태 체크 엔드포인트
# =========================================================

@app.get("/position/{symbol}")
def get_position(symbol: str):

    symbol = symbol.upper()
    position = nas_position if symbol == "NAS" else btc_position if symbol == "BTC" else None

    return {"symbol": symbol, "position": position}


@app.get("/signal/{symbol}")
def get_signal(symbol: str):

    symbol = symbol.upper()

    if symbol not in signals_history:
        return {"status": "error", "reason": "unknown_symbol"}

    return {"symbol": symbol, "signals": signals_history[symbol]}
