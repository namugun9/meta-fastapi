"""Cloudtype deployment: TradingView signal filter only (no MT5 connection)."""

import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException, Request

app = FastAPI(title="TradingView signal filter")

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "signals.db"))

# 두 조건이 발생해야 최종 BUY/SELL
# 어느 조건이 먼저 와도 먼저 온 시점부터 20분 인정
WAIT_SECONDS = int(os.getenv("WAIT_SECONDS", "1200"))

# MT5 executor가 신호를 빌리는 시간
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "90"))

UTC = timezone.utc
KST = timezone(timedelta(hours=9))


# =========================================================
# DB
# =========================================================

@contextmanager
def database():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row

    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def kst_now_text() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def is_trade_time_kst() -> bool:
    """
    신규 BUY / SELL 허용 시간

    08:00 ~ 21:00
    22:35 ~ 05:00

    CLOSE는 24시간 허용
    """
    now = datetime.now(KST)

    current_minutes = now.hour * 60 + now.minute

    # 08:00 ~ 21:00
    if 8 * 60 <= current_minutes < 21 * 60:
        return True

    # 22:35 ~ 05:00
    if current_minutes >= 22 * 60 + 35 or current_minutes < 5 * 60:
        return True

    return False


def ensure_schema():
    with database() as db:

        # -------------------------------------------------
        # waiting
        #
        # NAS:
        #   support_ready  : 지지구간 생성/진입 조건
        #   rise_ready     : NAS100 상승 조건
        #
        #   resistance_ready : 저항구간 생성/진입 조건
        #   fall_ready       : NAS100 하락 조건
        #
        # BTC:
        #   기존 방식 유지
        # -------------------------------------------------

        db.execute("""
        CREATE TABLE IF NOT EXISTS waiting (
            symbol TEXT PRIMARY KEY,

            support_ready INTEGER NOT NULL DEFAULT 0,
            support_at TEXT,

            resistance_ready INTEGER NOT NULL DEFAULT 0,
            resistance_at TEXT,

            rise_ready INTEGER NOT NULL DEFAULT 0,
            rise_at TEXT,

            fall_ready INTEGER NOT NULL DEFAULT 0,
            fall_at TEXT,

            direction TEXT,

            updated_at TEXT NOT NULL
        )
        """)

        # -------------------------------------------------
        # signals
        # -------------------------------------------------

        db.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,

            direction TEXT NOT NULL
                CHECK(
                    direction IN (
                        'BUY',
                        'SELL',
                        'CLOSE',
                        'CLOSE_BUY',
                        'CLOSE_SELL'
                    )
                ),

            created_at TEXT NOT NULL,

            status TEXT NOT NULL DEFAULT 'pending',

            lease_until TEXT,

            executor_id TEXT,

            result_detail TEXT
        )
        """)


@app.on_event("startup")
def startup():
    ensure_schema()


# =========================================================
# MESSAGE PARSER
# =========================================================

def parse_message(message: str) -> Optional[Tuple[str, str]]:
    """
    NAS 실제 TradingView 메시지

        NAS 지지구간 생성
        NAS 지지구간 진입
        NAS100 상승

        NAS 저항구간 생성
        NAS 저항구간 진입
        NAS100 하락

        NAS100 상승청산
        NAS100 하락청산

    BTC 기존 메시지

        BTC_지지구간
        BTC_저항구간
        BTC_0선돌파
        BTC_청산

    공백은 자동 제거.
    """

    normalized = re.sub(r"\s+", "", message).upper()

    # -----------------------------------------------------
    # NAS 방향별 청산
    # -----------------------------------------------------

    if "NAS100상승청산" in normalized:
        return "NAS", "close_buy"

    if "NAS100하락청산" in normalized:
        return "NAS", "close_sell"

    # -----------------------------------------------------
    # NAS BUY 조건
    #
    # 지지구간 생성 OR 지지구간 진입
    # -----------------------------------------------------

    if normalized.startswith("NAS지지구간생성"):
        return "NAS", "support"

    if normalized.startswith("NAS지지구간진입"):
        return "NAS", "support"

    # -----------------------------------------------------
    # NAS SELL 조건
    #
    # 저항구간 생성 OR 저항구간 진입
    # -----------------------------------------------------

    if normalized.startswith("NAS저항구간생성"):
        return "NAS", "resistance"

    if normalized.startswith("NAS저항구간진입"):
        return "NAS", "resistance"

    # -----------------------------------------------------
    # NAS 최종 방향 조건
    # -----------------------------------------------------

    if normalized.startswith("NAS100상승"):
        return "NAS", "rise"

    if normalized.startswith("NAS100하락"):
        return "NAS", "fall"

    # -----------------------------------------------------
    # 기존 NAS underscore 형식도 호환
    # -----------------------------------------------------

    if normalized.startswith("NAS_지지구간"):
        return "NAS", "support"

    if normalized.startswith("NAS_저항구간"):
        return "NAS", "resistance"

    if normalized.startswith("NAS_0선돌파"):
        return "NAS", "zero_cross"

    if normalized.startswith("NAS_청산"):
        return "NAS", "close"

    # -----------------------------------------------------
    # BTC 기존 형식
    # -----------------------------------------------------

    if normalized.startswith("BTC_지지구간"):
        return "BTC", "support"

    if normalized.startswith("BTC_저항구간"):
        return "BTC", "resistance"

    if normalized.startswith("BTC_0선돌파"):
        return "BTC", "zero_cross"

    if normalized.startswith("BTC_청산"):
        return "BTC", "close"

    # -----------------------------------------------------
    # BTC 공백 형식도 호환
    # -----------------------------------------------------

    if normalized.startswith("BTC지지구간"):
        return "BTC", "support"

    if normalized.startswith("BTC저항구간"):
        return "BTC", "resistance"

    if normalized.startswith("BTC0선돌파"):
        return "BTC", "zero_cross"

    if normalized.startswith("BTC청산"):
        return "BTC", "close"

    return None


# =========================================================
# SIGNAL INSERT
# =========================================================

def create_signal(db, symbol, direction):

    cursor = db.execute(
        """
        INSERT INTO signals
        (
            symbol,
            direction,
            created_at
        )
        VALUES (?, ?, ?)
        """,
        (
            symbol,
            direction,
            now_iso(),
        ),
    )

    return cursor.lastrowid


# =========================================================
# WAITING ROW
# =========================================================

def ensure_waiting_row(db, symbol):

    row = db.execute(
        """
        SELECT *
        FROM waiting
        WHERE symbol = ?
        """,
        (symbol,),
    ).fetchone()

    if row is not None:
        return

    db.execute(
        """
        INSERT INTO waiting
        (
            symbol,
            support_ready,
            support_at,
            resistance_ready,
            resistance_at,
            rise_ready,
            rise_at,
            fall_ready,
            fall_at,
            direction,
            updated_at
        )
        VALUES (?, 0, NULL, 0, NULL, 0, NULL, 0, NULL, NULL, ?)
        """,
        (
            symbol,
            now_iso(),
        ),
    )


# =========================================================
# NAS WAITING CLEANUP
# =========================================================

def clear_nas_waiting(db):

    db.execute(
        """
        DELETE FROM waiting
        WHERE symbol = 'NAS'
        """
    )


# =========================================================
# NAS CONDITION CHECK
# =========================================================

def check_nas_conditions(db):

    row = db.execute(
        """
        SELECT *
        FROM waiting
        WHERE symbol = 'NAS'
        """
    ).fetchone()

    if row is None:
        return None

    now = datetime.now(UTC)

    # -----------------------------------------------------
    # BUY
    #
    # support_ready + rise_ready
    #
    # 둘 중 먼저 발생한 시간부터 20분
    # -----------------------------------------------------

    if row["support_ready"] and row["rise_ready"]:

        support_at = datetime.fromisoformat(row["support_at"])
        rise_at = datetime.fromisoformat(row["rise_at"])

        first_at = min(support_at, rise_at)

        if now - first_at <= timedelta(seconds=WAIT_SECONDS):

            clear_nas_waiting(db)

            signal_id = create_signal(
                db,
                "NAS",
                "BUY",
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "BUY",
                "reason": "support_and_rise",
                "kst": kst_now_text(),
            }

        # 20분 초과
        clear_nas_waiting(db)

        return {
            "status": "ignored",
            "reason": "buy_conditions_expired",
            "symbol": "NAS",
            "kst": kst_now_text(),
        }

    # -----------------------------------------------------
    # SELL
    #
    # resistance_ready + fall_ready
    #
    # 둘 중 먼저 발생한 시간부터 20분
    # -----------------------------------------------------

    if row["resistance_ready"] and row["fall_ready"]:

        resistance_at = datetime.fromisoformat(row["resistance_at"])
        fall_at = datetime.fromisoformat(row["fall_at"])

        first_at = min(resistance_at, fall_at)

        if now - first_at <= timedelta(seconds=WAIT_SECONDS):

            clear_nas_waiting(db)

            signal_id = create_signal(
                db,
                "NAS",
                "SELL",
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "SELL",
                "reason": "resistance_and_fall",
                "kst": kst_now_text(),
            }

        # 20분 초과
        clear_nas_waiting(db)

        return {
            "status": "ignored",
            "reason": "sell_conditions_expired",
            "symbol": "NAS",
            "kst": kst_now_text(),
        }

    return None


# =========================================================
# WEBHOOK
# =========================================================

@app.post("/webhook")
async def tradingview_webhook(request: Request):

    raw_body = await request.body()

    message = raw_body.decode(
        "utf-8",
        errors="replace",
    ).strip()

    parsed = parse_message(message)

    if parsed is None:

        return {
            "status": "ignored",
            "reason": "unsupported_message",
            "message": message,
            "kst": kst_now_text(),
        }

    symbol, event = parsed

    # =====================================================
    # DATABASE
    # =====================================================

    with database() as db:

        # =================================================
        # NAS 방향별 청산
        #
        # 24시간 허용
        # =================================================

        if symbol == "NAS" and event == "close_buy":

            clear_nas_waiting(db)

            signal_id = create_signal(
                db,
                "NAS",
                "CLOSE_BUY",
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "CLOSE_BUY",
                "kst": kst_now_text(),
            }

        if symbol == "NAS" and event == "close_sell":

            clear_nas_waiting(db)

            signal_id = create_signal(
                db,
                "NAS",
                "CLOSE_SELL",
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "CLOSE_SELL",
                "kst": kst_now_text(),
            }

        # =================================================
        # 기존 전체 청산
        #
        # 24시간 허용
        # =================================================

        if event == "close":

            if symbol == "NAS":
                clear_nas_waiting(db)

            else:
                db.execute(
                    """
                    DELETE FROM waiting
                    WHERE symbol = ?
                    """,
                    (symbol,),
                )

            signal_id = create_signal(
                db,
                symbol,
                "CLOSE",
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": symbol,
                "direction": "CLOSE",
                "kst": kst_now_text(),
            }

        # =================================================
        # NAS SUPPORT
        #
        # 지지구간 생성 OR 지지구간 진입
        #
        # 둘 다 같은 BUY 조건으로 취급
        # =================================================

        if symbol == "NAS" and event == "support":

            if not is_trade_time_kst():

                return {
                    "status": "ignored",
                    "reason": "outside_trading_hours",
                    "symbol": "NAS",
                    "event": "support",
                    "kst": kst_now_text(),
                }

            ensure_waiting_row(db, "NAS")

            current_time = now_iso()

            # 지지 조건을 새로 기억
            db.execute(
                """
                UPDATE waiting
                SET
                    support_ready = 1,
                    support_at = ?,
                    updated_at = ?,

                    -- 반대 방향 대기는 제거
                    resistance_ready = 0,
                    resistance_at = NULL,

                    fall_ready = 0,
                    fall_at = NULL,

                    direction = 'BUY'
                WHERE symbol = 'NAS'
                """,
                (
                    current_time,
                    current_time,
                ),
            )

            # 혹시 NAS100 상승이 이미 먼저 왔다면
            result = check_nas_conditions(db)

            if result is not None:
                return result

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "BUY",
                "condition": "support",
                "kst": kst_now_text(),
            }

        # =================================================
        # NAS RESISTANCE
        #
        # 저항구간 생성 OR 저항구간 진입
        #
        # 둘 다 같은 SELL 조건으로 취급
        # =================================================

        if symbol == "NAS" and event == "resistance":

            if not is_trade_time_kst():

                return {
                    "status": "ignored",
                    "reason": "outside_trading_hours",
                    "symbol": "NAS",
                    "event": "resistance",
                    "kst": kst_now_text(),
                }

            ensure_waiting_row(db, "NAS")

            current_time = now_iso()

            # 저항 조건을 새로 기억
            db.execute(
                """
                UPDATE waiting
                SET
                    resistance_ready = 1,
                    resistance_at = ?,
                    updated_at = ?,

                    -- 반대 방향 대기는 제거
                    support_ready = 0,
                    support_at = NULL,

                    rise_ready = 0,
                    rise_at = NULL,

                    direction = 'SELL'
                WHERE symbol = 'NAS'
                """,
                (
                    current_time,
                    current_time,
                ),
            )

            # 혹시 NAS100 하락이 이미 먼저 왔다면
            result = check_nas_conditions(db)

            if result is not None:
                return result

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "SELL",
                "condition": "resistance",
                "kst": kst_now_text(),
            }

        # =================================================
        # NAS100 상승
        #
        # 상승이 먼저 와도 기억
        # 지지구간 생성/진입이 먼저 와도 기억
        # =================================================

        if symbol == "NAS" and event == "rise":

            if not is_trade_time_kst():

                clear_nas_waiting(db)

                return {
                    "status": "ignored",
                    "reason": "outside_trading_hours",
                    "symbol": "NAS",
                    "event": "rise",
                    "kst": kst_now_text(),
                }

            ensure_waiting_row(db, "NAS")

            current_time = now_iso()

            # 상승 조건 기억
            db.execute(
                """
                UPDATE waiting
                SET
                    rise_ready = 1,
                    rise_at = ?,
                    updated_at = ?
                WHERE symbol = 'NAS'
                """,
                (
                    current_time,
                    current_time,
                ),
            )

            result = check_nas_conditions(db)

            if result is not None:
                return result

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "BUY",
                "condition": "rise",
                "kst": kst_now_text(),
            }

        # =================================================
        # NAS100 하락
        #
        # 하락이 먼저 와도 기억
        # 저항구간 생성/진입이 먼저 와도 기억
        # =================================================

        if symbol == "NAS" and event == "fall":

            if not is_trade_time_kst():

                clear_nas_waiting(db)

                return {
                    "status": "ignored",
                    "reason": "outside_trading_hours",
                    "symbol": "NAS",
                    "event": "fall",
                    "kst": kst_now_text(),
                }

            ensure_waiting_row(db, "NAS")

            current_time = now_iso()

            # 하락 조건 기억
            db.execute(
                """
                UPDATE waiting
                SET
                    fall_ready = 1,
                    fall_at = ?,
                    updated_at = ?
                WHERE symbol = 'NAS'
                """,
                (
                    current_time,
                    current_time,
                ),
            )

            result = check_nas_conditions(db)

            if result is not None:
                return result

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "SELL",
                "condition": "fall",
                "kst": kst_now_text(),
            }

        # =================================================
        # 기존 NAS 0선돌파
        #
        # 기존 방식 호환용
        # =================================================

        if symbol == "NAS" and event == "zero_cross":

            if not is_trade_time_kst():

                clear_nas_waiting(db)

                return {
                    "status": "ignored",
                    "reason": "outside_trading_hours",
                    "symbol": "NAS",
                    "event": "zero_cross",
                    "kst": kst_now_text(),
                }

            # 기존 waiting direction 방식은
            # 새 NAS 로직에서는 사용하지 않음
            clear_nas_waiting(db)

            return {
                "status": "ignored",
                "reason": "nas_zero_cross_not_used",
                "symbol": "NAS",
                "kst": kst_now_text(),
            }

        # =================================================
        # BTC
        #
        # 기존 방식 유지
        #
        # BTC 지지구간 → BUY 대기
        # BTC 저항구간 → SELL 대기
        # BTC 0선돌파 → 해당 방향 최종 주문
        # =================================================

        if symbol == "BTC" and event in ("support", "resistance"):

            if not is_trade_time_kst():

                return {
                    "status": "ignored",
                    "reason": "outside_trading_hours",
                    "symbol": "BTC",
                    "event": event,
                    "kst": kst_now_text(),
                }

            ensure_waiting_row(db, "BTC")

            direction = (
                "BUY"
                if event == "support"
                else "SELL"
            )

            current_time = now_iso()

            db.execute(
                """
                UPDATE waiting
                SET
                    direction = ?,
                    updated_at = ?,

                    support_ready = 0,
                    support_at = NULL,

                    resistance_ready = 0,
                    resistance_at = NULL,

                    rise_ready = 0,
                    rise_at = NULL,

                    fall_ready = 0,
                    fall_at = NULL
                WHERE symbol = 'BTC'
                """,
                (
                    direction,
                    current_time,
                ),
            )

            # BTC의 기존 waiting 시간은 updated_at
            return {
                "status": "waiting",
                "symbol": "BTC",
                "direction": direction,
                "kst": kst_now_text(),
            }

        # =================================================
        # BTC 0선돌파
        # =================================================

        if symbol == "BTC" and event == "zero_cross":

            if not is_trade_time_kst():

                db.execute(
                    """
                    DELETE FROM waiting
                    WHERE symbol = 'BTC'
                    """
                )

                return {
                    "status": "ignored",
                    "reason": "outside_trading_hours",
                    "symbol": "BTC",
                    "event": "zero_cross",
                    "kst": kst_now_text(),
                }

            waiting = db.execute(
                """
                SELECT *
                FROM waiting
                WHERE symbol = 'BTC'
                """
            ).fetchone()

            if waiting is None or not waiting["direction"]:

                return {
                    "status": "ignored",
                    "reason": "no_btc_waiting",
                    "symbol": "BTC",
                    "kst": kst_now_text(),
                }

            updated_at = datetime.fromisoformat(
                waiting["updated_at"]
            )

            if (
                datetime.now(UTC) - updated_at
                > timedelta(seconds=WAIT_SECONDS)
            ):

                db.execute(
                    """
                    DELETE FROM waiting
                    WHERE symbol = 'BTC'
                    """
                )

                return {
                    "status": "ignored",
                    "reason": "waiting_expired",
                    "symbol": "BTC",
                    "kst": kst_now_text(),
                }

            direction = waiting["direction"]

            db.execute(
                """
                DELETE FROM waiting
                WHERE symbol = 'BTC'
                """
            )

            signal_id = create_signal(
                db,
                "BTC",
                direction,
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "BTC",
                "direction": direction,
                "kst": kst_now_text(),
            }

        # =================================================
        # 알 수 없는 이벤트
        # =================================================

        return {
            "status": "ignored",
            "reason": "unknown_event",
            "symbol": symbol,
            "event": event,
            "kst": kst_now_text(),
        }


# =========================================================
# MT5 EXECUTOR → NEXT SIGNAL
# =========================================================

@app.get("/api/v1/signals/next")
def next_signal(executor_id: str):

    with database() as db:

        db.execute("BEGIN IMMEDIATE")

        now = now_iso()

        # -------------------------------------------------
        # 만료된 lease 복구
        # -------------------------------------------------

        db.execute(
            """
            UPDATE signals
            SET
                status = 'pending',
                lease_until = NULL,
                executor_id = NULL
            WHERE
                status = 'leased'
                AND lease_until < ?
            """,
            (now,),
        )

        # -------------------------------------------------
        # 가장 오래된 pending 신호
        # -------------------------------------------------

        row = db.execute(
            """
            SELECT *
            FROM signals
            WHERE status = 'pending'
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()

        if row is None:

            return {
                "signal": None
            }

        # -------------------------------------------------
        # lease
        # -------------------------------------------------

        lease_until = (
            datetime.now(UTC)
            + timedelta(seconds=LEASE_SECONDS)
        ).isoformat()

        db.execute(
            """
            UPDATE signals
            SET
                status = 'leased',
                lease_until = ?,
                executor_id = ?
            WHERE id = ?
            """,
            (
                lease_until,
                executor_id,
                row["id"],
            ),
        )

        return {
            "signal": {
                "id": row["id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
            }
        }


# =========================================================
# MT5 EXECUTOR → ACK
# =========================================================

@app.post("/api/v1/signals/{signal_id}/ack")
async def acknowledge(
    signal_id: int,
    request: Request,
):

    payload = await request.json()

    status = payload.get("status")

    if status not in ("done", "failed"):

        raise HTTPException(
            status_code=422,
            detail="status must be done or failed",
        )

    with database() as db:

        updated = db.execute(
            """
            UPDATE signals
            SET
                status = ?,
                result_detail = ?,
                lease_until = NULL
            WHERE id = ?
            """,
            (
                status,
                str(
                    payload.get(
                        "detail",
                        "",
                    )
                )[:500],
                signal_id,
            ),
        ).rowcount

    if updated != 1:

        raise HTTPException(
            status_code=404,
            detail="signal not found",
        )

    return {
        "status": "ok"
    }


# =========================================================
# HEALTH CHECK
# =========================================================

@app.get("/health")
def health():

    return {
        "status": "ok",
        "kst": kst_now_text(),
        "trading_time": is_trade_time_kst(),
        "wait_seconds": WAIT_SECONDS,
    }
