import os
import re
import sqlite3

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException, Request


# =========================================================
# [1] 기본 설정
# =========================================================

app = FastAPI(title="Ella NAS100 Signal Filter")

DATABASE_PATH = Path(
    os.getenv("DATABASE_PATH", "signals.db")
)

# 같은 방향의 두 신호가 완성될 수 있는 최대 대기시간
WAIT_SECONDS = 120 * 60

# MT5 Executor가 신호를 빌리는 시간
LEASE_SECONDS = int(
    os.getenv("LEASE_SECONDS", "90")
)

UTC = timezone.utc
KST = timezone(timedelta(hours=9))


# =========================================================
# [2] DB
# =========================================================

@contextmanager
def database():
    connection = sqlite3.connect(
        DATABASE_PATH,
        timeout=30
    )
    connection.row_factory = sqlite3.Row

    try:
        yield connection
        connection.commit()

    finally:
        connection.close()


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def kst_now_text() -> str:
    return datetime.now(KST).strftime(
        "%Y-%m-%d %H:%M:%S KST"
    )


# =========================================================
# [3] DB 구조
# =========================================================

def ensure_schema():

    with database() as db:

        # -------------------------------------------------
        # NAS BUY 조합 대기
        #
        # 지지구간 / 매수세력감지
        # 어느 것이 먼저 와도 120분 동안 서로 기다린다.
        # -------------------------------------------------

        db.execute("""
        CREATE TABLE IF NOT EXISTS waiting (
            symbol TEXT PRIMARY KEY,

            support_ready INTEGER NOT NULL DEFAULT 0,
            support_at TEXT,

            buy_force_ready INTEGER NOT NULL DEFAULT 0,
            buy_force_at TEXT,

            resistance_ready INTEGER NOT NULL DEFAULT 0,
            resistance_at TEXT,

            sell_force_ready INTEGER NOT NULL DEFAULT 0,
            sell_force_at TEXT,

            updated_at TEXT NOT NULL
        )
        """)

        # 기존 DB에 새 컬럼이 없을 경우 추가
        existing = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(waiting)"
            ).fetchall()
        }

        new_columns = {
            "buy_force_ready":
                "INTEGER NOT NULL DEFAULT 0",
            "buy_force_at":
                "TEXT",
            "sell_force_ready":
                "INTEGER NOT NULL DEFAULT 0",
            "sell_force_at":
                "TEXT",
        }

        for column, definition in new_columns.items():
            if column not in existing:
                db.execute(
                    f"ALTER TABLE waiting ADD COLUMN "
                    f"{column} {definition}"
                )

        # -------------------------------------------------
        # 최종 신호
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

            status TEXT NOT NULL
                DEFAULT 'pending',

            lease_until TEXT,

            executor_id TEXT,

            result_detail TEXT
        )
        """)


@app.on_event("startup")
def startup():
    ensure_schema()


# =========================================================
# [4] TradingView 메시지 파서
# =========================================================

def parse_message(
    message: str
) -> Optional[Tuple[str, str]]:

    """
    NAS 신호만 처리.

    지원 신호
    ------------------------------------------------
    NAS 지지구간 생성
    NAS 지지구간 진입

    NAS 저항구간 생성
    NAS 저항구간 진입

    매수세력감지
    매도세력감지

    매수세력빠짐
    매도세력빠짐

    BTC
    → 무조건 무시
    """

    # 공백 / 이모지 / 특수문자 제거
    normalized = re.sub(
        r"[^A-Z0-9가-힣_]",
        "",
        message.upper()
    )

    # -----------------------------------------------------
    # BTC
    # -----------------------------------------------------

    if normalized.startswith("BTC"):
        return None

    # -----------------------------------------------------
    # 매수세력빠짐
    # -----------------------------------------------------

    if "매수세력빠짐" in normalized:
        return "NAS", "close_buy"

    # -----------------------------------------------------
    # 매도세력빠짐
    # -----------------------------------------------------

    if "매도세력빠짐" in normalized:
        return "NAS", "close_sell"

    # -----------------------------------------------------
    # 매수세력감지
    # -----------------------------------------------------

    if "매수세력감지" in normalized:
        return "NAS", "buy_force"

    # -----------------------------------------------------
    # 매도세력감지
    # -----------------------------------------------------

    if "매도세력감지" in normalized:
        return "NAS", "sell_force"

    # -----------------------------------------------------
    # NAS 지지구간
    # 생성/진입 모두 같은 BUY 조건
    # -----------------------------------------------------

    if normalized.startswith("NAS지지구간생성"):
        return "NAS", "support"

    if normalized.startswith("NAS지지구간진입"):
        return "NAS", "support"

    # -----------------------------------------------------
    # NAS 저항구간
    # 생성/진입 모두 같은 SELL 조건
    # -----------------------------------------------------

    if normalized.startswith("NAS저항구간생성"):
        return "NAS", "resistance"

    if normalized.startswith("NAS저항구간진입"):
        return "NAS", "resistance"

    # -----------------------------------------------------
    # 기존 underscore 형식도 일부 호환
    # -----------------------------------------------------

    if normalized.startswith("NAS_지지구간"):
        return "NAS", "support"

    if normalized.startswith("NAS_저항구간"):
        return "NAS", "resistance"

    return None


# =========================================================
# [5] 최종 신호 생성
# =========================================================

def create_signal(
    db,
    symbol,
    direction
):

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
# [6] NAS 대기 행 생성
# =========================================================

def ensure_nas_waiting(db):

    row = db.execute(
        """
        SELECT *
        FROM waiting
        WHERE symbol = 'NAS'
        """
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
            buy_force_ready,
            buy_force_at,
            resistance_ready,
            resistance_at,
            sell_force_ready,
            sell_force_at,
            updated_at
        )
        VALUES (
            'NAS',
            0,
            NULL,
            0,
            NULL,
            0,
            NULL,
            0,
            NULL,
            ?
        )
        """,
        (
            now_iso(),
        ),
    )


# =========================================================
# [7] 개별 BUY 대기 삭제
# =========================================================

def clear_buy_waiting(db):

    db.execute(
        """
        UPDATE waiting
        SET
            support_ready = 0,
            support_at = NULL,
            buy_force_ready = 0,
            buy_force_at = NULL
        WHERE symbol = 'NAS'
        """
    )


# =========================================================
# [8] 개별 SELL 대기 삭제
# =========================================================

def clear_sell_waiting(db):

    db.execute(
        """
        UPDATE waiting
        SET
            resistance_ready = 0,
            resistance_at = NULL,
            sell_force_ready = 0,
            sell_force_at = NULL
        WHERE symbol = 'NAS'
        """
    )


# =========================================================
# [9] BUY 조합 유효성
#
# 두 신호 중 먼저 나온 신호부터 120분
# 순서는 관계없음.
# =========================================================

def buy_pair_valid(row):

    if not row:
        return False

    if not row["support_ready"]:
        return False

    if not row["support_at"]:
        return False

    if not row["buy_force_ready"]:
        return False

    if not row["buy_force_at"]:
        return False

    support_at = datetime.fromisoformat(
        row["support_at"]
    )

    buy_force_at = datetime.fromisoformat(
        row["buy_force_at"]
    )

    first_at = min(
        support_at,
        buy_force_at
    )

    second_at = max(
        support_at,
        buy_force_at
    )

    return (
        second_at - first_at
        <= timedelta(seconds=WAIT_SECONDS)
    )


# =========================================================
# [10] SELL 조합 유효성
#
# 두 신호 중 먼저 나온 신호부터 120분
# 순서는 관계없음.
# =========================================================

def sell_pair_valid(row):

    if not row:
        return False

    if not row["resistance_ready"]:
        return False

    if not row["resistance_at"]:
        return False

    if not row["sell_force_ready"]:
        return False

    if not row["sell_force_at"]:
        return False

    resistance_at = datetime.fromisoformat(
        row["resistance_at"]
    )

    sell_force_at = datetime.fromisoformat(
        row["sell_force_at"]
    )

    first_at = min(
        resistance_at,
        sell_force_at
    )

    second_at = max(
        resistance_at,
        sell_force_at
    )

    return (
        second_at - first_at
        <= timedelta(seconds=WAIT_SECONDS)
    )


# =========================================================
# [11] 오래된 단일 대기 자동 정리
#
# BUY:
#   지지구간 또는 매수세력감지 중
#   하나만 존재할 때 120분이 지나면 제거
#
# SELL:
#   저항구간 또는 매도세력감지 중
#   하나만 존재할 때 120분이 지나면 제거
# =========================================================

def cleanup_expired_waiting(db):

    row = db.execute(
        """
        SELECT *
        FROM waiting
        WHERE symbol = 'NAS'
        """
    ).fetchone()

    if row is None:
        return

    now = datetime.now(UTC)

    # -----------------------------------------------------
    # BUY
    # -----------------------------------------------------

    buy_times = []

    if row["support_ready"] and row["support_at"]:
        buy_times.append(
            datetime.fromisoformat(row["support_at"])
        )

    if row["buy_force_ready"] and row["buy_force_at"]:
        buy_times.append(
            datetime.fromisoformat(row["buy_force_at"])
        )

    if buy_times:

        first_buy = min(buy_times)

        if now - first_buy > timedelta(
            seconds=WAIT_SECONDS
        ):

            clear_buy_waiting(db)

            print(
                "[WAIT EXPIRED] NAS BUY pair"
            )

    # -----------------------------------------------------
    # SELL
    # -----------------------------------------------------

    row = db.execute(
        """
        SELECT *
        FROM waiting
        WHERE symbol = 'NAS'
        """
    ).fetchone()

    if row is None:
        return

    sell_times = []

    if row["resistance_ready"] and row["resistance_at"]:
        sell_times.append(
            datetime.fromisoformat(row["resistance_at"])
        )

    if row["sell_force_ready"] and row["sell_force_at"]:
        sell_times.append(
            datetime.fromisoformat(row["sell_force_at"])
        )

    if sell_times:

        first_sell = min(sell_times)

        if now - first_sell > timedelta(
            seconds=WAIT_SECONDS
        ):

            clear_sell_waiting(db)

            print(
                "[WAIT EXPIRED] NAS SELL pair"
            )


# =========================================================
# [12] WEBHOOK
# =========================================================

@app.post("/webhook")
async def tradingview_webhook(
    request: Request
):

    raw_body = await request.body()

    message = raw_body.decode(
        "utf-8",
        errors="replace"
    ).strip()

    print(
        f"\n[WEBHOOK RECEIVED] {message}"
    )

    parsed = parse_message(
        message
    )

    # -----------------------------------------------------
    # 처리하지 않는 신호
    # -----------------------------------------------------

    if parsed is None:

        print(
            "[IGNORED] BTC 또는 알 수 없는 신호"
        )

        return {
            "status": "ignored",
            "message": message,
            "kst": kst_now_text(),
        }

    symbol, event = parsed

    if symbol != "NAS":

        return {
            "status": "ignored",
            "reason": "non_nas_symbol",
            "message": message,
            "kst": kst_now_text(),
        }

    with database() as db:

        ensure_nas_waiting(db)

        cleanup_expired_waiting(db)

        # =================================================
        # [A] 매수세력빠짐
        #
        # 대기 상태와 관계없이 즉시 CLOSE_BUY
        # =================================================

        if event == "close_buy":

            signal_id = create_signal(
                db,
                "NAS",
                "CLOSE_BUY"
            )

            print(
                f"[CLOSE_BUY] signal_id={signal_id}"
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "CLOSE_BUY",
                "kst": kst_now_text(),
            }

        # =================================================
        # [B] 매도세력빠짐
        #
        # 대기 상태와 관계없이 즉시 CLOSE_SELL
        # =================================================

        if event == "close_sell":

            signal_id = create_signal(
                db,
                "NAS",
                "CLOSE_SELL"
            )

            print(
                f"[CLOSE_SELL] signal_id={signal_id}"
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "CLOSE_SELL",
                "kst": kst_now_text(),
            }

        # =================================================
        # [C] NAS 지지구간
        #
        # 매수세력감지가 먼저 왔든
        # 지지구간이 먼저 왔든 상관없음.
        # =================================================

        if event == "support":

            current_time = now_iso()

            # 지지구간은 같은 방향의 기준 신호이므로
            # 다시 들어오면 120분 타이머 갱신.
            db.execute(
                """
                UPDATE waiting
                SET
                    support_ready = 1,
                    support_at = ?,
                    updated_at = ?
                WHERE symbol = 'NAS'
                """,
                (
                    current_time,
                    current_time,
                ),
            )

            row = db.execute(
                """
                SELECT *
                FROM waiting
                WHERE symbol = 'NAS'
                """
            ).fetchone()

            # 매수세력감지가 이미 살아 있으면
            # 순서와 관계없이 즉시 BUY
            if buy_pair_valid(row):

                clear_buy_waiting(db)

                signal_id = create_signal(
                    db,
                    "NAS",
                    "BUY"
                )

                print(
                    f"[BUY FINAL] signal_id={signal_id} "
                    f"reason=buy_force_then_support"
                )

                return {
                    "status": "final_signal",
                    "id": signal_id,
                    "symbol": "NAS",
                    "direction": "BUY",
                    "reason": "two_buy_signals_within_120_minutes",
                    "kst": kst_now_text(),
                }

            print(
                "[WAIT BUY] "
                "NAS 지지구간 → 매수세력감지 대기"
            )

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "BUY",
                "wait_seconds": WAIT_SECONDS,
                "kst": kst_now_text(),
            }

        # =================================================
        # [D] NAS 저항구간
        #
        # 매도세력감지가 먼저 왔든
        # 저항구간이 먼저 왔든 상관없음.
        # =================================================

        if event == "resistance":

            current_time = now_iso()

            db.execute(
                """
                UPDATE waiting
                SET
                    resistance_ready = 1,
                    resistance_at = ?,
                    updated_at = ?
                WHERE symbol = 'NAS'
                """,
                (
                    current_time,
                    current_time,
                ),
            )

            row = db.execute(
                """
                SELECT *
                FROM waiting
                WHERE symbol = 'NAS'
                """
            ).fetchone()

            # 매도세력감지가 이미 살아 있으면
            # 순서와 관계없이 즉시 SELL
            if sell_pair_valid(row):

                clear_sell_waiting(db)

                signal_id = create_signal(
                    db,
                    "NAS",
                    "SELL"
                )

                print(
                    f"[SELL FINAL] signal_id={signal_id} "
                    f"reason=sell_force_then_resistance"
                )

                return {
                    "status": "final_signal",
                    "id": signal_id,
                    "symbol": "NAS",
                    "direction": "SELL",
                    "reason": "two_sell_signals_within_120_minutes",
                    "kst": kst_now_text(),
                }

            print(
                "[WAIT SELL] "
                "NAS 저항구간 → 매도세력감지 대기"
            )

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "SELL",
                "wait_seconds": WAIT_SECONDS,
                "kst": kst_now_text(),
            }

        # =================================================
        # [E] 매수세력감지
        #
        # 지지구간이 먼저 왔든
        # 매수세력감지가 먼저 왔든 상관없음.
        # =================================================

        if event == "buy_force":

            current_time = now_iso()

            # 매수세력감지 자체가 먼저 나온 경우에도
            # 120분 동안 지지구간을 기다린다.
            db.execute(
                """
                UPDATE waiting
                SET
                    buy_force_ready = 1,
                    buy_force_at = ?,
                    updated_at = ?
                WHERE symbol = 'NAS'
                """,
                (
                    current_time,
                    current_time,
                ),
            )

            row = db.execute(
                """
                SELECT *
                FROM waiting
                WHERE symbol = 'NAS'
                """
            ).fetchone()

            # 지지구간과 120분 이내면 즉시 BUY
            if buy_pair_valid(row):

                clear_buy_waiting(db)

                signal_id = create_signal(
                    db,
                    "NAS",
                    "BUY"
                )

                print(
                    f"[BUY FINAL] signal_id={signal_id} "
                    f"reason=support_then_buy_force"
                )

                return {
                    "status": "final_signal",
                    "id": signal_id,
                    "symbol": "NAS",
                    "direction": "BUY",
                    "reason": "two_buy_signals_within_120_minutes",
                    "kst": kst_now_text(),
                }

            print(
                "[WAIT BUY] "
                "매수세력감지 → 지지구간 대기"
            )

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "BUY",
                "wait_seconds": WAIT_SECONDS,
                "kst": kst_now_text(),
            }

        # =================================================
        # [F] 매도세력감지
        #
        # 저항구간이 먼저 왔든
        # 매도세력감지가 먼저 왔든 상관없음.
        # =================================================

        if event == "sell_force":

            current_time = now_iso()

            # 매도세력감지 자체가 먼저 나온 경우에도
            # 120분 동안 저항구간을 기다린다.
            db.execute(
                """
                UPDATE waiting
                SET
                    sell_force_ready = 1,
                    sell_force_at = ?,
                    updated_at = ?
                WHERE symbol = 'NAS'
                """,
                (
                    current_time,
                    current_time,
                ),
            )

            row = db.execute(
                """
                SELECT *
                FROM waiting
                WHERE symbol = 'NAS'
                """
            ).fetchone()

            # 저항구간과 120분 이내면 즉시 SELL
            if sell_pair_valid(row):

                clear_sell_waiting(db)

                signal_id = create_signal(
                    db,
                    "NAS",
                    "SELL"
                )

                print(
                    f"[SELL FINAL] signal_id={signal_id} "
                    f"reason=resistance_then_sell_force"
                )

                return {
                    "status": "final_signal",
                    "id": signal_id,
                    "symbol": "NAS",
                    "direction": "SELL",
                    "reason": "two_sell_signals_within_120_minutes",
                    "kst": kst_now_text(),
                }

            print(
                "[WAIT SELL] "
                "매도세력감지 → 저항구간 대기"
            )

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "SELL",
                "wait_seconds": WAIT_SECONDS,
                "kst": kst_now_text(),
            }

        # =================================================
        # 알 수 없는 이벤트
        # =================================================

        return {
            "status": "ignored",
            "reason": "unknown_event",
            "symbol": "NAS",
            "event": event,
            "kst": kst_now_text(),
        }


# =========================================================
# [13] 현재 대기 상태
# =========================================================

@app.get("/waiting/NAS")
def get_nas_waiting():

    with database() as db:

        ensure_nas_waiting(db)
        cleanup_expired_waiting(db)

        row = db.execute(
            """
            SELECT *
            FROM waiting
            WHERE symbol = 'NAS'
            """
        ).fetchone()

        if row is None:

            return {
                "symbol": "NAS",
                "buy_waiting": False,
                "sell_waiting": False,
            }

        now = datetime.now(UTC)

        # -------------------------------------------------
        # BUY 남은 시간
        # -------------------------------------------------

        buy_times = []

        if row["support_ready"] and row["support_at"]:
            buy_times.append(
                datetime.fromisoformat(
                    row["support_at"]
                )
            )

        if row["buy_force_ready"] and row["buy_force_at"]:
            buy_times.append(
                datetime.fromisoformat(
                    row["buy_force_at"]
                )
            )

        buy_remaining = 0

        if buy_times:

            first_buy = min(buy_times)

            elapsed = (
                now - first_buy
            ).total_seconds()

            buy_remaining = max(
                0,
                int(
                    WAIT_SECONDS - elapsed
                )
            )

        # -------------------------------------------------
        # SELL 남은 시간
        # -------------------------------------------------

        sell_times = []

        if row["resistance_ready"] and row["resistance_at"]:
            sell_times.append(
                datetime.fromisoformat(
                    row["resistance_at"]
                )
            )

        if row["sell_force_ready"] and row["sell_force_at"]:
            sell_times.append(
                datetime.fromisoformat(
                    row["sell_force_at"]
                )
            )

        sell_remaining = 0

        if sell_times:

            first_sell = min(sell_times)

            elapsed = (
                now - first_sell
            ).total_seconds()

            sell_remaining = max(
                0,
                int(
                    WAIT_SECONDS - elapsed
                )
            )

        return {
            "symbol": "NAS",

            "buy_waiting": bool(
                row["support_ready"]
                or row["buy_force_ready"]
            ),

            "buy_support_ready": bool(
                row["support_ready"]
            ),

            "buy_force_ready": bool(
                row["buy_force_ready"]
            ),

            "buy_remaining_seconds":
                buy_remaining,

            "buy_remaining_minutes":
                round(
                    buy_remaining / 60,
                    1
                ),

            "sell_waiting": bool(
                row["resistance_ready"]
                or row["sell_force_ready"]
            ),

            "sell_resistance_ready": bool(
                row["resistance_ready"]
            ),

            "sell_force_ready": bool(
                row["sell_force_ready"]
            ),

            "sell_remaining_seconds":
                sell_remaining,

            "sell_remaining_minutes":
                round(
                    sell_remaining / 60,
                    1
                ),

            "kst": kst_now_text(),
        }


# =========================================================
# [14] 신호 확인
# =========================================================

@app.get("/signal/NAS")
def get_nas_signals():

    with database() as db:

        rows = db.execute(
            """
            SELECT
                id,
                symbol,
                direction,
                created_at,
                status,
                executor_id,
                result_detail
            FROM signals
            WHERE symbol = 'NAS'
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

        return {
            "symbol": "NAS",
            "signals": [
                dict(row)
                for row in rows
            ]
        }


# =========================================================
# [15] MT5 EXECUTOR → NEXT SIGNAL
# =========================================================

@app.get("/api/v1/signals/next")
def next_signal(
    executor_id: str
):

    with database() as db:

        db.execute(
            "BEGIN IMMEDIATE"
        )

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
            + timedelta(
                seconds=LEASE_SECONDS
            )
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
            )
        )

        print(
            f"[SIGNAL SENT] "
            f"id={row['id']} "
            f"{row['symbol']} "
            f"{row['direction']} "
            f"executor={executor_id}"
        )

        return {
            "signal": {
                "id": row["id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
            }
        }


# =========================================================
# [16] MT5 EXECUTOR → ACK
# =========================================================

@app.post(
    "/api/v1/signals/{signal_id}/ack"
)
async def acknowledge(
    signal_id: int,
    request: Request
):

    payload = await request.json()

    # 현재 MT5 Executor 형식:
    # {
    #     "success": true/false,
    #     "result_detail": "..."
    # }
    #
    # 기존 status/detail 형식도 호환

    success = payload.get("success")

    if success is not None:

        status = (
            "done"
            if bool(success)
            else "failed"
        )

        detail = str(
            payload.get(
                "result_detail",
                ""
            )
        )[:500]

    else:

        status = payload.get("status")

        if status not in (
            "done",
            "failed"
        ):

            raise HTTPException(
                status_code=422,
                detail=(
                    "success must be true/false "
                    "or status must be done/failed"
                ),
            )

        detail = str(
            payload.get("detail", "")
        )[:500]

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
                detail,
                signal_id,
            ),
        ).rowcount

    if updated != 1:

        raise HTTPException(
            status_code=404,
            detail="signal not found",
        )

    print(
        f"[ACK] "
        f"id={signal_id} "
        f"status={status}"
    )

    return {
        "status": "ok"
    }


# =========================================================
# [17] HEALTH CHECK
# =========================================================

@app.get("/health")
def health():

    return {
        "status": "ok",

        "system":
            "NAS100 ONLY",

        "wait_minutes":
            WAIT_SECONDS // 60,

        "pair_order":
            "ANY ORDER",

        "btc":
            "IGNORED",

        "trading_time":
            "UNLIMITED",

        "kst":
            kst_now_text(),
    }
