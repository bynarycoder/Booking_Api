"""Clinic booking API -- Koya Cohort 4, Task 1.

Implements the contract published at https://koyaacademy.com/assessment-api
Levels 1-5: CRUD, API key + validation, race-free booking with cancellation,
idempotent retries with cursor pagination, and per-email rate limiting.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager
from datetime import timedelta

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response

from store import Store, UniqueViolation, now_ms

API_KEY = os.getenv("API_KEY", "clinic-demo-key-2026")
RATE_LIMIT = 5              # attempts
RATE_WINDOW_MS = 60_000     # per minute
DEFAULT_LIMIT = 10
MAX_LIMIT = 50

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$")

STATIC_DIR = Path(__file__).parent / "static"

store = Store()


def demo_slots(days: int = 12) -> list[dict]:
    """A fortnight of plausible demo availability, starting tomorrow (UTC)."""
    providers = [("Dr Ade", 9, 0, 30), ("Dr Bello", 11, 0, 30), ("Dr Chi", 14, 0, 45)]
    out, day = [], datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    made = 0
    while made < days:
        if day.weekday() != 6:  # closed on Sundays
            for idx, (provider, hour, minute, length) in enumerate(providers):
                start = day.replace(hour=hour, minute=minute)
                end = start + timedelta(minutes=length)
                out.append({
                    "id": f"slot-{start.strftime('%Y%m%d')}-{idx + 1}",
                    "provider": provider,
                    "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "start_ms": int(start.timestamp() * 1000),
                    "end_ms": int(end.timestamp() * 1000),
                })
            made += 1
        day += timedelta(days=1)
    return out


@asynccontextmanager
async def lifespan(_: FastAPI):
    if os.getenv("SEED_DEMO_SLOTS", "1") not in ("0", "false", ""):
        store.seed_if_empty(demo_slots())
    yield


app = FastAPI(title="Clinic booking API", version="1.0", docs_url="/docs", redoc_url=None, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Retry-After"],
)


# --------------------------------------------------------------------- helpers
def err(status: int, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status, headers=headers)


def iso_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def parse_dt(value) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def booking_json(row: dict) -> dict:
    return {
        "id": row["id"],
        "slot_id": row["slot_id"],
        "name": row["name"],
        "email": row["email"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


def slot_json(row: dict) -> dict:
    return {
        "id": row["id"],
        "provider": row["provider"],
        "start": row["start_at"],
        "end": row["end_at"],
        "available": bool(row["available"]),
    }


async def json_body(request: Request):
    """Parse the body ourselves so every failure is `{"error": ...}` on a 400."""
    raw = await request.body()
    if not raw:
        return None, None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    return parsed, raw


def authed(request: Request) -> bool:
    supplied = request.headers.get("x-api-key")
    if supplied is None:
        return False
    return hmac.compare_digest(supplied, API_KEY)


def encode_cursor(row: dict) -> str:
    raw = f"{row['created_ms']}:{row['seq']}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str) -> tuple[int, int] | None:
    try:
        padded = value + "=" * (-len(value) % 4)
        created, seq = base64.urlsafe_b64decode(padded.encode()).decode().split(":")
        return int(created), int(seq)
    except Exception:  # noqa: BLE001 - any malformed cursor is a 400
        return None


# --------------------------------------------------------------------- routes
@app.get("/", response_class=HTMLResponse)
def index():
    page = STATIC_DIR / "index.html"
    return HTMLResponse(page.read_text(encoding="utf-8"))


@app.get("/health")
def health():
    return {"status": "ok", "time": iso_now()}


@app.post("/reset")
async def reset(request: Request):
    """Level 1 - delete every booking and replace all slots."""
    if not authed(request):
        return err(401, "missing or invalid API key")

    body, _ = await json_body(request)
    if body is None or not isinstance(body.get("slots"), list):
        return err(400, "body must be a JSON object with a 'slots' array")

    cleaned: list[dict] = []
    for i, raw_slot in enumerate(body["slots"]):
        if not isinstance(raw_slot, dict):
            return err(400, f"slots[{i}] must be an object")
        slot_id = raw_slot.get("id")
        provider = raw_slot.get("provider")
        start_dt = parse_dt(raw_slot.get("start"))
        end_dt = parse_dt(raw_slot.get("end"))
        if not isinstance(slot_id, str) or not slot_id.strip():
            return err(400, f"slots[{i}].id is required")
        if not isinstance(provider, str) or not provider.strip():
            return err(400, f"slots[{i}].provider is required")
        if start_dt is None:
            return err(400, f"slots[{i}].start must be an ISO-8601 date-time")
        if end_dt is None:
            return err(400, f"slots[{i}].end must be an ISO-8601 date-time")
        cleaned.append(
            {
                "id": slot_id.strip(),
                "provider": provider,
                "start": raw_slot["start"],
                "start_ms": ms(start_dt),
                "end": raw_slot["end"],
                "end_ms": ms(end_dt),
            }
        )

    ids = [s["id"] for s in cleaned]
    if len(set(ids)) != len(ids):
        return err(400, "slot ids must be unique")

    store.reset(cleaned)
    return {"status": "ok", "slots": len(cleaned)}


@app.get("/slots")
def list_slots(request: Request):
    """Level 1 - every slot, earliest start first, with availability."""
    if not authed(request):
        return err(401, "missing or invalid API key")
    provider = request.query_params.get("provider")
    return {"slots": [slot_json(r) for r in store.list_slots(provider)]}


@app.post("/bookings")
async def create_booking(request: Request):
    """Levels 1-5 - book a slot."""
    if not authed(request):
        return err(401, "missing or invalid API key")

    body, _ = await json_body(request)
    raw_body_for_hash = json.dumps(body, sort_keys=True, separators=(",", ":")) if body is not None else ""

    # The rate limiter keys on whatever email was supplied, even if it is invalid,
    # so failed attempts count too (level 5).
    email_field = body.get("email") if body is not None else None
    rate_key = email_field.strip().lower() if isinstance(email_field, str) and email_field.strip() else None

    idem_key = (request.headers.get("idempotency-key") or "").strip()
    body_hash = hashlib.sha256(raw_body_for_hash.encode()).hexdigest() if idem_key else ""

    if idem_key:
        existing = store.idem_get(idem_key)
        if existing is not None:
            if existing["body_hash"] != body_hash:
                return err(422, "Idempotency-Key was already used with a different request body")
            if existing["response"] is not None:
                return JSONResponse(json.loads(existing["response"]), status_code=existing["status_code"])
            # Another request with this key is still in flight: wait for its result.
            for _ in range(40):
                time.sleep(0.05)
                fresh = store.idem_get(idem_key)
                if fresh is not None and fresh["response"] is not None:
                    return JSONResponse(json.loads(fresh["response"]), status_code=fresh["status_code"])
            return err(409, "a request with this Idempotency-Key is still in progress")

    if rate_key is not None:
        allowed, retry_after = store.rate_take(rate_key, RATE_LIMIT, RATE_WINDOW_MS)
        if not allowed:
            return err(
                429,
                "too many booking attempts for this email, please try again shortly",
                headers={"Retry-After": str(retry_after)},
            )

    claimed = False
    if idem_key:
        if not store.idem_claim(idem_key, body_hash):
            # Lost the race to an identical concurrent request: return its answer.
            for _ in range(40):
                fresh = store.idem_get(idem_key)
                if fresh is not None and fresh["response"] is not None:
                    return JSONResponse(json.loads(fresh["response"]), status_code=fresh["status_code"])
                if fresh is not None and fresh["body_hash"] != body_hash:
                    return err(422, "Idempotency-Key was already used with a different request body")
                time.sleep(0.05)
            return err(409, "a request with this Idempotency-Key is still in progress")
        claimed = True

    def finish(status: int, payload: dict, headers: dict | None = None):
        if claimed:
            store.idem_finish(idem_key, status, json.dumps(payload))
        return JSONResponse(payload, status_code=status, headers=headers)

    # ---- level 2: validation -------------------------------------------------
    if body is None:
        return finish(400, {"error": "request body must be a JSON object"})

    slot_id = body.get("slot_id")
    name = body.get("name")
    email = body.get("email")

    if not isinstance(slot_id, str) or not slot_id.strip():
        return finish(400, {"error": "slot_id is required"})
    if not isinstance(name, str) or not name.strip():
        return finish(400, {"error": "name is required"})
    if not isinstance(email, str) or not email.strip():
        return finish(400, {"error": "email is required"})

    slot_id = slot_id.strip()
    name = name.strip()
    email = email.strip().lower()
    if not EMAIL_RE.match(email):
        return finish(400, {"error": "email is not a valid address"})

    slot = store.get_slot(slot_id)
    if slot is None:
        return finish(404, {"error": "slot not found"})
    if slot["start_ms"] <= now_ms():
        return finish(400, {"error": "slot has already started"})

    # ---- levels 1 & 3: insert, race-free ------------------------------------
    row = {
        "id": "bk_" + uuid.uuid4().hex[:16],
        "slot_id": slot_id,
        "name": name,
        "email": email,
        "created_at": iso_now(),
        "created_ms": now_ms(),
    }
    try:
        store.create_booking(row)
    except UniqueViolation:
        return finish(409, {"error": "slot is already booked"})

    stored = store.get_booking(row["id"]) or row
    return finish(201, booking_json(stored))


@app.get("/bookings")
def list_bookings(request: Request):
    """Level 4 - a person's bookings, oldest first, cursor paginated."""
    if not authed(request):
        return err(401, "missing or invalid API key")

    email = request.query_params.get("email")
    if email is None or not email.strip():
        return err(400, "email is required")
    email = email.strip().lower()

    limit_raw = request.query_params.get("limit")
    if limit_raw is None or limit_raw == "":
        limit = DEFAULT_LIMIT
    else:
        try:
            if "." in limit_raw or "e" in limit_raw.lower():
                raise ValueError
            limit = int(limit_raw)
        except ValueError:
            return err(400, "limit must be an integer between 1 and 50")
        if limit < 1 or limit > MAX_LIMIT:
            return err(400, "limit must be an integer between 1 and 50")

    cursor_raw = request.query_params.get("cursor")
    cursor = None
    if cursor_raw:
        cursor = decode_cursor(cursor_raw)
        if cursor is None:
            return err(400, "cursor is not valid")

    rows = store.list_bookings(email, limit, cursor)
    has_more = len(rows) > limit
    page = rows[:limit]
    next_cursor = encode_cursor(page[-1]) if has_more and page else None
    return {"bookings": [booking_json(r) for r in page], "next_cursor": next_cursor}


@app.get("/bookings/{booking_id}")
def get_booking(request: Request, booking_id: str):
    """Level 1 - fetch one booking."""
    if not authed(request):
        return err(401, "missing or invalid API key")
    row = store.get_booking(booking_id)
    if row is None:
        return err(404, "booking not found")
    return booking_json(row)


@app.delete("/bookings/{booking_id}")
def cancel_booking(request: Request, booking_id: str):
    """Level 3 - cancel a booking and free the slot."""
    if not authed(request):
        return err(401, "missing or invalid API key")
    result = store.cancel_booking(booking_id)
    if result == "missing":
        return err(404, "booking not found")
    if result == "already":
        return err(409, "booking is already cancelled")
    return booking_json(store.get_booking(booking_id))


# --------------------------------------------------------------------- errors
@app.exception_handler(404)
def not_found(request: Request, exc):  # noqa# ARG001
    return err(404, "not found")


@app.exception_handler(Exception)
def unhandled(request: Request, exc: Exception):  # noqa# ARG001
    return err(500, "internal error")
