import sqlite3
from datetime import datetime, timezone

from fastapi import FastAPI, Request


app = FastAPI()


# ============================================================
# 설정
# ============================================================

DB_PATH = "nas_test.db"

WAIT_SECONDS = 60 * 60


# ============================================================
# 시간
# ============================================================

def now():
    return datetime.now(timezone.utc)


def now_iso():
    return now().isoformat()


# ============================================================
# DB 초기화
# ============================================================

def init_db():

    with sqlite3.connect(DB_PATH) as db:

        db.execute("""
            CREATE TABLE IF NOT EXISTS waiting (
                id INTEGER PRIMARY KEY CHECK (id = 1),

                support_ready INTEGER NOT NULL DEFAULT 0,
                support_at TEXT,

                resistance_ready INTEGER NOT NULL DEFAULT 0,
                resistance_at TEXT
            )
        """)

        db.execute("""
            INSERT OR IGNORE INTO waiting
            (id, support_ready, resistance_ready)
            VALUES (1, 0, 0)
        """)

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


# ============================================================
# 대기시간 검사
# ============================================================

def is_valid_wait(timestamp_text):

    if not timestamp_text:

        return False

    try:

        signal_time = datetime.fromisoformat(
            timestamp_text
        )

        elapsed = (
            now() - signal_time
        ).total_seconds()

        return elapsed <= WAIT_SECONDS

    except Exception:

        return False


# ============================================================
# 신호 저장
# ============================================================

def create_signal(direction):

    with sqlite3.connect(DB_PATH) as db:

        cursor = db.execute(
            """
            INSERT INTO signals
            (direction, created_at, status)
            VALUES (?, ?, 'pending')
            """,
            (
                direction,
                now_iso()
            )
        )

        signal_id = cursor.lastrowid

    print(
        f"🔥 NAS SIGNAL "
        f"{signal_id} → {direction}"
    )

    return signal_id


# ============================================================
# NAS 조건 처리
# ============================================================

def process_nas_signal(message):

    clean = (
        message
        .replace(" ", "")
        .replace("\n", "")
        .replace("\r", "")
    )

    clean_upper = clean.upper()


    # --------------------------------------------------------
    # 지지구간
    # --------------------------------------------------------

    if (
        "NAS지지구간생성" in clean
        or "NAS지지구간진입" in clean
    ):

        with sqlite3.connect(DB_PATH) as db:

            db.execute(
                """
                UPDATE waiting
                SET
                    support_ready = 1,
                    support_at = ?
                WHERE id = 1
                """,
                (now_iso(),)
            )

        print(
            "🟢 NAS 지지구간 → "
            "매수 대기 시작 / 60분"
        )

        return {
            "status": "waiting",
            "waiting": "BUY",
            "reason": "support"
        }


    # --------------------------------------------------------
    # 저항구간
    # --------------------------------------------------------

    if (
        "NAS저항구간생성" in clean
        or "NAS저항구간진입" in clean
    ):

        with sqlite3.connect(DB_PATH) as db:

            db.execute(
                """
                UPDATE waiting
                SET
                    resistance_ready = 1,
                    resistance_at = ?
                WHERE id = 1
                """,
                (now_iso(),)
            )

        print(
            "🔴 NAS 저항구간 → "
            "매도 대기 시작 / 60분"
        )

        return {
            "status": "waiting",
            "waiting": "SELL",
            "reason": "resistance"
        }


    # --------------------------------------------------------
    # 세력 빠짐 → 청산
    # --------------------------------------------------------

    if "세력빠짐" in clean:

        signal_id = create_signal(
            "세력빠짐"
        )

        return {
            "status": "ok",
            "id": signal_id,
            "direction": "세력빠짐"
        }


    # --------------------------------------------------------
    # NAS100 상승
    # --------------------------------------------------------

    if "NAS100상승" in clean_upper:

        with sqlite3.connect(DB_PATH) as db:

            row = db.execute(
                """
                SELECT
                    support_ready,
                    support_at
                FROM waiting
                WHERE id = 1
                """
            ).fetchone()


            if row is None:

                return {
                    "status": "ignored",
                    "reason": "no_waiting"
                }


            support_ready = row[0]
            support_at = row[1]


            # 지지구간이 먼저 있어야 함
            if not support_ready:

                print(
                    "⚪ NAS100 상승 → "
                    "지지구간 없음 / 무시"
                )

                return {
                    "status": "ignored",
                    "reason": "no_support"
                }


            # 60분 초과
            if not is_valid_wait(support_at):

                db.execute(
                    """
                    UPDATE waiting
                    SET
                        support_ready = 0,
                        support_at = NULL
                    WHERE id = 1
                    """
                )

                print(
                    "⏰ NAS 매수 대기 만료"
                )

                return {
                    "status": "ignored",
                    "reason": "support_expired"
                }


            # 매수 조건 완성
            db.execute(
                """
                UPDATE waiting
                SET
                    support_ready = 0,
                    support_at = NULL
                WHERE id = 1
                """
            )


        signal_id = create_signal(
            "매수세력감지"
        )

        print(
            "🟢 NAS 매수 조건 완성 → "
            "매수세력감지"
        )

        return {
            "status": "ok",
            "id": signal_id,
            "direction": "매수세력감지"
        }


    # --------------------------------------------------------
    # NAS100 하락
    # --------------------------------------------------------

    if "NAS100하락" in clean_upper:

        with sqlite3.connect(DB_PATH) as db:

            row = db.execute(
                """
                SELECT
                    resistance_ready,
                    resistance_at
                FROM waiting
                WHERE id = 1
                """
            ).fetchone()


            if row is None:

                return {
                    "status": "ignored",
                    "reason": "no_waiting"
                }


            resistance_ready = row[0]
            resistance_at = row[1]


            # 저항구간이 먼저 있어야 함
            if not resistance_ready:

                print(
                    "⚪ NAS100 하락 → "
                    "저항구간 없음 / 무시"
                )

                return {
                    "status": "ignored",
                    "reason": "no_resistance"
                }


            # 60분 초과
            if not is_valid_wait(resistance_at):

                db.execute(
                    """
                    UPDATE waiting
                    SET
                        resistance_ready = 0,
                        resistance_at = NULL
                    WHERE id = 1
                    """
                )

                print(
                    "⏰ NAS 매도 대기 만료"
                )

                return {
                    "status": "ignored",
                    "reason": "resistance_expired"
                }


            # 매도 조건 완성
            db.execute(
                """
                UPDATE waiting
                SET
                    resistance_ready = 0,
                    resistance_at = NULL
                WHERE id = 1
                """
            )


        signal_id = create_signal(
            "매도세력감지"
        )

        print(
            "🔴 NAS 매도 조건 완성 → "
            "매도세력감지"
        )

        return {
            "status": "ok",
            "id": signal_id,
            "direction": "매도세력감지"
        }


    # --------------------------------------------------------
    # 기타 신호
    # --------------------------------------------------------

    print(
        "⚪ 무시된 신호:",
        message
    )

    return {
        "status": "ignored",
        "message": message
    }


# ============================================================
# TradingView Webhook
# ============================================================

@app.post("/webhook")
async def webhook(request: Request):

    body = await request.body()

    message = body.decode(
        "utf-8",
        errors="ignore"
    ).strip()


    print(
        "📩 TradingView:",
        message
    )


    return process_nas_signal(
        message
    )


# ============================================================
# MT5가 가져갈 다음 신호
# ============================================================

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


# ============================================================
# 정상 처리 완료
# ============================================================

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


# ============================================================
# 주문 실패 시 다시 대기
# ============================================================

@app.post("/retry/{signal_id}")
def retry(signal_id: int):

    with sqlite3.connect(DB_PATH) as db:

        db.execute(
            """
            UPDATE signals
            SET status = 'pending'
            WHERE id = ?
            """,
            (signal_id,)
        )


    return {
        "status": "pending"
    }


# ============================================================
# 상태 확인
# ============================================================

@app.get("/health")
def health():

    return {
        "status": "ok",
        "symbol": "NAS100",
        "wait_minutes": 60
    }


# ============================================================
# 실행
# ============================================================

if __name__ == "__main__":

    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000
    )
