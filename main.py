"""Cloudtype deployment: TradingView signal filter only (no MT5 connection)."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request

app = FastAPI(title="TradingView signal filter")

DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "signals.db"))
WAIT_SECONDS = int(os.getenv("WAIT_SECONDS", "1200"))
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "90"))
UTC = timezone.utc


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
            direction TEXT NOT NULL CHECK(direction IN ('BUY', 'SELL', 'CLOSE')),
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


def parse_message(message: str) -> tuple[str, str] | None:
    """Accept only explicit messages such as NAS_지지구간, BTC_0선돌파, NAS_청산."""
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


def create_signal(db: sqlite3.Connection, symbol: str, direction: str) -> int:
    cursor = db.execute(
        "INSERT INTO signals (symbol, direction, created_at) VALUES (?, ?, ?)",
        (symbol, direction, now_iso()),
    )
    return cursor.lastrowid


@app.post("/webhook")
async def tradingview_webhook(request: Request):
    parsed = parse_message((await request.body()).decode("utf-8", errors="replace"))
    if parsed is None:
        return {"status": "ignored", "reason": "unsupported_message"}
    symbol, event = parsed

    with database() as db:
        if event == "close":
            db.execute("DELETE FROM waiting WHERE symbol = ?", (symbol,))
            signal_id = create_signal(db, symbol, "CLOSE")
            return {"status": "final_signal", "id": signal_id, "symbol": symbol, "direction": "CLOSE"}

        if event in ("support", "resistance"):
            direction = "BUY" if event == "support" else "SELL"
            db.execute(
                "INSERT INTO waiting(symbol, direction, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET direction=excluded.direction, updated_at=excluded.updated_at",
                (symbol, direction, now_iso()),
            )
            return {"status": "waiting", "symbol": symbol, "direction": direction}

        waiting = db.execute("SELECT * FROM waiting WHERE symbol = ?", (symbol,)).fetchone()
        if waiting is None:
            return {"status": "ignored", "reason": "no_smr_waiting"}
        updated_at = datetime.fromisoformat(waiting["updated_at"])
        if datetime.now(UTC) - updated_at > timedelta(seconds=WAIT_SECONDS):
            db.execute("DELETE FROM waiting WHERE symbol = ?", (symbol,))
            return {"status": "ignored", "reason": "waiting_expired"}
        db.execute("DELETE FROM waiting WHERE symbol = ?", (symbol,))
        signal_id = create_signal(db, symbol, waiting["direction"])
        return {"status": "final_signal", "id": signal_id, "symbol": symbol, "direction": waiting["direction"]}


@app.get("/api/v1/signals/next")
def next_signal(executor_id: str):
    with database() as db:
        db.execute("BEGIN IMMEDIATE")
        now = now_iso()
        db.execute("UPDATE signals SET status='pending', lease_until=NULL, executor_id=NULL "
                   "WHERE status='leased' AND lease_until < ?", (now,))
        row = db.execute("SELECT * FROM signals WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return {"signal": None}
        lease_until = (datetime.now(UTC) + timedelta(seconds=LEASE_SECONDS)).isoformat()
        db.execute("UPDATE signals SET status='leased', lease_until=?, executor_id=? WHERE id=?",
                   (lease_until, executor_id, row["id"]))
        return {"signal": {"id": row["id"], "symbol": row["symbol"], "direction": row["direction"]}}


@app.post("/api/v1/signals/{signal_id}/ack")
async def acknowledge(signal_id: int, request: Request):
    payload = await request.json()
    status = payload.get("status")
    if status not in ("done", "failed"):
        raise HTTPException(status_code=422, detail="status must be done or failed")
    with database() as db:
        updated = db.execute("UPDATE signals SET status=?, result_detail=?, lease_until=NULL WHERE id=?",
                             (status, str(payload.get("detail", ""))[:500], signal_id)).rowcount
    if updated != 1:
        raise HTTPException(status_code=404, detail="signal not found")
    return {"status": "ok"}


@app.get("/health")
def health():
    return {"status": "ok"}
