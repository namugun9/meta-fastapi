from fastapi import FastAPI, Request
from datetime import datetime, timezone, timedelta
import MetaTrader5 as mt5


# =========================================================
# [1] 기본 설정
# =========================================================

app = FastAPI()

KST = timezone(timedelta(hours=9))

# =========================================================
# MT5 설정
# =========================================================

# XM MT5에서 실제 사용하는 심볼명으로 수정
NAS_SYMBOL = "US100Cash#"
BTC_SYMBOL = "BTCUSD#"

# 주문 수량 = 1계약
LOT_SIZE = 1.0

# EA 매직넘버가 아니라 Python 주문 식별용
MAGIC_NUMBER = 20260903

# 주문 허용 deviation
DEVIATION = 20

# 마지막 SMR 신호를 기준으로 20분 동안 0선 돌파 대기
WAIT_SECONDS = 20 * 60


# =========================================================
# [2] MT5 연결
# =========================================================

def initialize_mt5():

    if not mt5.initialize():

        print(
            f"❌ MT5 연결 실패: "
            f"{mt5.last_error()}"
        )

        return False

    account = mt5.account_info()

    if account is None:

        print(
            f"❌ MT5 계좌 정보 확인 실패: "
            f"{mt5.last_error()}"
        )

        return False

    print("================================")
    print("✅ MT5 연결 성공")
    print(f"계좌번호: {account.login}")
    print(f"서버: {account.server}")
    print(f"잔고: {account.balance}")
    print("================================")

    return True


# 서버 시작 시 MT5 연결
initialize_mt5()


# =========================================================
# [3] NAS / BTC 대기 상태
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
# [4] 종목명 변환
# =========================================================

def get_mt5_symbol(symbol):

    if symbol == "NAS":
        return NAS_SYMBOL

    if symbol == "BTC":
        return BTC_SYMBOL

    return None


# =========================================================
# [5] MT5 심볼 확인
# =========================================================

def prepare_symbol(symbol):

    if not mt5.initialize():

        print("❌ MT5 초기화 실패")

        return False

    info = mt5.symbol_info(symbol)

    if info is None:

        print(
            f"❌ MT5에서 종목을 찾을 수 없음: "
            f"{symbol}"
        )

        return False

    # Market Watch에 없으면 선택
    if not info.visible:

        if not mt5.symbol_select(symbol, True):

            print(
                f"❌ 종목 활성화 실패: "
                f"{symbol}"
            )

            return False

    return True


# =========================================================
# [6] 현재 MT5 포지션 확인
# =========================================================

def get_current_position(symbol):

    positions = mt5.positions_get(
        symbol=symbol
    )

    if positions is None:
        return None

    if len(positions) == 0:
        return None

    # 같은 심볼에 여러 포지션이 있을 경우
    # 첫 번째 포지션을 기준으로 처리
    position = positions[0]

    if position.type == mt5.POSITION_TYPE_BUY:

        return {
            "ticket": position.ticket,
            "direction": "BUY",
            "volume": position.volume
        }

    if position.type == mt5.POSITION_TYPE_SELL:

        return {
            "ticket": position.ticket,
            "direction": "SELL",
            "volume": position.volume
        }

    return None


# =========================================================
# [7] MT5 매수 / 매도 주문
# =========================================================

def send_mt5_order(symbol, direction):

    print(
        f"📤 MT5 주문 요청 → "
        f"{symbol} / {direction} / "
        f"{LOT_SIZE} 계약"
    )

    # -----------------------------------------------------
    # MT5 연결 확인
    # -----------------------------------------------------

    if not mt5.initialize():

        print("❌ MT5 연결 실패")

        return False

    # -----------------------------------------------------
    # 종목 준비
    # -----------------------------------------------------

    if not prepare_symbol(symbol):

        return False

    # -----------------------------------------------------
    # 현재 포지션 확인
    # -----------------------------------------------------

    current = get_current_position(symbol)

    # -----------------------------------------------------
    # 같은 방향 포지션이 이미 있으면 중복 주문 방지
    # -----------------------------------------------------

    if current is not None:

        if current["direction"] == direction:

            print(
                f"⚪ {symbol} "
                f"{direction} 포지션 이미 존재"
            )

            print(
                "→ 중복 주문하지 않음"
            )

            return False

        # 반대 포지션이 있으면 먼저 청산
        print(
            f"🔄 {symbol} 반대 포지션 존재"
        )

        if not close_mt5_position(symbol):

            print(
                "❌ 기존 반대 포지션 "
                "청산 실패"
            )

            return False

    # -----------------------------------------------------
    # 현재 가격
    # -----------------------------------------------------

    tick = mt5.symbol_info_tick(symbol)

    if tick is None:

        print(
            f"❌ 현재 가격 확인 실패: "
            f"{symbol}"
        )

        return False

    # -----------------------------------------------------
    # BUY / SELL 가격
    # -----------------------------------------------------

    if direction == "BUY":

        order_type = mt5.ORDER_TYPE_BUY
        price = tick.ask

    elif direction == "SELL":

        order_type = mt5.ORDER_TYPE_SELL
        price = tick.bid

    else:

        print(
            f"❌ 잘못된 방향: "
            f"{direction}"
        )

        return False

    # -----------------------------------------------------
    # 주문 요청
    # -----------------------------------------------------

    request = {

        "action": mt5.TRADE_ACTION_DEAL,

        "symbol": symbol,

        "volume": LOT_SIZE,

        "type": order_type,

        "price": price,

        "deviation": DEVIATION,

        "magic": MAGIC_NUMBER,

        "comment": "TV_AutoTrade",

        "type_time": mt5.ORDER_TIME_GTC,

        "type_filling": mt5.ORDER_FILLING_IOC
    }

    # -----------------------------------------------------
    # 주문 실행
    # -----------------------------------------------------

    result = mt5.order_send(request)

    if result is None:

        print(
            "❌ MT5 order_send 결과 없음"
        )

        print(
            mt5.last_error()
        )

        return False

    # -----------------------------------------------------
    # 주문 성공
    # -----------------------------------------------------

    if result.retcode == mt5.TRADE_RETCODE_DONE:

        print("================================")
        print("✅ MT5 주문 성공")
        print(f"종목: {symbol}")
        print(f"방향: {direction}")
        print(f"수량: {LOT_SIZE}")
        print(f"가격: {price}")
        print(f"Ticket: {result.order}")
        print("================================")

        return True

    # -----------------------------------------------------
    # 주문 실패
    # -----------------------------------------------------

    print("================================")
    print("❌ MT5 주문 실패")
    print(f"retcode: {result.retcode}")
    print(f"comment: {result.comment}")
    print("================================")

    return False


# =========================================================
# [8] MT5 포지션 청산
# =========================================================

def close_mt5_position(symbol):

    print(
        f"⚪ MT5 포지션 청산 요청 → "
        f"{symbol}"
    )

    if not prepare_symbol(symbol):

        return False

    positions = mt5.positions_get(
        symbol=symbol
    )

    if positions is None:

        print(
            f"❌ 포지션 조회 실패: "
            f"{mt5.last_error()}"
        )

        return False

    if len(positions) == 0:

        print(
            f"⚪ {symbol} "
            f"현재 포지션 없음"
        )

        return True

    all_closed = True

    # 같은 심볼에 여러 포지션이 있으면
    # 모두 청산
    for position in positions:

        ticket = position.ticket
        volume = position.volume

        tick = mt5.symbol_info_tick(symbol)

        if tick is None:

            print(
                f"❌ 가격 확인 실패: "
                f"{symbol}"
            )

            all_closed = False
            continue

        # BUY 포지션 청산 → SELL 주문
        if position.type == mt5.POSITION_TYPE_BUY:

            order_type = mt5.ORDER_TYPE_SELL
            price = tick.bid

        # SELL 포지션 청산 → BUY 주문
        elif position.type == mt5.POSITION_TYPE_SELL:

            order_type = mt5.ORDER_TYPE_BUY
            price = tick.ask

        else:

            continue

        request = {

            "action": mt5.TRADE_ACTION_DEAL,

            "symbol": symbol,

            "volume": volume,

            "type": order_type,

            "position": ticket,

            "price": price,

            "deviation": DEVIATION,

            "magic": MAGIC_NUMBER,

            "comment": "TV_AutoClose",

            "type_time": mt5.ORDER_TIME_GTC,

            "type_filling": mt5.ORDER_FILLING_IOC
        }

        result = mt5.order_send(request)

        if result is None:

            print(
                f"❌ 청산 결과 없음: "
                f"{mt5.last_error()}"
            )

            all_closed = False
            continue

        if result.retcode == mt5.TRADE_RETCODE_DONE:

            print(
                f"✅ {symbol} "
                f"포지션 청산 성공 "
                f"Ticket={ticket}"
            )

        else:

            print(
                f"❌ {symbol} "
                f"포지션 청산 실패"
            )

            print(
                f"retcode: "
                f"{result.retcode}"
            )

            print(
                f"comment: "
                f"{result.comment}"
            )

            all_closed = False

    return all_closed


# =========================================================
# [9] 최종 매매 신호
# =========================================================

def create_final_signal(symbol, direction):

    global nas_position
    global btc_position

    print(
        f"🔥 최종 신호 발생 → "
        f"{symbol} / {direction}"
    )

    mt5_symbol = get_mt5_symbol(symbol)

    if mt5_symbol is None:

        print(
            f"❌ MT5 종목 매핑 실패: "
            f"{symbol}"
        )

        return {
            "status": "error",
            "reason": "unknown_symbol"
        }

    # -----------------------------------------------------
    # 실제 MT5 주문
    # -----------------------------------------------------

    success = send_mt5_order(
        mt5_symbol,
        direction
    )

    # -----------------------------------------------------
    # 주문 실패
    # -----------------------------------------------------

    if not success:

        print(
            f"❌ {symbol} "
            f"{direction} 주문 실패"
        )

        return {
            "status": "order_failed",
            "symbol": symbol,
            "direction": direction
        }

    # -----------------------------------------------------
    # Python 상태 저장
    # -----------------------------------------------------

    if symbol == "NAS":

        nas_position = direction

    else:

        btc_position = direction

    # -----------------------------------------------------
    # 신호 기록
    # -----------------------------------------------------

    signals_history[symbol].append({

        "time":
            datetime.now(KST).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),

        "direction":
            direction
    })

    print(
        f"✅ {symbol} 최종 "
        f"{direction} 매매 완료"
    )

    return {
        "status": "final_signal",
        "symbol": symbol,
        "direction": direction
    }


# =========================================================
# [10] 청산 처리
# =========================================================

def process_close(symbol):

    global nas_position
    global btc_position

    mt5_symbol = get_mt5_symbol(symbol)

    if mt5_symbol is None:

        return {
            "status": "error",
            "reason": "unknown_symbol"
        }

    # -----------------------------------------------------
    # 실제 MT5 포지션 확인
    # -----------------------------------------------------

    current = get_current_position(
        mt5_symbol
    )

    print(
        f"⚪ {symbol} 청산 신호 수신"
    )

    if current is None:

        print(
            f"⚪ {symbol} "
            f"현재 MT5 포지션 없음"
        )

        if symbol == "NAS":
            nas_position = None
        else:
            btc_position = None

        return {
            "status": "ignored",
            "reason": "no_position"
        }

    # -----------------------------------------------------
    # 실제 MT5 청산
    # -----------------------------------------------------

    success = close_mt5_position(
        mt5_symbol
    )

    if not success:

        print(
            f"❌ {symbol} "
            f"청산 실패"
        )

        return {
            "status": "close_failed",
            "symbol": symbol
        }

    # -----------------------------------------------------
    # Python 상태 초기화
    # -----------------------------------------------------

    if symbol == "NAS":

        nas_position = None

    else:

        btc_position = None

    print(
        f"✅ {symbol} "
        f"청산 완료"
    )

    return {
        "status": "closed",
        "symbol": symbol,
        "previous_position":
            current["direction"]
    }


# =========================================================
# [11] SMR 대기 시작 / 갱신
# =========================================================

def start_waiting(symbol, direction):

    check_timeout(symbol)

    now = datetime.now(KST)

    if symbol == "NAS":

        waiting = nas_waiting

    else:

        waiting = btc_waiting

    # -----------------------------------------------------
    # 같은 방향이면 20분 다시 시작
    # -----------------------------------------------------

    if (
        waiting["active"]
        and
        waiting["direction"] == direction
    ):

        waiting["timestamp"] = now

        print(
            f"🔄 {symbol} {direction} "
            f"SMR 추가 발생"
        )

        print(
            f"⏱ 마지막 신호 기준 "
            f"20분 대기 갱신"
        )

        return

    # -----------------------------------------------------
    # 새로운 대기 시작
    # -----------------------------------------------------

    waiting["active"] = True
    waiting["direction"] = direction
    waiting["timestamp"] = now

    print(
        f"⏳ {symbol} {direction} "
        f"대기 시작"
    )

    print(
        f"⏰ 기준 시간: "
        f"{now.strftime('%H:%M:%S')} KST"
    )


# =========================================================
# [12] 20분 시간 초과 확인
# =========================================================

def check_timeout(symbol):

    if symbol == "NAS":

        waiting = nas_waiting

    else:

        waiting = btc_waiting

    if not waiting["active"]:

        return False

    now = datetime.now(KST)

    elapsed = (
        now - waiting["timestamp"]
    ).total_seconds()

    if elapsed >= WAIT_SECONDS:

        print(
            f"⌛ {symbol} "
            f"{waiting['direction']} "
            f"대기시간 종료"
        )

        waiting["active"] = False
        waiting["direction"] = None
        waiting["timestamp"] = None

        return True

    return False


# =========================================================
# [13] 0선 돌파 처리
# =========================================================

def process_zero_cross(symbol):

    if symbol == "NAS":

        waiting = nas_waiting

    else:

        waiting = btc_waiting

    # -----------------------------------------------------
    # SMR 대기 없음
    # -----------------------------------------------------

    if not waiting["active"]:

        print(
            f"⚪ {symbol} 0선 돌파 → "
            f"SMR 대기 없음. 무시"
        )

        return {
            "status": "ignored",
            "reason": "no_smr_waiting"
        }

    # -----------------------------------------------------
    # 20분 초과
    # -----------------------------------------------------

    if check_timeout(symbol):

        print(
            f"⚪ {symbol} 0선 돌파 → "
            f"20분 초과. 무시"
        )

        return {
            "status": "ignored",
            "reason": "waiting_expired"
        }

    # -----------------------------------------------------
    # 방향
    # -----------------------------------------------------

    direction = waiting["direction"]

    # -----------------------------------------------------
    # 최종 매매
    # -----------------------------------------------------

    result = create_final_signal(
        symbol,
        direction
    )

    # -----------------------------------------------------
    # 주문 성공했을 때만 대기 초기화
    # -----------------------------------------------------

    if result["status"] == "final_signal":

        waiting["active"] = False
        waiting["direction"] = None
        waiting["timestamp"] = None

        print(
            f"✅ {symbol} 최종 "
            f"{direction} 신호 완료"
        )

    return result


# =========================================================
# [14] 신호 기록
# =========================================================

signals_history = {

    "NAS": [],

    "BTC": []
}


# =========================================================
# [15] TradingView 웹훅
# =========================================================

@app.post("/webhook")
async def webhook(request: Request):

    body = await request.body()

    message = body.decode(
        "utf-8",
        errors="ignore"
    ).strip()

    print("\n==============================")
    print("📩 TradingView 수신")
    print(message)
    print("==============================")

    # 공백 제거 + 대문자 변환
    clean_message = (
        message
        .replace(" ", "")
        .upper()
    )


    # =====================================================
    # NAS
    # =====================================================

    if "NAS" in clean_message:

        # -------------------------------------------------
        # NAS 청산
        # -------------------------------------------------

        if "NAS청산" in clean_message:

            return process_close("NAS")


        # -------------------------------------------------
        # NAS 0선 돌파
        # -------------------------------------------------

        if "NAS1000선돌파" in clean_message:

            return process_zero_cross("NAS")


        # -------------------------------------------------
        # NAS 지지
        # -------------------------------------------------

        if "지지구간" in clean_message:

            start_waiting(
                "NAS",
                "BUY"
            )

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "BUY"
            }


        # -------------------------------------------------
        # NAS 저항
        # -------------------------------------------------

        if "저항구간" in clean_message:

            start_waiting(
                "NAS",
                "SELL"
            )

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "SELL"
            }


        return {
            "status": "ignored",
            "reason": "NAS_unknown_signal"
        }


    # =====================================================
    # BTC
    # =====================================================

    if "BTC" in clean_message:

        # -------------------------------------------------
        # BTC 청산
        # -------------------------------------------------

        if "BTC청산" in clean_message:

            return process_close("BTC")


        # -------------------------------------------------
        # BTC 0선 돌파
        # -------------------------------------------------

        if "BTC0선돌파" in clean_message:

            return process_zero_cross("BTC")


        # -------------------------------------------------
        # BTC 지지
        # -------------------------------------------------

        if "지지구간" in clean_message:

            start_waiting(
                "BTC",
                "BUY"
            )

            return {
                "status": "waiting",
                "symbol": "BTC",
                "direction": "BUY"
            }


        # -------------------------------------------------
        # BTC 저항
        # -------------------------------------------------

        if "저항구간" in clean_message:

            start_waiting(
                "BTC",
                "SELL"
            )

            return {
                "status": "waiting",
                "symbol": "BTC",
                "direction": "SELL"
            }


        return {
            "status": "ignored",
            "reason": "BTC_unknown_signal"
        }


    # =====================================================
    # 종목 태그 없음
    # =====================================================

    return {
        "status": "ignored",
        "reason": "no_symbol_tag"
    }


# =========================================================
# [16] 현재 대기 상태 확인
# =========================================================

@app.get("/waiting/{symbol}")
def get_waiting(symbol: str):

    symbol = symbol.upper()

    if symbol == "NAS":

        waiting = nas_waiting

    elif symbol == "BTC":

        waiting = btc_waiting

    else:

        return {
            "status": "error",
            "reason": "unknown_symbol"
        }

    return {

        "symbol": symbol,

        "active":
            waiting["active"],

        "direction":
            waiting["direction"],

        "timestamp": (

            waiting["timestamp"].strftime(
                "%Y-%m-%d %H:%M:%S KST"
            )

            if waiting["timestamp"]

            else None
        )
    }


# =========================================================
# [17] 현재 포지션 상태 확인
# =========================================================

@app.get("/position/{symbol}")
def get_position(symbol: str):

    symbol = symbol.upper()

    mt5_symbol = get_mt5_symbol(symbol)

    if mt5_symbol is None:

        return {
            "status": "error",
            "reason": "unknown_symbol"
        }

    # 실제 MT5 포지션 확인
    position = get_current_position(
        mt5_symbol
    )

    return {

        "symbol": symbol,

        "mt5_symbol":
            mt5_symbol,

        "position":
            position
    }


# =========================================================
# [18] 신호 기록 확인
# =========================================================

@app.get("/signal/{symbol}")
def get_signal(symbol: str):

    symbol = symbol.upper()

    if symbol not in signals_history:

        return {
            "status": "error",
            "reason": "unknown_symbol"
        }

    return {

        "symbol": symbol,

        "signals":
            signals_history[symbol]
    }


# =========================================================
# [19] 테스트용 MT5 상태
# =========================================================

@app.get("/mt5")
def mt5_status():

    if not mt5.initialize():

        return {

            "status": "error",

            "connected": False,

            "error":
                str(mt5.last_error())
        }

    account = mt5.account_info()

    if account is None:

        return {

            "status": "error",

            "connected": False,

            "error":
                str(mt5.last_error())
        }

    return {

        "status": "ok",

        "connected": True,

        "login":
            account.login,

        "server":
            account.server,

        "balance":
            account.balance,

        "equity":
            account.equity,

        "margin":
            account.margin
    }
