# Clinic booking API

A small appointment-booking API for a clinic, built to the contract published at
<https://koyaacademy.com/assessment-api>, plus a one-page web UI that uses it.

* **Live API:** _(see the deployment section / link in the submission message)_
* **Web page:** the same URL — `GET /` serves the booking UI.
* **Interactive docs:** `/docs`
* **API key:** `clinic-demo-key-2026` (send it as the `X-API-Key` header; override with the `API_KEY` env var).

Stack: Python 3.12 · FastAPI · SQLite (default) or PostgreSQL (`DATABASE_URL`).
No ORM — the guarantees below come from the database itself.

## Endpoints

| Method | Path | Level | Notes |
| --- | --- | --- | --- |
| `POST` | `/reset` | 1 | Deletes every booking and replaces all slots with the ones sent (past slots included). |
| `GET` | `/slots` | 1 | Every slot, earliest `start` first, each with `available`. Optional `?provider=` exact match. |
| `POST` | `/bookings` | 1–5 | Creates a `confirmed` booking → `201`. |
| `GET` | `/bookings` | 4 | A person's bookings, oldest first, cursor paginated. |
| `GET` | `/bookings/{id}` | 1 | One booking. |
| `DELETE` | `/bookings/{id}` | 3 | Cancels a booking and frees its slot. |

Every request needs `X-API-Key`; a missing or wrong key is `401`.
Every error body is `{"error": "<message>"}`.

## How each level is guaranteed

### Level 1 — CRUD
Slots and bookings live in a real database; the `/reset` endpoint replaces the
slot catalogue atomically. `available` on `GET /slots` is computed from confirmed
bookings, so it flips back to `true` as soon as a booking is cancelled.

### Level 2 — API key + validation
`X-API-Key` is compared with `hmac.compare_digest` (constant time) on every
endpoint, including `/reset`. `slot_id`, `name` and `email` must be present and
non-empty; `email` must match a real address pattern. `name`/`email` are trimmed
and `email` is stored lower-cased. Unknown slot → `404`, already-started slot → `400`.

### Level 3 — one booking per slot, cancellable
Uniqueness is enforced by the database, not by a check-then-write in Python:

```sql
CREATE UNIQUE INDEX ux_confirmed_slot ON bookings (slot_id) WHERE status = 'confirmed';
```

A losing request hits an `IntegrityError` and is turned into `409`. Because the
constraint lives in the index, this holds across threads, across worker
processes and across hosts sharing one database. Cancelling sets
`status = 'cancelled'`, which drops the row out of the partial index and frees
the slot for re-booking.

Verified with 60 simultaneous requests (2 worker processes): exactly one `201`,
59 × `409`, one stored booking.

### Level 4 — idempotency + pagination
The `Idempotency-Key` header is stored with a SHA-256 of the canonical JSON body
and the exact response that was produced:

* same key + same body → the stored response is replayed verbatim (same status,
  same booking `id`), and no second booking is created;
* same key + different body → `422`;
* two *concurrent* requests with the same key → one claims the key with an
  `INSERT` guarded by the primary key, the other waits for and replays its result.

Pagination is a keyset cursor over `(created_ms, seq)` — stable even when many
bookings share a timestamp — returned as `next_cursor` (`null` on the last page)
and replayed by the client as `?cursor=`. `limit` outside 1–50, or non-integer,
is a `400`.

### Level 5 — rate limiting
At most 5 booking attempts per email per minute, *failed attempts included*. The
attempt is recorded in one atomic statement:

```sql
INSERT INTO rate_attempts (email, at_ms)
SELECT ?, ? WHERE (SELECT COUNT(*) FROM rate_attempts WHERE email = ? AND at_ms > ?) < 5
```

If nothing is inserted, the request is the 6th and gets `429` with a whole-second
`Retry-After` computed from when the oldest attempt in the window expires. The
key is the lower-cased email, so casing cannot be used to dodge the limit, and
other emails are unaffected. `/reset` clears the window.

## Run it locally

```bash
pip install -r requirements.txt
API_KEY=clinic-demo-key-2026 uvicorn app:app --host 0.0.0.0 --port 8000
```

Open <http://localhost:8000>. Data is written to `data/clinic.db`.

## Test it

With the server running:

```bash
python tests/test_levels.py http://127.0.0.1:8000 clinic-demo-key-2026
```

68 checks across levels 1–5, including a 30-thread race on a single slot,
concurrent duplicate `Idempotency-Key` retries, cursor pagination walk, and the
6th-attempt `429`.

## Deploy it

The app is a single stateless web service; it only needs a database URL and an
API key. `render.yaml` describes a free Render web service plus a free Render
Postgres, and `DATABASE_URL` switches the storage layer to Postgres automatically.

Any host works — the start command is always:

```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

Environment variables:

| Variable | Purpose |
| --- | --- |
| `API_KEY` | The key required in `X-API-Key`. |
| `DATABASE_URL` | Postgres connection string. Omit to use SQLite. |
| `SQLITE_PATH` | Where the SQLite file lives (default `./data/clinic.db`). Use a mounted disk if the host's filesystem is ephemeral. |
| `SEED_DEMO_SLOTS` | `0` to start with no appointments instead of the demo fortnight. |
