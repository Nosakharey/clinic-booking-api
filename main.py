"""Clinic booking API (Koya Cohort 4 assessment, Task 1).

One FastAPI app that:
  * implements the spec at koyaacademy.com/assessment-api (levels 1-5)
  * serves a small web page at "/" that uses the API

Storage is Postgres (set DATABASE_URL). Without DATABASE_URL it falls back to a
local SQLite file, which is only meant for trying it out on your laptop.
"""
import base64
import hashlib
import json
import math
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import (
    Column, Integer, MetaData, String, Table, Text, create_engine, delete,
    func, insert, select, update, Index, text,
)
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------
API_KEY = os.getenv("API_KEY", "dev-key")
RATE_LIMIT = 5          # booking attempts...
RATE_WINDOW = 60        # ...per email per this many seconds


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "sqlite:///./clinic.db")
    # Neon / Render give "postgres://" or "postgresql://"; SQLAlchemy needs the driver name.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return url


DB_URL = _db_url()
engine = create_engine(DB_URL, pool_pre_ping=True, future=True,
                       **({} if DB_URL.startswith("sqlite") else {"pool_size": 10, "max_overflow": 20}))

# --------------------------------------------------------------------------
# Tables
# --------------------------------------------------------------------------
meta = MetaData()

slots = Table(
    "slots", meta,
    Column("id", String, primary_key=True),
    Column("provider", String, nullable=False),
    Column("start", String, nullable=False),      # as sent to /reset
    Column("end", String, nullable=False),
    Column("start_utc", String, nullable=False),  # normalised, sortable
)

bookings = Table(
    "bookings", meta,
    Column("seq", Integer, primary_key=True, autoincrement=True),  # order + cursor
    Column("id", String, unique=True, nullable=False),
    Column("slot_id", String, nullable=False),
    Column("name", String, nullable=False),
    Column("email", String, nullable=False),
    Column("status", String, nullable=False),
    Column("created_at", String, nullable=False),
)

# Level 3: the database itself refuses a second *confirmed* booking for a slot.
# This holds even when many requests arrive at the same moment.
Index(
    "one_confirmed_booking_per_slot", bookings.c.slot_id, unique=True,
    postgresql_where=text("status = 'confirmed'"),
    sqlite_where=text("status = 'confirmed'"),
)
Index("bookings_by_email", bookings.c.email, bookings.c.seq)

idempotency = Table(
    "idempotency", meta,
    Column("key", String, primary_key=True),
    Column("body_hash", String, nullable=False),
    Column("status_code", Integer, nullable=False),
    Column("response", Text, nullable=False),
)

attempts = Table(
    "booking_attempts", meta,
    Column("seq", Integer, primary_key=True, autoincrement=True),
    Column("email", String, nullable=False),
    Column("at", String, nullable=False),  # epoch seconds, stored as text
)

meta.create_all(engine)

# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_dt(value) -> datetime:
    """Parse an ISO 8601 date-time. Times without a zone are treated as UTC."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("not a date-time")
    v = value.strip()
    if v.endswith("Z") or v.endswith("z"):
        v = v[:-1] + "+00:00"
    dt = datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def err(status: int, message: str, headers=None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def booking_json(row) -> dict:
    return {
        "id": row.id,
        "slot_id": row.slot_id,
        "name": row.name,
        "email": row.email,
        "status": row.status,
        "created_at": row.created_at,
    }


def encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(str(seq).encode()).decode()


def decode_cursor(cursor: str) -> int:
    return int(base64.urlsafe_b64decode(cursor.encode()).decode())


# --------------------------------------------------------------------------
# App, auth and error format
# --------------------------------------------------------------------------
app = FastAPI(title="Clinic booking API")
PUBLIC_PATHS = {"/", "/health"}
WEB_PAGE = Path(__file__).parent / "static" / "index.html"


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Level 2: every API request needs the right X-API-Key header."""
    if request.url.path not in PUBLIC_PATHS and request.method != "OPTIONS":
        if request.headers.get("X-API-Key") != API_KEY:
            return err(401, "missing or invalid API key")
    return await call_next(request)


# Make *every* error look like {"error": "..."}, including FastAPI's own ones.
@app.exception_handler(StarletteHTTPException)
async def http_error(_request, exc):
    return err(exc.status_code, str(exc.detail).lower())


@app.exception_handler(RequestValidationError)
async def validation_error(_request, _exc):
    return err(400, "invalid request")


@app.exception_handler(Exception)
async def unexpected_error(_request, _exc):
    return err(500, "internal error")


async def read_json(request: Request):
    try:
        return json.loads(await request.body() or b"null")
    except (ValueError, UnicodeDecodeError):
        return None


# --------------------------------------------------------------------------
# Web page
# --------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
def web_page():
    return FileResponse(WEB_PAGE)


@app.get("/health", include_in_schema=False)
def health():
    return {"ok": True}


# --------------------------------------------------------------------------
# Level 1: POST /reset
# --------------------------------------------------------------------------
@app.post("/reset")
async def reset(request: Request):
    body = await read_json(request)
    return await run_in_threadpool(_reset, body)


def _reset(body):
    if not isinstance(body, dict) or not isinstance(body.get("slots"), list):
        return err(400, "body must be {\"slots\": [...]}")

    rows = []
    seen = set()
    for s in body["slots"]:
        if not isinstance(s, dict):
            return err(400, "each slot must be an object")
        sid, provider = s.get("id"), s.get("provider")
        if not isinstance(sid, str) or not sid or not isinstance(provider, str):
            return err(400, "each slot needs id and provider")
        if sid in seen:
            return err(400, f"duplicate slot id {sid}")
        seen.add(sid)
        try:
            start_utc = iso(parse_dt(s.get("start")))
            parse_dt(s.get("end"))
        except ValueError:
            return err(400, "slot start and end must be ISO 8601 date-times")
        rows.append({"id": sid, "provider": provider, "start": s["start"],
                     "end": s["end"], "start_utc": start_utc})

    with engine.begin() as conn:  # one transaction: all or nothing
        conn.execute(delete(bookings))
        conn.execute(delete(idempotency))
        conn.execute(delete(attempts))
        conn.execute(delete(slots))
        if rows:
            conn.execute(insert(slots), rows)
    return {"ok": True, "slots": len(rows)}


# --------------------------------------------------------------------------
# Level 1: GET /slots
# --------------------------------------------------------------------------
@app.get("/slots")
def list_slots(provider: str | None = None):
    q = select(slots, slots.c.id.in_(select(bookings.c.slot_id)
                                     .where(bookings.c.status == "confirmed"))
               .label("booked")).order_by(slots.c.start_utc, slots.c.id)
    if provider is not None:
        q = q.where(slots.c.provider == provider)
    with engine.connect() as conn:
        rows = conn.execute(q).all()
    return {"slots": [
        {"id": r.id, "provider": r.provider, "start": r.start, "end": r.end,
         "available": not r.booked}
        for r in rows
    ]}


# --------------------------------------------------------------------------
# Levels 1-5: POST /bookings
# --------------------------------------------------------------------------
def _rate_limited(email: str):
    """Level 5: count this attempt; return seconds to wait if over the limit."""
    now = now_utc().timestamp()
    window_start = now - RATE_WINDOW
    with engine.begin() as conn:
        if engine.dialect.name == "postgresql":
            # Serialise attempts for the same email so bursts can't slip past the limit.
            conn.execute(text("SELECT pg_advisory_xact_lock(hashtext(:e))"), {"e": email})
        conn.execute(delete(attempts).where(attempts.c.at < f"{window_start:017.6f}"))
        recent = conn.execute(
            select(attempts.c.at).where(attempts.c.email == email)
            .order_by(attempts.c.at)
        ).scalars().all()
        if len(recent) >= RATE_LIMIT:
            oldest = float(recent[-RATE_LIMIT])
            return max(1, math.ceil(oldest + RATE_WINDOW - now))
        conn.execute(insert(attempts).values(email=email, at=f"{now:017.6f}"))
    return None


def _create_booking(body) -> tuple[int, dict]:
    """Validate and create a booking. Returns (status_code, response_body)."""
    if not isinstance(body, dict):
        return 400, {"error": "body must be a JSON object"}

    fields = {}
    for f in ("slot_id", "name", "email"):
        v = body.get(f)
        if not isinstance(v, str) or not v.strip():
            return 400, {"error": f"{f} is required"}
        fields[f] = v.strip()
    email = fields["email"].lower()
    if not EMAIL_RE.match(email):
        return 400, {"error": "email is not valid"}

    with engine.connect() as conn:
        slot = conn.execute(
            select(slots).where(slots.c.id == fields["slot_id"])
        ).first()
    if slot is None:
        return 404, {"error": "slot not found"}
    if parse_dt(slot.start) <= now_utc():
        return 400, {"error": "slot has already started"}

    row = {
        "id": "bk_" + uuid.uuid4().hex[:16],
        "slot_id": slot.id,
        "name": fields["name"],
        "email": email,
        "status": "confirmed",
        "created_at": iso(now_utc()),
    }
    try:
        with engine.begin() as conn:
            conn.execute(insert(bookings).values(**row))
    except IntegrityError:
        # The unique index rejected a second confirmed booking for this slot.
        return 409, {"error": "slot already booked"}
    return 201, row


PENDING = 0  # status_code of an idempotency key whose request is still running


def _replay(key: str, body_hash: str):
    """Wait for the first request with this key to finish, then return its result."""
    import time
    for _ in range(100):  # up to ~10 seconds
        with engine.connect() as conn:
            saved = conn.execute(
                select(idempotency).where(idempotency.c.key == key)
            ).first()
        if saved is None:
            return None  # the first request gave the key up (e.g. it was rate limited)
        if saved.body_hash != body_hash:
            return err(422, "Idempotency-Key was already used with a different body")
        if saved.status_code != PENDING:
            return JSONResponse(json.loads(saved.response), status_code=saved.status_code)
        time.sleep(0.1)
    return err(409, "a request with this Idempotency-Key is still in progress")


@app.post("/bookings")
async def create_booking(request: Request):
    raw = await request.body()
    body = await read_json(request)
    key = request.headers.get("Idempotency-Key")
    # The database work runs in a worker thread so many requests run side by side.
    return await run_in_threadpool(_handle_booking, raw, body, key)


def _handle_booking(raw: bytes, body, key):
    # Compare bodies by their JSON content, so spacing or key order doesn't matter.
    canonical = json.dumps(body, sort_keys=True) if body is not None else raw.decode("utf-8", "replace")
    body_hash = hashlib.sha256(canonical.encode()).hexdigest()

    # Level 4: claim the Idempotency-Key before doing anything. If another request
    # already claimed it, return that request's response instead of booking again.
    if key:
        while True:
            try:
                with engine.begin() as conn:
                    conn.execute(insert(idempotency).values(
                        key=key, body_hash=body_hash, status_code=PENDING, response=""))
                break  # we own the key
            except IntegrityError:
                replay = _replay(key, body_hash)
                if replay is not None:
                    return replay
                # the key was released; try to claim it again

    def release_key():
        if key:
            with engine.begin() as conn:
                conn.execute(delete(idempotency).where(idempotency.c.key == key))

    try:
        # Level 5: rate limit per email (failed attempts count too).
        if isinstance(body, dict) and isinstance(body.get("email"), str) and body["email"].strip():
            wait = _rate_limited(body["email"].strip().lower())
            if wait is not None:
                release_key()  # a later retry with this key should be tried for real
                return err(429, "too many booking attempts for this email",
                           headers={"Retry-After": str(wait)})

        status, payload = _create_booking(body)
    except Exception:
        release_key()
        raise

    if key:
        with engine.begin() as conn:
            conn.execute(update(idempotency).where(idempotency.c.key == key)
                         .values(status_code=status, response=json.dumps(payload)))
    return JSONResponse(payload, status_code=status)


# --------------------------------------------------------------------------
# Level 4: GET /bookings?email=...&limit=...&cursor=...
# --------------------------------------------------------------------------
@app.get("/bookings")
def list_bookings(request: Request):
    params = request.query_params
    email = (params.get("email") or "").strip().lower()
    if not email:
        return err(400, "email is required")

    limit_raw = params.get("limit")
    if limit_raw is None:
        limit = 10
    else:
        if not re.fullmatch(r"\d+", limit_raw.strip()):
            return err(400, "limit must be an integer from 1 to 50")
        limit = int(limit_raw)
        if not 1 <= limit <= 50:
            return err(400, "limit must be an integer from 1 to 50")

    after = 0
    cursor = params.get("cursor")
    if cursor:
        try:
            after = decode_cursor(cursor)
        except (ValueError, UnicodeDecodeError):
            return err(400, "invalid cursor")

    with engine.connect() as conn:
        rows = conn.execute(
            select(bookings)
            .where(func.lower(bookings.c.email) == email, bookings.c.seq > after)
            .order_by(bookings.c.seq)
            .limit(limit + 1)  # one extra tells us if there is another page
        ).all()

    page = rows[:limit]
    next_cursor = encode_cursor(page[-1].seq) if len(rows) > limit else None
    return {"bookings": [booking_json(r) for r in page], "next_cursor": next_cursor}


# --------------------------------------------------------------------------
# Level 1: GET /bookings/{id}   Level 3: DELETE /bookings/{id}
# --------------------------------------------------------------------------
@app.get("/bookings/{booking_id}")
def get_booking(booking_id: str):
    with engine.connect() as conn:
        row = conn.execute(select(bookings).where(bookings.c.id == booking_id)).first()
    if row is None:
        return err(404, "booking not found")
    return booking_json(row)


@app.delete("/bookings/{booking_id}")
def cancel_booking(booking_id: str):
    with engine.begin() as conn:
        # Only flips confirmed -> cancelled, so two cancels can't both succeed.
        result = conn.execute(
            update(bookings)
            .where(bookings.c.id == booking_id, bookings.c.status == "confirmed")
            .values(status="cancelled")
        )
        row = conn.execute(select(bookings).where(bookings.c.id == booking_id)).first()
    if row is None:
        return err(404, "booking not found")
    if result.rowcount == 0:
        return err(409, "booking already cancelled")
    return booking_json(row)
