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

Stage 4 (persistence) is complete. The application starts, serves a health
check, validates webhook event envelopes, accepts them at
`POST /webhooks/{source}` and stores them in SQLite before responding, so
events survive a restart. Processing, duplicate detection and inspection over
HTTP do not exist yet. Stages are implemented one at a time, and each stage is
committed separately.

| Stage | Scope | Status |
| --- | --- | --- |
| 0 | Repository inspection and project initialization | Complete |
| 1 | Webhook data contract | Complete |
| 2 | Webhook simulator | Complete |
| 3 | Webhook receiver | Complete |
| 4 | Persistence | Complete |
| 5 | Event lifecycle and idempotency | Not started |
| 6 | Processing engine | Not started |
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

On success it answers `202 Accepted`:

```json
{"status": "accepted", "source": "github", "event_id": "evt_1"}
```

`202` rather than `200` is deliberate: the event has been received, validated and
stored, but it is not processed yet. Returning `200` would imply the work is
finished.

The `source` segment accepts 1 to 64 characters from letters, digits, dot, dash
and underscore.

| Status | Meaning |
| --- | --- |
| `202` | The event is valid, stored, and accepted. Processing does not exist yet. |
| `404` | No route matched, for example `POST /webhooks` with no source segment. |
| `405` | The method is not allowed, for example `GET /webhooks/github`. |
| `422` | The body or the source segment failed validation. The response body lists every offending field. |

Accepted and rejected requests are both logged:

```text
INFO:app.api.webhooks:webhook accepted source=github event_id=evt_1 event_type=invoice.paid
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
| `event_id` | text | The identifier the sender supplied. Not unique yet. |
| `event_type` | text | Provider-defined event name. |
| `occurred_at` | text | When the sender says it happened, normalised to UTC. |
| `data` | text | The payload as JSON. |
| `received_at` | text | When HookWatch stored it, normalised to UTC. |

Timestamps are stored as ISO 8601 text in UTC. SQLite has no datetime type, and
text in this fixed format sorts correctly. Offsets are converted to UTC first,
because mixing `+01:00` and `Z` values in text comparison produces the wrong
order.

Inspect what was stored:

```shell
uv run python -m tools.inspect_events
uv run python -m tools.inspect_events --limit 5
uv run python -m tools.inspect_events --db C:\temp\other.db
```

It prints one JSON object per line, newest first:

```json
{"id": 2, "source": "github", "event_id": "evt_...", "event_type": "invoice.paid", "occurred_at": "2026-11-08T08:24:54+00:00", "received_at": "2026-11-08T08:24:54+00:00", "data": {}}
```

If the `sqlite3` command line tool is installed, the same data is available
directly:

```shell
sqlite3 hookwatch.db "SELECT id, source, event_type, received_at FROM events ORDER BY id DESC LIMIT 5;"
```

Known limits at this stage:

- The schema is created with `CREATE TABLE IF NOT EXISTS` at startup. There is
  no migration tool. That works for creating a table, but cannot alter one, so
  the first schema change will need its own decision.
- There is no unique constraint on `source` and `event_id`. A redelivered event
  is stored again as a new row. Duplicate detection is the next stage.
- The table grows forever. There is no retention, archival or cleanup.
- SQL access is confined to `app/repositories/`. Routes never see SQL, so
  replacing SQLite later means rewriting one class.

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
so a valid delivery returns `202` and the tool exits `0`. Use `--raw` to send a
broken payload and watch the receiver reject it with `422`.

Sending the same `--event-id` more than once delivers the same event repeatedly,
which is how duplicate deliveries will be exercised once duplicate detection
exists.

The simulator relies on development dependencies, which `uv run` installs by
default.

## Tests

```shell
uv run pytest
```

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
|   |-- db.py             SQLite connection, schema and stored event model
|   |-- main.py           App factory, router and logging setup
|   |-- repositories/
|   |   |-- __init__.py
|   |   `-- events.py     EventRepository, the only place with event SQL
|   `-- schemas/
|       |-- __init__.py
|       `-- event.py      WebhookEvent data contract
|-- tests/
|   |-- conftest.py            Shared app and client fixtures on a temp database
|   |-- test_health.py         Health endpoint test
|   |-- test_event_schema.py   Event contract validation tests
|   |-- test_simulator.py      Webhook simulator tests
|   |-- test_webhooks.py       Receiver API tests
|   |-- test_events_repository.py  Repository tests
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

The package layout is intentionally minimal. Directories for services,
persistence and MCP will be added when a stage needs them, not in advance.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `HOOKWATCH_DB_PATH` | `hookwatch.db` | SQLite database file. Read once at startup. |

Nothing else is configurable yet. There are no secrets and no authentication.

## Not implemented yet

These are planned but deliberately absent, so nothing here should be assumed to
exist:

- Duplicate detection and idempotent processing. Redeliveries are stored again.
- Automatic database schema migrations. The schema is created once with
  `CREATE TABLE IF NOT EXISTS`.
- Background processing, retries or dead-letter handling
- Event inspection over HTTP. Use `tools/inspect_events.py` until then.
- Signature verification, rate limiting or authentication
- Payload size limits at the HTTP layer
- Retention, archival or cleanup of stored events
- MCP tools and resources
