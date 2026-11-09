# HookWatch

Webhook monitoring and management system with an MCP interface.

HookWatch receives webhook events, validates them, stores them and tracks their
processing lifecycle so that failed deliveries can be investigated and
recovered. The same application services will eventually be exposed to AI
agents through an MCP server, without duplicating business logic between the
HTTP API and the MCP layer.

The authoritative specification and staged roadmap are kept in a local
`hookwatch_implementation.md` document. It is intentionally not tracked in Git,
so it will not appear in a fresh clone of this repository.

## Status

Stage 6 (processing engine) is complete. The application starts, serves a
health check, validates webhook event envelopes, accepts them at
`POST /webhooks/{source}` and stores them in SQLite before responding, so
events survive a restart. A redelivered event is detected and acknowledged
without creating a second row, and every event carries a lifecycle status. An
in-process engine processes stored events, retries temporary failures with
exponential backoff, classifies permanent failures and dead-letters events that
will not be retried. HTTP inspection and replay do not exist yet. Stages are
implemented one at a time, and each stage is committed separately.

| Stage | Scope | Status |
| --- | --- | --- |
| 0 | Repository inspection and project initialization | Complete |
| 1 | Webhook data contract | Complete |
| 2 | Webhook simulator | Complete |
| 3 | Webhook receiver | Complete |
| 4 | Persistence | Complete |
| 5 | Event lifecycle and idempotency | Complete |
| 6 | Processing engine | Complete |
| 7 | Monitoring and event replay | Not started |
| 8 | MCP server | Not started |
| 9 | AI agent integration | Not started |
| 10 | Production hardening | Not started |

## Requirements

- Windows 11 (the supported development environment)
- [uv](https://docs.astral.sh/uv/) 0.13 or newer
- Python 3.13 or newer. `.python-version` pins the project to 3.14, and uv
  downloads that interpreter automatically if it is not already installed.

## Setup

```shell
uv sync --all-groups
```

`uv sync` creates `.venv`, installs the runtime and development dependencies and
records exact versions in `uv.lock`. Commit `uv.lock` so that every machine gets
the same dependency set.

## Run

```shell
uv run uvicorn app.main:app --reload
```

- Health check: http://127.0.0.1:8000/health
- Interactive API documentation: http://127.0.0.1:8000/docs

The first start creates `hookwatch.db` in the working directory. See Storage
below for its location, contents and how to inspect it.

## Receiving webhooks

`POST /webhooks/{source}` accepts one event. The `{source}` segment names the
sending service and lives in the URL, not the body, so an event from GitHub is
posted to `/webhooks/github`.

```shell
curl -i -X POST http://127.0.0.1:8000/webhooks/github \
  -H "Content-Type: application/json" \
  -d '{"event_id":"evt_1","event_type":"invoice.paid","occurred_at":"2026-11-07T12:00:00Z","data":{"amount":500}}'
```

On success a new event is stored and answered with `202 Accepted`:

```json
{"status": "accepted", "source": "github", "event_id": "evt_1"}
```

`202` rather than `200` is deliberate: the event has been received, validated and
stored, and the background engine will process it shortly. The response does not
wait for that work. Returning `200` would imply processing has finished.

If the same `source` and `event_id` were already stored, nothing new is written
and the delivery is answered with `200 OK`:

```json
{"status": "duplicate", "source": "github", "event_id": "evt_1"}
```

Both responses are successes. Senders retry after timeouts, so the same event
arriving twice is normal. `200` tells the sender that this exact event is
already safe, which is a reason to stop retrying. The original stored payload is
left untouched; a changed body with an already used event id does not overwrite
it.

Duplicate detection is keyed on `source` and `event_id` together, not on
`event_id` alone, because two providers may legitimately use the same identifier.
The `source` segment accepts 1 to 64 characters from letters, digits, dot, dash
and underscore.

| Status | Meaning |
| --- | --- |
| `202` | The event is new, valid, stored and queued for processing. |
| `200` | The event was already stored. Nothing new was written. |
| `404` | No route matched, for example `POST /webhooks` with no source segment. |
| `405` | The method is not allowed, for example `GET /webhooks/github`. |
| `422` | The body or the source segment failed validation. The response body lists every offending field. |

Accepted, duplicate and rejected requests are all logged:

```text
INFO:app.api.webhooks:webhook accepted source=github event_id=evt_1 event_type=invoice.paid row=1
INFO:app.api.webhooks:webhook duplicate source=github event_id=evt_1 row=1
WARNING:app.api.webhooks:request rejected path=/webhooks/github fields=[('body', 'event_id')]
```

Requests are not authenticated and carry no signature. Anything that matches the
schema is accepted, which is a known gap until the security work.

## Storage

Accepted events are written to a SQLite database before the response is sent,
so a `202` means "stored", not merely "received". The database file is created
on first startup.

- Default location: `hookwatch.db` in the working directory.
- Override with the `HOOKWATCH_DB_PATH` environment variable. It is read once at
  startup, so changing it requires a restart.
- The file is excluded from Git by the `*.db` rule.

The `events` table:

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | integer | Internal row key, assigned by the database. |
| `source` | text | The `{source}` path segment the event arrived on. |
| `event_id` | text | The identifier the sender supplied. Unique together with `source`. |
| `event_type` | text | Provider-defined event name. |
| `occurred_at` | text | When the sender says it happened, normalised to UTC. |
| `data` | text | The payload as JSON. |
| `received_at` | text | When HookWatch stored it, normalised to UTC. |
| `status` | text | Lifecycle status: `received`, `processing`, `processed` or `failed`. |
| `dead_lettered_at` | text | When the engine stopped retrying, or null while the event is still eligible. |

A unique index on `(source, event_id)` is what enforces idempotency. The insert
uses `ON CONFLICT (source, event_id) DO NOTHING` and then checks how many rows
changed, so the database makes the decision under its own lock. Checking with a
`SELECT` first would leave a window in which two concurrent deliveries both
believe they are first.

The `attempts` table records one row per processing attempt:

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | integer | Internal attempt key. |
| `event_row_id` | integer | The `events.id` this attempt belongs to. |
| `attempt_number` | integer | 1, 2, 3 and so on within one event. |
| `started_at` | text | When the attempt began, in UTC. |
| `finished_at` | text | When it ended, or null while it is open. |
| `outcome` | text | `succeeded`, `failed`, or null while it is open. |
| `failure_kind` | text | `retryable`, `permanent`, or null while it is open or after success. |
| `error` | text | The failure message, when there was one. |

Attempts exist so retries leave a history rather than overwriting a single
counter. The processing engine writes one row per try and uses the count to
apply the attempt limit.

Timestamps are stored as ISO 8601 text in UTC. SQLite has no datetime type, and
text in this fixed format sorts correctly. Offsets are converted to UTC first,
because mixing `+01:00` and `Z` values in text comparison produces the wrong
order.

### Lifecycle

An event moves through a small set of statuses:

```text
received --> processing --> processed
                   |
                   `--> failed --> processing (retry)
```

The rules live in `app/lifecycle.py` and are enforced by `EventService`, not by
the route. The allowed moves are:

| From | To |
| --- | --- |
| `received` | `processing` |
| `processing` | `processed` or `failed` |
| `failed` | `processing` (a retry) |
| `processed` | nothing, it is terminal |

Any other move raises `InvalidTransitionError`. Starting a second processing run
on a `processed` event, for example, is refused instead of silently reopening
finished work. Finishing an attempt that is missing or already finished raises
`AttemptNotOpenError`, so closing the same attempt twice is a detectable mistake
rather than a silent overwrite.

A failed attempt is classified as `retryable` or `permanent`:

- A retryable failure leaves the event `failed` and eligible for another try.
- A permanent failure, or a retryable failure that reaches the attempt limit,
  sets `dead_lettered_at`. The event stays `failed`, but the engine stops
  selecting it. The payload and every attempt remain in the database.

The HTTP API only creates `received` events. The processing engine moves them
through the rest of the lifecycle using the same `EventService` methods that the
API uses.

### Migrations

The schema is versioned with SQLite's `PRAGMA user_version`, which stores one
integer in the database file itself. `app/db.py` holds an ordered list of
migrations, and `migrate()` runs only the versions newer than the file reports.
Each version runs in its own transaction and bumps the version only after all of
its statements succeed, so a failure leaves the database at the last complete
version instead of half upgraded.

| Version | Contents |
| --- | --- |
| 1 | The original `events` table from Stage 4. |
| 2 | Adds `status`, deduplicates any existing rows, then adds the unique index on `(source, event_id)`. |
| 3 | Creates the `attempts` table. |
| 4 | Adds `attempts.failure_kind`, `events.dead_lettered_at` and an index on `events.status`. |

Version 2 deletes all but the oldest row of every duplicate `(source, event_id)`
pair before creating the unique index, because SQLite refuses to create a unique
index while duplicates exist. This only affects data written before idempotency
existed.

Inspect what was stored:

```shell
uv run python -m tools.inspect_events
uv run python -m tools.inspect_events --limit 5
uv run python -m tools.inspect_events --db C:\temp\other.db
```

It prints one JSON object per line, newest first:

```json
{"id": 2, "source": "github", "event_id": "evt_...", "event_type": "invoice.paid", "occurred_at": "2026-11-08T08:24:54+00:00", "received_at": "2026-11-08T08:24:54+00:00", "status": "processed", "dead_lettered_at": null, "data": {}}
```

If the `sqlite3` command line tool is installed, the same data is available
directly:

```shell
sqlite3 hookwatch.db "SELECT id, source, event_id, status, dead_lettered_at, received_at FROM events ORDER BY id DESC LIMIT 5;"
```

Known limits at this stage:

- The engine runs inside the application process. Stopping the application also
  stops processing; stored events resume when it starts again.
- There is one engine task per process. Running several uvicorn workers would
  start one each. Claiming is atomic, so events are not processed twice, but
  there is no leader election or shared scheduling yet.
- There is no HTTP inspection, dead-letter listing or replay. Use
  `tools/inspect_events.py` to see the stored status and timestamps.
- The tables grow forever. There is no retention, archival or cleanup.
- SQL access is confined to `app/repositories/`. Routes and services never see a
  connection or a table name, so replacing SQLite later means rewriting one
  class.

## Processing engine

The engine is one asyncio task started during the FastAPI lifespan. It asks the
service for a batch of due events, processes them, and sleeps only when nothing
was due. HTTP requests never wait for this work.

Why in-process: SQLite is local, the database is already the queue, and the
roadmap excludes Celery, Redis and brokers until a demonstrated need exists. A
task in the lifespan is the smallest architecture that keeps webhook responses
fast and survives a restart with pending work still in the database.

The default processor logs the event and succeeds. Stage 6 exercises the
scheduling, retry and dead-letter mechanics without a real third-party
integration. A real processor can be injected into `EventService` without
changing the engine.

Processing is at-least-once. If the process dies after the side effect but
before the attempt is closed, recovery will retry the event and the side effect
can happen again. A real processor should therefore be safe to repeat, or use
the event id as its own idempotency key.

### Claiming work

Before processing, the engine claims an event with a conditional update:

```text
UPDATE events SET status = 'processing'
WHERE id = ? AND status IN ('received', 'failed') AND dead_lettered_at IS NULL
```

A changed row count of 1 means this worker owns the event. 0 means another
worker won, or the event is not eligible, so the worker skips it. The status
change and the new `attempts` row happen in one transaction, so an event cannot
be processing with no attempt recorded.

The database decides the race under its own lock. Two workers cannot both claim
the same event.

### Retries and backoff

A retryable failure closes the attempt as `failed`, leaves `dead_lettered_at`
null, and lets a later batch try again. An event is due when the attempt count is
below the limit and enough time has passed since the last attempt:

```text
delay = min(backoff_base_seconds * 2^(attempts_so_far - 1), backoff_cap_seconds)
```

With the defaults that is 2, 4, 8, 16, 32 and then 60 seconds. The attempt count
and finish time come from the existing `attempts` table, so no extra retry
counter column is needed.

### Failure classification

The processor can signal what kind of failure happened:

| Signal | Meaning | Result |
| --- | --- | --- |
| `RetryableProcessingError` | Temporary, such as a timeout or 503 | Event stays `failed` and is retried with backoff |
| `PermanentProcessingError` | Cannot succeed, such as a malformed payload | Event is dead-lettered immediately |
| Any other exception | Unexpected bug | Logged with a traceback, treated as retryable, still bounded by the attempt limit |

Treating an unknown exception as retryable is deliberate. Dead-lettering on the
first unexpected error would discard an event that a later fix might process. The
attempt limit still stops an event that always crashes.

### Dead letters

An event is dead-lettered when a permanent failure occurs, or when the attempt
limit is reached. The attempt is closed, the error is stored, `dead_lettered_at`
is set, and the engine stops selecting the event. Nothing is deleted: the payload
and the full attempt history remain available for investigation.

A dead-lettered event keeps the `failed` status. `dead_lettered_at` is what
distinguishes "failed, will retry" from "failed, given up".

### Crash recovery

An open attempt is a lease. If the process dies after claiming an event, the
attempt keeps `finished_at` null and the event stays `processing` forever.

Before each batch the engine looks for open attempts whose `started_at` is older
than `stale_after_seconds`. It closes them as failed with an `abandoned` error
and moves their events to `failed`, so the normal retry path takes over. The
abandoned attempt counts toward the limit, because a crash is a failed attempt.

### Settings

Processing settings are code defaults in `app/services/events.py`, not
environment variables yet. Configuration for deployment belongs to a later
stage.

| Setting | Default | Meaning |
| --- | --- | --- |
| `poll_interval_seconds` | `1.0` | Sleep when a batch had no due work. |
| `batch_size` | `10` | Maximum events examined in one call. |
| `max_attempts` | `5` | Attempts before an event is dead-lettered. |
| `backoff_base_seconds` | `2.0` | Wait after the first failure. |
| `backoff_cap_seconds` | `60.0` | Largest wait between attempts. |
| `stale_after_seconds` | `300.0` | Age at which an open attempt is treated as abandoned. |

## Webhook simulator

`tools/simulate_webhook.py` is a development tool that posts events to a
running instance. It builds each event through the same `WebhookEvent` contract
the application uses, so it cannot send a malformed envelope, and it prints the
status code and response body so failures can be inspected.

```shell
uv run python -m tools.simulate_webhook
uv run python -m tools.simulate_webhook --event-type user.created --count 3
uv run python -m tools.simulate_webhook --data '{"amount": 500, "currency": "USD"}'
uv run python -m tools.simulate_webhook --event-id evt_fixed --count 2
uv run python -m tools.simulate_webhook --raw '{"event_id": "", "event_type": "x"}'
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--url` | `http://127.0.0.1:8000/webhooks/simulator` | Target URL. |
| `--event-type` | `simulator.ping` | Event type to send. |
| `--data` | `{}` | JSON object used as the event data. |
| `--raw` | none | Sends this JSON exactly as given, bypassing contract validation. |
| `--event-id` | generated per delivery | Fixed id reused for every delivery. |
| `--count` | `1` | Number of deliveries. |
| `--timeout` | `5.0` | Request timeout in seconds. |

Without `--raw`, the tool refuses to send anything the contract would reject,
so every simulated delivery is a valid one. `--raw` exists for the opposite
case: sending a broken payload on purpose to check that the receiver rejects it
with a meaningful response.

Exit codes: `0` when every delivery returned a 2xx response, `1` when a
delivery failed or could not be sent, `2` when the arguments or the generated
event were invalid.

Against a running HookWatch instance the default URL targets the receiver above,
so a new event returns `202` and the tool exits `0`. A redelivery returns `200`
with status `duplicate`, which is still a 2xx response and also exits `0`. Use
`--raw` to send a broken payload and watch the receiver reject it with `422`.

Sending the same `--event-id` more than once delivers the same event repeatedly.
That exercises idempotency: the first delivery is stored and answered with
`202`, every later one is answered with `200` and no second row is written.

```shell
uv run python -m tools.simulate_webhook --event-id evt_fixed --count 2
```

The response describes acceptance, not processing. A stored event is usually
picked up by the engine within a second, so `tools.inspect_events.py` may already
show it as `processed` or `failed` by the time you look.

The simulator relies on development dependencies, which `uv run` installs by
default.

## Tests

```shell
uv run pytest
```

The API test fixtures disable the background engine with
`create_app(..., start_engine=False)`, so assertions about the initial `received`
state are deterministic. Engine tests start it explicitly or drive the service
directly.

## Event data contract

Webhook events share one envelope, defined in `app/schemas/event.py` by the
`WebhookEvent` model:

```json
{
  "event_id": "evt_123",
  "event_type": "invoice.paid",
  "occurred_at": "2026-11-07T12:00:00Z",
  "data": {"amount": 500, "currency": "USD"}
}
```

| Field | Type | Rules |
| --- | --- | --- |
| `event_id` | string | Required. Not blank, at most 255 characters. Surrounding whitespace is stripped. Identifiers are provider-defined, so a UUID is not required. |
| `event_type` | string | Required. Not blank, at most 100 characters. Surrounding whitespace is stripped. Provider-defined name such as `invoice.paid`. |
| `occurred_at` | ISO 8601 timestamp | Required. Must carry a timezone, for example `Z` or `+01:00`. Naive timestamps are rejected because they are ambiguous. |
| `data` | JSON object | Required. Must be an object, and may be empty. Its contents are kept as-is and are not validated. |

Behaviour worth knowing:

- Unknown top-level fields are rejected. This catches misspelled field names
  early while the contract is small and under our control. If real third-party
  senders turn out to send extra fields, this can be relaxed in one line.
- Fields generated by HookWatch itself, such as the event source, the moment it
  was received, processing status and attempt counts, are not part of this
  contract. The sender does not supply them.
- Timestamp freshness is not enforced. Rejecting events dated far in the past
  or future only becomes meaningful once signatures are verified, so it is
  deferred to the security work and recorded here rather than implemented.
- No payload size limit is applied at this layer. That belongs to the HTTP
  receiver.

## Project layout

```text
.
|-- app/
|   |-- __init__.py
|   |-- api/
|   |   |-- __init__.py
|   |   |-- deps.py       Shared route dependencies
|   |   `-- webhooks.py   Webhook receiver endpoint and error logging
|   |-- db.py             Connections, migrations and row models
|   |-- engine.py         Background processing loop
|   |-- lifecycle.py      Event statuses, allowed transitions and failure kinds
|   |-- main.py           App factory, router, engine startup and logging
|   |-- repositories/
|   |   |-- __init__.py
|   |   `-- events.py     EventRepository, the only place with event SQL
|   |-- schemas/
|   |   |-- __init__.py
|   |   `-- event.py      WebhookEvent data contract
|   `-- services/
|       |-- __init__.py
|       `-- events.py     EventService and processing policy
|-- tests/
|   |-- conftest.py            Shared app and client fixtures on a temp database
|   |-- test_health.py         Health endpoint test
|   |-- test_event_schema.py   Event contract validation tests
|   |-- test_simulator.py      Webhook simulator tests
|   |-- test_webhooks.py       Receiver API tests
|   |-- test_events_repository.py  Repository tests
|   |-- test_lifecycle.py      EventService lifecycle tests
|   |-- test_migrations.py     Schema versioning tests
|   |-- test_idempotency.py    API level duplicate delivery tests
|   |-- test_processing.py     Retry, backoff, classification and dead-letter tests
|   |-- test_engine.py         Background loop and FastAPI integration tests
|   |-- test_persistence.py    API to database tests and restart behaviour
|   `-- test_inspect_events.py Inspector tool tests
|-- tools/
|   |-- simulate_webhook.py    Development tool that sends simulated events
|   `-- inspect_events.py      Development tool that prints stored events
|-- pyproject.toml        Project metadata, dependencies, pytest configuration
|-- uv.lock               Reproducible dependency lock file
|-- .python-version       Python version selected by uv
`-- hookwatch_implementation.md   Local specification, not tracked by Git
```

The package layout is intentionally minimal. The business rules live in
`app/services/` so the HTTP API and the future MCP server can call the same
methods. Directories for MCP will be added when a stage needs them, not in
advance.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOOKWATCH_DB_PATH` | `hookwatch.db` | SQLite database file. Read once at startup. |

Processing intervals, attempt limits and backoff values are code defaults in
`ProcessingSettings`. They are not environment variables yet, because deployment
configuration belongs to the production hardening stage.

Nothing else is configurable yet. There are no secrets and no authentication.

## Not implemented yet

These are planned but deliberately absent, so nothing here should be assumed to
exist:

- Event inspection over HTTP and dead-letter listing. Use
  `tools/inspect_events.py` until then.
- Event replay
- Signature verification, rate limiting or authentication
- Payload size limits at the HTTP layer
- Retention, archival or cleanup of stored events
- Operational metrics or dashboards
- MCP tools and resources
