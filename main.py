import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, Request

app = FastAPI()

DB_PATH = "btc_test.db"


def now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                direction TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending'
            )
        """)


@app.on_event("startup")
def startup():
    init_db()


@app.post("/webhook")
async def webhook(request: Request):

    body = await request.body()

    message = body.decode(
        "utf-8",
        errors="ignore"
    ).strip()

    print("TradingView:", message)

    clean = message.replace(" ", "").upper()

    if "상승" in clean:

        direction = "BUY"

    elif "하락" in clean:

        direction = "SELL"

    else:

        return {
            "status": "ignored",
            "message": message
        }

    with sqlite3.connect(DB_PATH) as db:

        cursor = db.execute(
            """
            INSERT INTO signals
            (direction, created_at)
            VALUES (?, ?)
            """,
            (
                direction,
                now()
            )
        )

        signal_id = cursor.lastrowid

    print(
        f"🔥 BTC TEST SIGNAL "
        f"{signal_id} → {direction}"
    )

    return {
        "status": "ok",
        "id": signal_id,
        "symbol": "BTC",
        "direction": direction
    }


@app.get("/next")
def next_signal():

    with sqlite3.connect(DB_PATH) as db:

        db.row_factory = sqlite3.Row

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

        db.execute(
            """
            UPDATE signals
            SET status = 'processing'
            WHERE id = ?
            """,
            (row["id"],)
        )

        return {
            "signal": {
                "id": row["id"],
                "direction": row["direction"]
            }
        }


@app.post("/complete/{signal_id}")
def complete(signal_id: int):

    with sqlite3.connect(DB_PATH) as db:

        db.execute(
            """
            UPDATE signals
            SET status = 'done'
            WHERE id = ?
            """,
            (signal_id,)
        )

    return {
        "status": "done"
    }


@app.get("/health")
def health():

    return {
        "status": "ok"
    }


if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000
    )
