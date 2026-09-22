import os
import re
import sqlite3

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Tuple

from fastapi import FastAPI, HTTPException, Request


app = FastAPI(title="Ella NAS100 Signal Filter")

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "signals.db"))

WAIT_SECONDS = 120 * 60
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "90"))

UTC = timezone.utc
KST = timezone(timedelta(hours=9))

EVENT_GROUP = {
    "support": "BUY",
    "buy_force": "BUY",
    "resistance": "SELL",
    "sell_force": "SELL",
}

EVENT_COMPLEMENT = {
    "support": "buy_force",
    "buy_force": "support",
    "resistance": "sell_force",
    "sell_force": "resistance",
}


@contextmanager
def database():
    connection = sqlite3.connect(DATABASE_PATH, timeout=30)
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


def ensure_schema():
    with database() as db:
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

        existing = {
            row["name"]
            for row in db.execute("PRAGMA table_info(waiting)").fetchall()
        }

        new_columns = {
            "buy_force_ready": "INTEGER NOT NULL DEFAULT 0",
            "buy_force_at": "TEXT",
            "sell_force_ready": "INTEGER NOT NULL DEFAULT 0",
            "sell_force_at": "TEXT",
            "last_type": "TEXT",
            "last_at": "TEXT",
        }

        for column, definition in new_columns.items():
            if column not in existing:
                db.execute(f"ALTER TABLE waiting ADD COLUMN {column} {definition}")

        db.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL
                CHECK(direction IN ('BUY','SELL','CLOSE','CLOSE_BUY','CLOSE_SELL')),
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


def parse_message(message: str) -> Optional[Tuple[str, str]]:
    normalized = re.sub(r"[^A-Z0-9가-힣_]", "", message.upper())

    if normalized.startswith("BTC"):
        return None

    if "매수세력빠짐" in normalized:
        return "NAS", "close_buy"

    if "매도세력빠짐" in normalized:
        return "NAS", "close_sell"

    if "매수세력감지" in normalized:
        return "NAS", "buy_force"

    if "매도세력감지" in normalized:
        return "NAS", "sell_force"

    if normalized.startswith("NAS지지구간생성") or normalized.startswith("NAS지지구간진입"):
        return "NAS", "support"

    if normalized.startswith("NAS저항구간생성") or normalized.startswith("NAS저항구간진입"):
        return "NAS", "resistance"

    if normalized.startswith("NAS_지지구간"):
        return "NAS", "support"

    if normalized.startswith("NAS_저항구간"):
        return "NAS", "resistance"

    return None


def create_signal(db, symbol, direction):
    cursor = db.execute(
        "INSERT INTO signals (symbol, direction, created_at) VALUES (?, ?, ?)",
        (symbol, direction, now_iso()),
    )
    return cursor.lastrowid


def ensure_nas_waiting(db):
    row = db.execute("SELECT * FROM waiting WHERE symbol = 'NAS'").fetchone()
    if row is not None:
        return

    db.execute(
        "INSERT INTO waiting (symbol, last_type, last_at, updated_at) VALUES ('NAS', NULL, NULL, ?)",
        (now_iso(),),
    )


def get_last_event(db):
    return db.execute("SELECT * FROM waiting WHERE symbol = 'NAS'").fetchone()


def last_event_is_valid(row) -> bool:
    if row is None or not row["last_type"] or not row["last_at"]:
        return False

    last_at = datetime.fromisoformat(row["last_at"])
    return datetime.now(UTC) - last_at <= timedelta(seconds=WAIT_SECONDS)


def set_last_event(db, event: str):
    current_time = now_iso()
    db.execute(
        "UPDATE waiting SET last_type = ?, last_at = ?, updated_at = ? WHERE symbol = 'NAS'",
        (event, current_time, current_time),
    )


def clear_last_event(db):
    current_time = now_iso()
    db.execute(
        "UPDATE waiting SET last_type = NULL, last_at = NULL, updated_at = ? WHERE symbol = 'NAS'",
        (current_time,),
    )


def handle_pairable_event(db, event: str):
    row = get_last_event(db)
    direction = EVENT_GROUP[event]
    complement = EVENT_COMPLEMENT[event]

    if last_event_is_valid(row) and row["last_type"] == complement:
        clear_last_event(db)
        signal_id = create_signal(db, "NAS", direction)

        print(f"[{direction} FINAL] signal_id={signal_id} reason={complement}_then_{event}")

        return {
            "status": "final_signal",
            "id": signal_id,
            "symbol": "NAS",
            "direction": direction,
            "reason": f"{complement}_then_{event}_within_120_minutes",
            "kst": kst_now_text(),
        }

    set_last_event(db, event)
    print(f"[WAIT {direction}] 마지막 신호를 '{event}'로 갱신")

    return {
        "status": "waiting",
        "symbol": "NAS",
        "direction": direction,
        "event": event,
        "wait_seconds": WAIT_SECONDS,
        "kst": kst_now_text(),
    }


@app.post("/webhook")
async def tradingview_webhook(request: Request):
    raw_body = await request.body()
    message = raw_body.decode("utf-8", errors="replace").strip()

    print(f"\n[WEBHOOK RECEIVED] {message}")

    parsed = parse_message(message)

    if parsed is None:
        print("[IGNORED] BTC 또는 알 수 없는 신호")
        return {"status": "ignored", "message": message, "kst": kst_now_text()}

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

        if event == "close_buy":
            signal_id = create_signal(db, "NAS", "CLOSE_BUY")
            print(f"[CLOSE_BUY] signal_id={signal_id}")
            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "CLOSE_BUY",
                "kst": kst_now_text(),
            }

        if event == "close_sell":
            signal_id = create_signal(db, "NAS", "CLOSE_SELL")
            print(f"[CLOSE_SELL] signal_id={signal_id}")
            return {
                "status": "final_signal",
                "id": signal_id,
                "symbol": "NAS",
                "direction": "CLOSE_SELL",
                "kst": kst_now_text(),
            }

        if event in ("support", "resistance", "buy_force", "sell_force"):
            return handle_pairable_event(db, event)

        return {
            "status": "ignored",
            "reason": "unknown_event",
            "symbol": "NAS",
            "event": event,
            "kst": kst_now_text(),
        }


@app.get("/waiting/NAS")
def get_nas_waiting():
    with database() as db:
        ensure_nas_waiting(db)
        row = get_last_event(db)

        if not last_event_is_valid(row):
            if row is not None and row["last_type"]:
                clear_last_event(db)
            return {"symbol": "NAS", "waiting": False, "kst": kst_now_text()}

        last_at = datetime.fromisoformat(row["last_at"])
        elapsed = (datetime.now(UTC) - last_at).total_seconds()
        remaining = max(0, int(WAIT_SECONDS - elapsed))

        return {
            "symbol": "NAS",
            "waiting": True,
            "last_event": row["last_type"],
            "direction": EVENT_GROUP[row["last_type"]],
            "remaining_seconds": remaining,
            "remaining_minutes": round(remaining / 60, 1),
            "kst": kst_now_text(),
        }


@app.get("/signal/NAS")
def get_nas_signals():
    with database() as db:
        rows = db.execute(
            """
            SELECT id, symbol, direction, created_at, status, executor_id, result_detail
            FROM signals
            WHERE symbol = 'NAS'
            ORDER BY id DESC
            LIMIT 50
            """
        ).fetchall()

        return {"symbol": "NAS", "signals": [dict(row) for row in rows]}


@app.get("/api/v1/signals/next")
def next_signal(executor_id: str):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        now = now_iso()

        db.execute(
            """
            UPDATE signals
            SET status = 'pending', lease_until = NULL, executor_id = NULL
            WHERE status = 'leased' AND lease_until < ?
            """,
            (now,),
        )

        row = db.execute(
            "SELECT * FROM signals WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()

        if row is None:
            return {"signal": None}

        lease_until = (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat()

        db.execute(
            "UPDATE signals SET status = 'leased', lease_until = ?, executor_id = ? WHERE id = ?",
            (lease_until, executor_id, row["id"]),
        )

        print(f"[SIGNAL SENT] id={row['id']} {row['symbol']} {row['direction']} executor={executor_id}")

        return {
            "signal": {
                "id": row["id"],
                "symbol": row["symbol"],
                "direction": row["direction"],
                "created_at": row["created_at"],
            }
        }


@app.post("/api/v1/signals/{signal_id}/ack")
async def acknowledge(signal_id: int, request: Request):
    payload = await request.json()

    success = payload.get("success")

    if success is not None:
        status = "done" if bool(success) else "failed"
        detail = str(payload.get("result_detail", ""))[:500]
    else:
        status = payload.get("status")
        if status not in ("done", "failed"):
            raise HTTPException(
                status_code=422,
                detail="success must be true/false or status must be done/failed",
            )
        detail = str(payload.get("detail", ""))[:500]

    with database() as db:
        updated = db.execute(
            "UPDATE signals SET status = ?, result_detail = ?, lease_until = NULL WHERE id = ?",
            (status, detail, signal_id),
        ).rowcount

    if updated != 1:
        raise HTTPException(status_code=404, detail="signal not found")

    print(f"[ACK] id={signal_id} status={status}")
    return {"status": "ok"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "system": "NAS100 ONLY",
        "wait_minutes": WAIT_SECONDS // 60,
        "pair_mode": "LAST_SIGNAL_ONLY",
        "btc": "IGNORED",
        "trading_time": "UNLIMITED",
        "kst": kst_now_text(),
    }
