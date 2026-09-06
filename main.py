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
WAIT_SECONDS = int(os.getenv("WAIT_SECONDS", "1200"))
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "90"))

UTC = timezone.utc
KST = timezone(timedelta(hours=9))


# =========================================================
# 데이터베이스
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


# =========================================================
# 시간
# =========================================================

def now_iso() -> str:
    """
    DB에는 기존과 동일하게 UTC 기준으로 저장.
    """
    return datetime.now(UTC).isoformat()


def kst_now_text() -> str:
    """
    현재 한국시간을 문자열로 반환.
    """
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")


def is_trade_time_kst() -> bool:
    """
    실제 신규 매매가 허용되는 한국시간.

    허용:
        08:00 ~ 21:00
        22:35 ~ 다음날 05:00

    차단:
        05:00 ~ 08:00
        21:00 ~ 22:35
    """

    now = datetime.now(KST)

    current_minutes = now.hour * 60 + now.minute

    # 08:00 ~ 21:00
    if 8 * 60 <= current_minutes < 21 * 60:
        return True

    # 22:35 ~ 다음날 05:00
    if current_minutes >= 22 * 60 + 35 or current_minutes < 5 * 60:
        return True

    return False


# =========================================================
# DB 스키마
# =========================================================

def ensure_schema():
    with database() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS waiting (
            symbol TEXT PRIMARY KEY,
            direction TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL
                CHECK(direction IN ('BUY', 'SELL', 'CLOSE')),
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            lease_until TEXT,
            executor_id TEXT,
            result_detail TEXT
        );
        """)


@app.on_event("startup")
def startup():
    ensure_schema()


# =========================================================
# TradingView 메시지 파싱
# =========================================================

def parse_message(message: str) -> Optional[Tuple[str, str]]:
    """
    허용되는 메시지 예:

        NAS_지지구간
        NAS_저항구간
        NAS_0선돌파
        NAS_청산

        BTC_지지구간
        BTC_저항구간
        BTC_0선돌파
        BTC_청산
    """

    normalized = re.sub(r"\s+", "", message).upper()

    match = re.match(r"^(NAS|BTC)_(.+)$", normalized)

    if not match:
        return None

    symbol, event = match.groups()

    if "청산" in event:
        return symbol, "close"

    if "0선돌파" in event:
        return symbol, "zero_cross"

    if "지지구간" in event:
        return symbol, "support"

    if "저항구간" in event:
        return symbol, "resistance"

    return None


# =========================================================
# 최종 신호 생성
# =========================================================

def create_signal(
    db: sqlite3.Connection,
    symbol: str,
    direction: str
) -> int:

    cursor = db.execute(
        """
        INSERT INTO signals
        (symbol, direction, created_at)
        VALUES (?, ?, ?)
        """,
        (
            symbol,
            direction,
            now_iso()
        ),
    )

    return cursor.lastrowid


# =========================================================
# TradingView Webhook
# =========================================================

@app.post("/webhook")
async def tradingview_webhook(request: Request):

    raw_body = await request.body()

    parsed = parse_message(
        raw_body.decode(
            "utf-8",
            errors="replace"
        )
    )

    if parsed is None:
        return {
            "status": "ignored",
            "reason": "unsupported_message"
        }

    symbol, event = parsed

    with database() as db:

        # =================================================
        # 청산
        #
        # 청산은 24시간 허용
        # =================================================

        if event == "close":

            # 기존 대기 신호 제거
            db.execute(
                "DELETE FROM waiting WHERE symbol = ?",
                (symbol,)
            )

            # 청산 신호 생성
            signal_id = create_signal(
                db,
                symbol,
                "CLOSE"
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": symbol,
                "direction": "CLOSE",
                "kst": kst_now_text()
            }


        # =================================================
        # 지지 / 저항 구간
        #
        # 거래시간 안에서만 waiting 생성
        # =================================================

        if event in ("support", "resistance"):

            if not is_trade_time_kst():

                return {
                    "status": "ignored",
                    "reason": "outside_trading_hours",
                    "symbol": symbol,
                    "event": event,
                    "kst": kst_now_text()
                }

            direction = (
                "BUY"
                if event == "support"
                else "SELL"
            )

            db.execute(
                """
                INSERT INTO waiting
                (symbol, direction, updated_at)
                VALUES (?, ?, ?)

                ON CONFLICT(symbol)
                DO UPDATE SET
                    direction=excluded.direction,
                    updated_at=excluded.updated_at
                """,
                (
                    symbol,
                    direction,
                    now_iso()
                ),
            )

            return {
                "status": "waiting",
                "symbol": symbol,
                "direction": direction,
                "kst": kst_now_text()
            }


        # =================================================
        # 0선 돌파
        #
        # 거래시간 안에서만 최종 BUY / SELL 생성
        # =================================================

        if event == "zero_cross":

            # ---------------------------------------------
            # 거래시간 밖이면 신규 진입 차단
            #
            # 동시에 기존 waiting도 삭제해서
            # 거래금지 시간의 오래된 SMR이
            # 나중에 살아나는 것을 방지
            # ---------------------------------------------

            if not is_trade_time_kst():

                db.execute(
                    "DELETE FROM waiting WHERE symbol = ?",
                    (symbol,)
                )

                return {
                    "status": "ignored",
                    "reason": "outside_trading_hours",
                    "symbol": symbol,
                    "event": "zero_cross",
                    "kst": kst_now_text()
                }


            # ---------------------------------------------
            # 해당 종목의 SMR 대기 상태 확인
            # ---------------------------------------------

            waiting = db.execute(
                """
                SELECT *
                FROM waiting
                WHERE symbol = ?
                """,
                (symbol,)
            ).fetchone()


            # SMR 대기 상태가 없으면 무시
            if waiting is None:

                return {
                    "status": "ignored",
                    "reason": "no_smr_waiting",
                    "symbol": symbol,
                    "kst": kst_now_text()
                }


            # ---------------------------------------------
            # WAIT_SECONDS 만료 확인
            # ---------------------------------------------

            updated_at = datetime.fromisoformat(
                waiting["updated_at"]
            )

            if (
                datetime.now(UTC) - updated_at
                > timedelta(seconds=WAIT_SECONDS)
            ):

                db.execute(
                    "DELETE FROM waiting WHERE symbol = ?",
                    (symbol,)
                )

                return {
                    "status": "ignored",
                    "reason": "waiting_expired",
                    "symbol": symbol,
                    "kst": kst_now_text()
                }


            # ---------------------------------------------
            # waiting 소비
            # ---------------------------------------------

            db.execute(
                "DELETE FROM waiting WHERE symbol = ?",
                (symbol,)
            )


            # ---------------------------------------------
            # 최종 BUY / SELL 생성
            # ---------------------------------------------

            signal_id = create_signal(
                db,
                symbol,
                waiting["direction"]
            )

            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": symbol,
                "direction": waiting["direction"],
                "kst": kst_now_text()
            }


        # 혹시 모르는 이벤트
        return {
            "status": "ignored",
            "reason": "unknown_event",
            "symbol": symbol,
            "event": event
        }


# =========================================================
# MT5 실행기가 가져갈 다음 신호
# =========================================================

@app.get("/api/v1/signals/next")
def next_signal(executor_id: str):

    with database() as db:

        db.execute("BEGIN IMMEDIATE")

        now = now_iso()

        # 만료된 lease 복구
        db.execute(
            """
            UPDATE signals
            SET
                status='pending',
                lease_until=NULL,
                executor_id=NULL
            WHERE
                status='leased'
                AND lease_until < ?
            """,
            (now,)
        )


        # 가장 오래된 pending 신호
        row = db.execute(
            """
            SELECT *
            FROM signals
            WHERE status='pending'
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()


        if row is None:
            return {
                "signal": None
            }


        # 실행기 lease
        lease_until = (
            datetime.now(UTC)
            + timedelta(seconds=LEASE_SECONDS)
        ).isoformat()


        db.execute(
            """
            UPDATE signals
            SET
                status='leased',
                lease_until=?,
                executor_id=?
            WHERE id=?
            """,
            (
                lease_until,
                executor_id,
                row["id"]
            )
        )


        return {
            "signal": {
                "id": row["id"],
                "symbol": row["symbol"],
                "direction": row["direction"]
            }
        }


# =========================================================
# 신호 처리 결과 ACK
# =========================================================

@app.post("/api/v1/signals/{signal_id}/ack")
async def acknowledge(
    signal_id: int,
    request: Request
):

    payload = await request.json()

    status = payload.get("status")

    if status not in ("done", "failed"):
        raise HTTPException(
            status_code=422,
            detail="status must be done or failed"
        )


    with database() as db:

        updated = db.execute(
            """
            UPDATE signals
            SET
                status=?,
                result_detail=?,
                lease_until=NULL
            WHERE id=?
            """,
            (
                status,
                str(
                    payload.get(
                        "detail",
                        ""
                    )
                )[:500],
                signal_id
            )
        ).rowcount


    if updated != 1:
        raise HTTPException(
            status_code=404,
            detail="signal not found"
        )


    return {
        "status": "ok"
    }


# =========================================================
# Health Check
# =========================================================

@app.get("/health")
def health():

    return {
        "status": "ok",
        "kst": kst_now_text(),
        "trading_time": is_trade_time_kst()
    }
