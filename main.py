"""
엘라 자동매매 - Cloudtype TradingView 신호 필터
================================================

TradingView
    ↓
Cloudtype FastAPI
    ↓
SQLite 신호 DB
    ↓
로컬 MT5 Executor
    ↓
XM MT5

최종 NAS 로직
------------------------------------------------
1. NAS 지지구간 생성/진입
   → 60분 동안 매수세력감지 대기

2. 대기 중 매수세력감지
   → BUY

3. 매수세력빠짐
   → 대기 상태와 관계없이 CLOSE_BUY

4. NAS 저항구간 생성/진입
   → 60분 동안 매도세력감지 대기

5. 대기 중 매도세력감지
   → SELL

6. 매도세력빠짐
   → 대기 상태와 관계없이 CLOSE_SELL

7. 같은 방향 구간 신호가 다시 오면
   → 해당 방향 60분 갱신

8. BTC
   → 전부 무시

9. 신규매매 시간 제한
   → 없음

10. 청산 여부
   → MT5가 실제 포지션을 확인
"""

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

# 60분
WAIT_SECONDS = 60 * 60

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
        # 대기 상태
        #
        # NAS BUY
        #   support_ready
        #   support_at
        #
        # NAS SELL
        #   resistance_ready
        #   resistance_at
        # -------------------------------------------------

        db.execute("""
        CREATE TABLE IF NOT EXISTS waiting (
            symbol TEXT PRIMARY KEY,

            support_ready INTEGER NOT NULL DEFAULT 0,
            support_at TEXT,

            resistance_ready INTEGER NOT NULL DEFAULT 0,
            resistance_at TEXT,

            updated_at TEXT NOT NULL
        )
        """)

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
    # 예: "NAS 🟡 지지구간 진입 30"
    #     → "NAS지지구간진입30"
    normalized = re.sub(
        r"[^A-Z0-9가-힣_]",
        "",
        message.upper()
    )

    # -----------------------------------------------------
    # BTC
    #
    # 가장 먼저 차단
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
    # NAS 지지구간 생성
    # -----------------------------------------------------

    if normalized.startswith(
        "NAS지지구간생성"
    ):
        return "NAS", "support"

    # -----------------------------------------------------
    # NAS 지지구간 진입
    # -----------------------------------------------------

    if normalized.startswith(
        "NAS지지구간진입"
    ):
        return "NAS", "support"

    # -----------------------------------------------------
    # NAS 저항구간 생성
    # -----------------------------------------------------

    if normalized.startswith(
        "NAS저항구간생성"
    ):
        return "NAS", "resistance"

    # -----------------------------------------------------
    # NAS 저항구간 진입
    # -----------------------------------------------------

    if normalized.startswith(
        "NAS저항구간진입"
    ):
        return "NAS", "resistance"

    # -----------------------------------------------------
    # 기존 underscore 형식도 일부 호환
    # -----------------------------------------------------

    if normalized.startswith(
        "NAS_지지구간"
    ):
        return "NAS", "support"

    if normalized.startswith(
        "NAS_저항구간"
    ):
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
            resistance_ready,
            resistance_at,
            updated_at
        )
        VALUES (
            'NAS',
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
# [7] NAS BUY 대기 삭제
# =========================================================

def clear_buy_waiting(db):

    db.execute(
        """
        UPDATE waiting

        SET
            support_ready = 0,
            support_at = NULL

        WHERE symbol = 'NAS'
        """
    )


# =========================================================
# [8] NAS SELL 대기 삭제
# =========================================================

def clear_sell_waiting(db):

    db.execute(
        """
        UPDATE waiting

        SET
            resistance_ready = 0,
            resistance_at = NULL

        WHERE symbol = 'NAS'
        """
    )


# =========================================================
# [9] NAS 전체 대기 삭제
# =========================================================

def clear_all_waiting(db):

    db.execute(
        """
        DELETE FROM waiting
        WHERE symbol = 'NAS'
        """
    )


# =========================================================
# [10] BUY 대기 유효성 확인
# =========================================================

def buy_waiting_valid(row):

    if not row:
        return False

    if not row["support_ready"]:
        return False

    if not row["support_at"]:
        return False

    support_at = datetime.fromisoformat(
        row["support_at"]
    )

    elapsed = (
        datetime.now(UTC) - support_at
    )

    return elapsed <= timedelta(
        seconds=WAIT_SECONDS
    )


# =========================================================
# [11] SELL 대기 유효성 확인
# =========================================================

def sell_waiting_valid(row):

    if not row:
        return False

    if not row["resistance_ready"]:
        return False

    if not row["resistance_at"]:
        return False

    resistance_at = datetime.fromisoformat(
        row["resistance_at"]
    )

    elapsed = (
        datetime.now(UTC) - resistance_at
    )

    return elapsed <= timedelta(
        seconds=WAIT_SECONDS
    )


# =========================================================
# [12] 오래된 대기 자동 정리
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

    # BUY 대기 만료
    if row["support_ready"]:

        if not buy_waiting_valid(row):

            clear_buy_waiting(db)

            print(
                "[WAIT EXPIRED] NAS BUY"
            )

    # SELL 대기 만료
    if row["resistance_ready"]:

        # row를 다시 읽는다.
        row2 = db.execute(
            """
            SELECT *
            FROM waiting
            WHERE symbol = 'NAS'
            """
        ).fetchone()

        if row2 and not sell_waiting_valid(row2):

            clear_sell_waiting(db)

            print(
                "[WAIT EXPIRED] NAS SELL"
            )


# =========================================================
# [13] WEBHOOK
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
        f"\n[WEBHOOK RECEIVED] "
        f"{message}"
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

    # NAS만 존재
    if symbol != "NAS":

        return {
            "status": "ignored",
            "reason": "non_nas_symbol",
            "message": message,
            "kst": kst_now_text(),
        }

    # =====================================================
    # DATABASE
    # =====================================================

    with database() as db:

        # -------------------------------------------------
        # 만료된 대기 정리
        # -------------------------------------------------

        ensure_nas_waiting(db)

        cleanup_expired_waiting(db)

        # =================================================
        # [A] 매수세력빠짐
        #
        # 대기 상태와 관계없이 즉시 신호 생성
        # =================================================

        if event == "close_buy":

            clear_buy_waiting(db)

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
        # 대기 상태와 관계없이 즉시 신호 생성
        # =================================================

        if event == "close_sell":

            clear_sell_waiting(db)

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
        # [D] NAS 지지구간
        #
        # 생성/진입 모두 여기로 들어옴
        #
        # 같은 방향 신호가 다시 오면
        # support_at을 현재 시간으로 갱신
        # =================================================

        if event == "support":

            current_time = now_iso()

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

            print(
                "[WAIT BUY] "
                "NAS 지지구간 → 60분 시작/갱신"
            )

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "BUY",
                "wait_seconds": WAIT_SECONDS,
                "kst": kst_now_text(),
            }

        # =================================================
        # [E] NAS 저항구간
        #
        # 생성/진입 모두 여기로 들어옴
        #
        # 같은 방향 신호가 다시 오면
        # resistance_at을 현재 시간으로 갱신
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

            print(
                "[WAIT SELL] "
                "NAS 저항구간 → 60분 시작/갱신"
            )

            return {
                "status": "waiting",
                "symbol": "NAS",
                "direction": "SELL",
                "wait_seconds": WAIT_SECONDS,
                "kst": kst_now_text(),
            }

        # =================================================
        # [F] 매수세력감지
        #
        # 반드시 현재 BUY 대기가 살아 있어야 함
        # =================================================

        if event == "buy_force":

            row = db.execute(
                """
                SELECT *
                FROM waiting
                WHERE symbol = 'NAS'
                """
            ).fetchone()

            # 대기 없음
            if row is None:

                print(
                    "[BUY IGNORED] "
                    "매수 대기 없음"
                )

                return {
                    "status": "ignored",
                    "reason": "no_buy_waiting",
                    "symbol": "NAS",
                    "kst": kst_now_text(),
                }

            # 대기 만료
            if not buy_waiting_valid(row):

                clear_buy_waiting(db)

                print(
                    "[BUY IGNORED] "
                    "60분 대기 만료"
                )

                return {
                    "status": "ignored",
                    "reason": "buy_waiting_expired",
                    "symbol": "NAS",
                    "kst": kst_now_text(),
                }

            # -------------------------------------------------
            # BUY 최종 신호
            # -------------------------------------------------

            clear_buy_waiting(db)

            signal_id = create_signal(
                db,
                "NAS",
                "BUY"
            )

            print(
                f"[BUY FINAL] signal_id={signal_id}"
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "BUY",
                "reason": "support_then_buy_force",
                "kst": kst_now_text(),
            }

        # =================================================
        # [G] 매도세력감지
        #
        # 반드시 현재 SELL 대기가 살아 있어야 함
        # =================================================

        if event == "sell_force":

            row = db.execute(
                """
                SELECT *
                FROM waiting
                WHERE symbol = 'NAS'
                """
            ).fetchone()

            # 대기 없음
            if row is None:

                print(
                    "[SELL IGNORED] "
                    "매도 대기 없음"
                )

                return {
                    "status": "ignored",
                    "reason": "no_sell_waiting",
                    "symbol": "NAS",
                    "kst": kst_now_text(),
                }

            # 대기 만료
            if not sell_waiting_valid(row):

                clear_sell_waiting(db)

                print(
                    "[SELL IGNORED] "
                    "60분 대기 만료"
                )

                return {
                    "status": "ignored",
                    "reason": "sell_waiting_expired",
                    "symbol": "NAS",
                    "kst": kst_now_text(),
                }

            # -------------------------------------------------
            # SELL 최종 신호
            # -------------------------------------------------

            clear_sell_waiting(db)

            signal_id = create_signal(
                db,
                "NAS",
                "SELL"
            )

            print(
                f"[SELL FINAL] signal_id={signal_id}"
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "SELL",
                "reason": "resistance_then_sell_force",
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
# [14] 현재 대기 상태
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

        buy_remaining = 0
        sell_remaining = 0

        # BUY 남은 시간
        if row["support_ready"]:

            support_at = datetime.fromisoformat(
                row["support_at"]
            )

            elapsed = (
                datetime.now(UTC)
                - support_at
            ).total_seconds()

            buy_remaining = max(
                0,
                int(
                    WAIT_SECONDS - elapsed
                )
            )

        # SELL 남은 시간
        if row["resistance_ready"]:

            resistance_at = datetime.fromisoformat(
                row["resistance_at"]
            )

            elapsed = (
                datetime.now(UTC)
                - resistance_at
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
# [15] 신호 확인
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
# [16] MT5 EXECUTOR → NEXT SIGNAL
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
            ),
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
# [17] MT5 EXECUTOR → ACK
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
    # 기존 status/detial 형식도 호환
    success = payload.get("success")

    if success is not None:
        status = "done" if bool(success) else "failed"
        detail = str(
            payload.get("result_detail", "")
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
# [18] HEALTH CHECK
# =========================================================

@app.get("/health")
def health():

    return {
        "status": "ok",

        "system":
            "NAS100 ONLY",

        "wait_minutes":
            WAIT_SECONDS // 60,

        "btc":
            "IGNORED",

        "trading_time":
            "UNLIMITED",

        "kst":
            kst_now_text(),
    }
