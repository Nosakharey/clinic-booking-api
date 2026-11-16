# Clinic Booking API

An API that lets patients book clinic appointment slots, plus a web page that uses it.
Built for Task 1 of the Koya Cohort 4 assessment, following the spec at
[koyaacademy.com/assessment-api](https://koyaacademy.com/assessment-api).

- **API:** Python + FastAPI (`main.py`)
- **Database:** Postgres (Neon)
- **Web page:** `static/index.html`, served at `/` by the same app
- **Hosting:** Render

## Endpoints

Every request needs the `X-API-Key` header. Every error looks like `{"error": "..."}`.

| Method | Path | What it does |
|---|---|---|
| POST | `/reset` | Deletes all bookings and replaces the slots |
| GET | `/slots?provider=` | Lists slots, earliest first, with `available` |
| POST | `/bookings` | Books a slot (optional `Idempotency-Key` header) |
| GET | `/bookings?email=&limit=&cursor=` | A person's bookings, oldest first, paged |
| GET | `/bookings/{id}` | One booking |
| DELETE | `/bookings/{id}` | Cancels a booking and frees the slot |

## How each level is handled

1. **Basics.** Slots and bookings live in Postgres, so they survive restarts.
2. **API key and validation.** A middleware rejects any request without the right `X-API-Key` (401).
   Bookings check that `slot_id`, `name` and `email` are present, non-empty strings, and that the
   email looks valid (400). Unknown slots give 404 and slots that have started give 400.
   Name and email are trimmed and the email is stored in lower case.
3. **No double booking.** A partial unique index allows only one *confirmed* booking per slot.
   The database enforces it, so even simultaneous requests can't both succeed: the loser gets 409.
   Cancelling only changes `confirmed` to `cancelled`, so a second cancel gets 409.
4. **Safe retries and paging.** The first request with an `Idempotency-Key` claims the key in the
   database. A retry waits for that result and returns the same status and booking. Reusing the
   key with a different body returns 422. `GET /bookings` uses cursor pagination over an
   ever-increasing sequence number, so pages never skip or repeat bookings.
5. **No spam.** Each booking attempt is recorded per email. The sixth attempt within 60 seconds
   returns 429 with `Retry-After` in seconds. Attempts for the same email are serialised with a
   Postgres advisory lock, so a burst can't slip past the limit.

## Run it locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
API_KEY=dev-key uvicorn main:app --reload
```
Without `DATABASE_URL` it uses a local SQLite file. Open http://127.0.0.1:8000.

## Tests

`tests/check_levels.py` checks every level, including concurrent bookings and retries.

```bash
pip install httpx
API_KEY=dev-key python tests/check_levels.py http://127.0.0.1:8000
```
It calls `POST /reset`, which wipes the data.
