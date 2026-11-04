"""End-to-end checks for levels 1-5 against a running server.

Usage: python tests/test_levels.py [base_url] [api_key]
"""

import os
import sys
import threading
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
KEY = sys.argv[2] if len(sys.argv) > 2 else os.getenv("API_KEY", "clinic-demo-key-2026")
H = {"X-API-Key": KEY}

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  <- {detail}" if not cond and detail else ""))


def slots_payload():
    return {
        "slots": [
            {"id": "s1", "provider": "Dr Ade", "start": "2099-12-10T09:00:00Z", "end": "2099-12-10T09:30:00Z"},
            {"id": "s2", "provider": "Dr Ade", "start": "2099-12-10T10:00:00Z", "end": "2099-12-10T10:30:00Z"},
            {"id": "s3", "provider": "Dr Bello", "start": "2099-12-11T09:00:00Z", "end": "2099-12-11T09:30:00Z"},
            {"id": "s4", "provider": "Dr Bello", "start": "2099-12-09T08:00:00Z", "end": "2099-12-09T08:30:00Z"},
            {"id": "s5", "provider": "Dr Chi", "start": "2099-12-12T11:00:00Z", "end": "2099-12-12T11:30:00Z"},
            {"id": "s6", "provider": "Dr Chi", "start": "2099-12-12T12:00:00Z", "end": "2099-12-12T12:30:00Z"},
            {"id": "past1", "provider": "Dr Ade", "start": "2020-01-01T09:00:00Z", "end": "2020-01-01T09:30:00Z"},
        ]
    }


def reset(c):
    r = c.post("/reset", json=slots_payload(), headers=H)
    assert r.status_code == 200, r.text


def book(c, slot, name="Ada Obi", email="ada@example.com", headers=None):
    h = dict(H)
    if headers:
        h.update(headers)
    return c.post("/bookings", json={"slot_id": slot, "name": name, "email": email}, headers=h)


def main():
    c = httpx.Client(base_url=BASE, timeout=30)

    # ------------------------------------------------------------------ level 1
    print("\n== Level 1: slots, booking, lookup ==")
    reset(c)

    r = c.get("/slots", headers=H)
    body = r.json()
    check("GET /slots -> 200", r.status_code == 200, r.text)
    ids = [s["id"] for s in body["slots"]]
    check("slots sorted earliest first", ids == ["past1", "s4", "s1", "s2", "s3", "s5", "s6"], ids)
    check("slot has id/provider/start/end/available",
          all(k in body["slots"][0] for k in ("id", "provider", "start", "end", "available")))
    check("all slots available after reset", all(s["available"] for s in body["slots"]))

    r = c.get("/slots", params={"provider": "Dr Ade"}, headers=H)
    check("provider filter (exact)", [s["id"] for s in r.json()["slots"]] == ["past1", "s1", "s2"], r.text)
    r = c.get("/slots", params={"provider": "dr ade"}, headers=H)
    check("provider filter is exact, not fuzzy", r.json()["slots"] == [], r.text)

    r = book(c, "s1")
    check("POST /bookings -> 201", r.status_code == 201, r.text)
    bk = r.json()
    check("booking payload complete",
          set(bk) == {"id", "slot_id", "name", "email", "status", "created_at"}, bk)
    check("booking status confirmed", bk["status"] == "confirmed")
    check("booking id is a string", isinstance(bk["id"], str) and len(bk["id"]) > 0)

    r = c.get(f"/bookings/{bk['id']}", headers=H)
    check("GET /bookings/{id} -> 200", r.status_code == 200 and r.json()["id"] == bk["id"], r.text)
    r = c.get("/bookings/does-not-exist", headers=H)
    check("GET unknown booking -> 404 {error}", r.status_code == 404 and "error" in r.json(), r.text)

    r = c.get("/slots", headers=H)
    taken = [s for s in r.json()["slots"] if s["id"] == "s1"][0]
    check("booked slot shows available:false", taken["available"] is False, taken)

    # ------------------------------------------------------------------ level 2
    print("\n== Level 2: API key + validation ==")
    reset(c)
    r = c.get("/slots")
    check("no key -> 401", r.status_code == 401 and "error" in r.json(), r.text)
    r = c.get("/slots", headers={"X-API-Key": "wrong"})
    check("wrong key -> 401", r.status_code == 401, r.text)
    r = c.post("/bookings", json={"slot_id": "s1", "name": "A", "email": "a@b.co"})
    check("no key on POST -> 401", r.status_code == 401, r.text)

    for label, payload in [
        ("missing slot_id", {"name": "A", "email": "a@example.com"}),
        ("missing name", {"slot_id": "s1", "email": "a@example.com"}),
        ("missing email", {"slot_id": "s1", "name": "A"}),
        ("empty name", {"slot_id": "s1", "name": "   ", "email": "a@example.com"}),
        ("empty slot_id", {"slot_id": "", "name": "A", "email": "a@example.com"}),
        ("bad email", {"slot_id": "s1", "name": "A", "email": "not-an-email"}),
        ("bad email 2", {"slot_id": "s1", "name": "A", "email": "a@b"}),
    ]:
        r = c.post("/bookings", json=payload, headers=H)
        check(f"{label} -> 400", r.status_code == 400 and "error" in r.json(), r.text)

    r = book(c, "nope", email="ghost@example.com")
    check("unknown slot -> 404", r.status_code == 404 and "error" in r.json(), r.text)
    r = book(c, "past1", email="past@example.com")
    check("past slot -> 400", r.status_code == 400, r.text)

    r = c.post("/bookings", json={"slot_id": "s2", "name": "  Ada Obi  ", "email": "  ADA@Example.COM "}, headers=H)
    check("trims + lowercases -> 201", r.status_code == 201, r.text)
    d = r.json()
    check("name trimmed", d["name"] == "Ada Obi", d)
    check("email trimmed & lowercased", d["email"] == "ada@example.com", d)

    # ------------------------------------------------------------------ level 3
    print("\n== Level 3: no double booking, cancellation ==")
    reset(c)
    r1 = book(c, "s1", email="one@example.com")
    r2 = book(c, "s1", email="two@example.com")
    check("first booking 201", r1.status_code == 201, r1.text)
    check("second booking 409", r2.status_code == 409 and "error" in r2.json(), r2.text)

    # race: 30 simultaneous attempts on one slot
    reset(c)
    results = []
    lock = threading.Lock()
    barrier = threading.Barrier(30)

    def worker(i):
        with httpx.Client(base_url=BASE, timeout=30) as cc:
            barrier.wait()
            rr = cc.post("/bookings",
                         json={"slot_id": "s1", "name": f"P{i}", "email": f"race{i}@example.com"},
                         headers=H)
            with lock:
                results.append(rr.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    check("race: exactly one 201", results.count(201) == 1, sorted(results))
    check("race: all others 409", results.count(409) == 29, sorted(results))
    # cancel
    booked_email = None
    for i in range(30):
        cand = c.get("/bookings", params={"email": f"race{i}@example.com"}, headers=H).json()["bookings"]
        if cand:
            booked_email = f"race{i}@example.com"
            bk_id = cand[0]["id"]
            break
    check("found the winning booking", booked_email is not None)

    r = c.delete(f"/bookings/{bk_id}", headers=H)
    check("DELETE -> 200 cancelled", r.status_code == 200 and r.json()["status"] == "cancelled", r.text)
    r = c.delete(f"/bookings/{bk_id}", headers=H)
    check("DELETE again -> 409", r.status_code == 409, r.text)
    r = c.delete("/bookings/nope", headers=H)
    check("DELETE unknown -> 404", r.status_code == 404, r.text)

    r = c.get("/slots", headers=H)
    check("cancelled slot is available again",
          [s for s in r.json()["slots"] if s["id"] == "s1"][0]["available"] is True)
    r = book(c, "s1", email="newperson@example.com")
    check("cancelled slot can be rebooked", r.status_code == 201, r.text)

    # ------------------------------------------------------------------ level 4
    print("\n== Level 4: idempotency + pagination ==")
    reset(c)
    key = {"Idempotency-Key": "idem-abc-123"}
    a = book(c, "s1", email="idem@example.com", headers=key)
    b = book(c, "s1", email="idem@example.com", headers=key)
    check("first idempotent call 201", a.status_code == 201, a.text)
    check("retry same key/body -> same status", b.status_code == a.status_code, (a.status_code, b.status_code))
    check("retry same key/body -> same booking id", b.json()["id"] == a.json()["id"], (a.text, b.text))
    lst = c.get("/bookings", params={"email": "idem@example.com"}, headers=H).json()["bookings"]
    check("retry created no duplicate", len(lst) == 1, lst)

    r = book(c, "s2", email="idem@example.com", headers=key)
    check("same key different body -> 422", r.status_code == 422 and "error" in r.json(), r.text)

    # concurrent identical retries of one booking
    reset(c)
    conc_keys = {"Idempotency-Key": "idem-race-1"}
    statuses, ids = [], []
    barrier2 = threading.Barrier(10)

    def worker2():
        with httpx.Client(base_url=BASE, timeout=30) as cc:
            barrier2.wait()
            rr = cc.post("/bookings", json={"slot_id": "s3", "name": "Race Retry", "email": "retry@example.com"},
                         headers={**H, **conc_keys})
            with lock:
                statuses.append(rr.status_code)
                try:
                    ids.append(rr.json().get("id"))
                except Exception:
                    ids.append(None)

    ts = [threading.Thread(target=worker2) for _ in range(10)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    check("concurrent identical retries all 201", statuses.count(201) == 10, statuses)
    check("concurrent identical retries share one booking id", len(set(ids)) == 1, ids)

    # pagination
    reset(c)
    pemail = "pager@example.com"
    made = []
    for slot in ["s1", "s2", "s3", "s4", "s5"]:
        rr = book(c, slot, email=pemail)
        if rr.status_code == 201:
            made.append(rr.json()["id"])
    check("made 5 bookings for pager", len(made) == 5, made)

    seen, cursor, pages = [], None, 0
    while True:
        params = {"email": "PAGER@Example.com", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        rr = c.get("/bookings", params=params, headers=H)
        if rr.status_code != 200:
            check("pagination page -> 200", False, rr.text)
            break
        page = rr.json()
        pages += 1
        seen += [b["id"] for b in page["bookings"]]
        check(f"page {pages} respects limit", len(page["bookings"]) <= 2, page)
        cursor = page["next_cursor"]
        if not cursor:
            break
        if pages > 10:
            break
    check("pagination walked all bookings", seen == made, (seen, made))
    check("email match ignores case", set(seen) == set(made))
    check("last page next_cursor is null", cursor is None, cursor)
    check("booking order oldest first", seen == made, seen)

    for bad in ["0", "51", "-3", "abc", "2.5"]:
        rr = c.get("/bookings", params={"email": pemail, "limit": bad}, headers=H)
        check(f"limit={bad} -> 400", rr.status_code == 400, rr.text)
    rr = c.get("/bookings", params={"email": pemail, "limit": 50}, headers=H)
    check("limit=50 accepted", rr.status_code == 200, rr.text)
    rr = c.get("/bookings", headers=H)
    check("missing email -> 400", rr.status_code == 400, rr.text)

    # ------------------------------------------------------------------ level 5
    print("\n== Level 5: rate limiting ==")
    reset(c)
    spam = "spam@example.com"
    codes = []
    for i in range(6):
        rr = book(c, ["s1", "s2", "s3", "s4", "s5", "s6"][i], email=spam)
        codes.append(rr.status_code)
        if rr.status_code == 429:
            retry = rr.headers.get("Retry-After")
            check("429 has integer Retry-After", retry is not None and retry.isdigit() and int(retry) >= 1, retry)
            check("429 body is {error}", "error" in rr.json(), rr.text)
    check("first five attempts allowed", codes[:5] == [201] * 5, codes)
    check("6th attempt -> 429", codes[5] == 429, codes)

    # other emails unaffected
    rr = book(c, "s6", email="other@example.com")
    check("other email unaffected by limit", rr.status_code in (201, 409), rr.text)

    # failed attempts count toward the limit
    reset(c)
    failer = "fail@example.com"
    codes = []
    for _ in range(5):
        rr = book(c, "missing-slot", email=failer)
        codes.append(rr.status_code)
    rr = book(c, "s1", email=failer)
    check("5 failed attempts -> 404 each", codes == [404] * 5, codes)
    check("failed attempts count: 6th -> 429", rr.status_code == 429, (rr.status_code, rr.text))

    # case-insensitive rate key
    reset(c)
    mixed = "Mixed@Example.com"
    for i in range(5):
        book(c, ["s1", "s2", "s3", "s4", "s5"][i], email=mixed.upper() if i % 2 else mixed)
    rr = book(c, "s6", email=mixed.lower())
    check("rate limit keyed case-insensitively", rr.status_code == 429, rr.text)

    # ------------------------------------------------------------------ report
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        print("Failed:", FAIL)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
