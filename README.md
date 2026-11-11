# HookWatch

Webhook monitoring and management system with an MCP interface.

HookWatch receives webhook events, validates them, stores them and tracks their
processing lifecycle so that failed deliveries can be investigated and
recovered. The same application services are exposed to AI agents through an
MCP server that stays read-only unless a replay token is configured, without
duplicating business logic between the HTTP API and the MCP layer.

The authoritative specification and staged roadmap are kept in a local
`hookwatch_implementation.md` document. It is intentionally not tracked in Git,
so it will not appear in a fresh clone of this repository.

## Status

Stage 9 (AI agent integration) is complete. The application starts, serves a
health check, validates webhook event envelopes, accepts them at
`POST /webhooks/{source}` and stores them in SQLite before responding, so events
survive a restart. A
redelivered event is detected and acknowledged without creating a second row,
and every event carries a lifecycle status. An in-process engine processes
stored events, retries temporary failures with exponential backoff, classifies
permanent failures and dead-letters events that will not be retried. Events,
attempts and replay history can be inspected over HTTP with filters, and failed
or processed events can be queued for another processing cycle by an operator
holding the replay token. A separate MCP server exposes the same listings,
detail and processing history over stdio, and can also queue an event for
replay when its process holds the replay token. Replay is annotated as
destructive, so clients such as Hermes require approval before running it.
Stages are implemented one at a time, and each stage is committed separately.
Stage 10 (production hardening) is in progress: the web process loads and
validates its configuration from the environment before it starts, and webhook
deliveries are authenticated per source with HMAC-SHA256 signatures. A process
that requires signatures without configuring a secret refuses to start.

| Stage | Scope | Status |
| --- | --- | --- |
| 0 | Repository inspection and project initialization | Complete |
| 1 | Webhook data contract | Complete |
| 2 | Webhook simulator | Complete |
| 3 | Webhook receiver | Complete |
| 4 | Persistence | Complete |
| 5 | Event lifecycle and idempotency | Complete |
| 6 | Processing engine | Complete |
| 7 | Monitoring and event replay | Complete |
| 8 | MCP server | Complete |
| 9 | AI agent integration | Complete |
| 10 | Production hardening | In progress (configuration, sender signatures) |

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
- Event monitoring: http://127.0.0.1:8000/events
- Interactive API documentation: http://127.0.0.1:8000/docs

The first start creates `hookwatch.db` in the working directory. See Storage
below for its location, contents and how to inspect it.

## Receiving webhooks

`POST /webhooks/{source}` accepts one event. The `{source}` segment names the
sending service and lives in the URL, not the body, so an event from GitHub is
posted to `/webhooks/github`.

With signature enforcement disabled (it is on by default, see
[Signatures](#signatures)):

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
| `401` | The signature is missing, malformed, stale or does not match the source secret. Nothing was stored. |
| `404` | No route matched, for example `POST /webhooks` with no source segment. |
| `405` | The method is not allowed, for example `GET /webhooks/github`. |
| `422` | The body or the source segment failed validation. The response body lists every offending field. |

Accepted, duplicate and rejected requests are all logged:

```text
INFO:app.api.webhooks:webhook accepted source=github event_id=evt_1 event_type=invoice.paid row=1
INFO:app.api.webhooks:webhook duplicate source=github event_id=evt_1 row=1
WARNING:app.api.webhooks:request rejected path=/webhooks/github fields=[('body', 'event_id')]
```

### Signatures

Sender authenticity is verified with an HMAC-SHA256 signature. It is required by
default. A delivery to a source with no configured secret is refused, because
the source name comes from the URL: accepting an unlisted one would let anyone
bypass the check by inventing a new name.

The sender and HookWatch share one secret per source. The sender computes:

```text
X-HookWatch-Signature: t=<unix seconds>,v1=<hex digest>
v1 = HMAC-SHA256(secret, f"{t}.".encode() + raw_body)
```

The timestamp is part of the signed message, so it cannot be moved without the
secret. HookWatch refuses a timestamp further than
`HOOKWATCH_SIGNATURE_TOLERANCE_SECONDS` (default 300) from its own clock, in
either direction. The digest covers the raw request bytes, not re-serialised
JSON: key order and whitespace are part of what the sender signed, and rewriting
the body would invalidate a correct signature.

Configure one secret per source:

```shell
HOOKWATCH_WEBHOOK_SECRETS="github=replace-with-a-long-random-value,stripe=another-value"
```

Pairs are comma separated and split at the first `=`, so a secret may contain an
equals sign. Generated secrets are URL-safe. The settings object hides the map
from its `repr`, so logging the configuration does not print the secrets.

The simulator signs automatically when the map is set, or with `--secret`:

```shell
uv run python -m tools.simulate_webhook --secret replace-with-a-long-random-value
```

To sign a manual request, compute the header over the exact bytes you send:

```python
import hashlib
import hmac
import time

body = b'{"event_id":"evt_1","event_type":"invoice.paid","occurred_at":"2026-11-07T12:00:00Z","data":{"amount":500}}'
timestamp = int(time.time())
digest = hmac.new(
    b"replace-with-a-long-random-value",
    f"{timestamp}.".encode() + body,
    hashlib.sha256,
).hexdigest()
print(f"t={timestamp},v1={digest}")
```

Then send those bytes with `--data-binary` and
`-H "X-HookWatch-Signature: t=...,v1=..."`.

A missing, malformed, stale, future or mismatching signature is answered with
`401` and nothing is stored. Every failure returns the same response, so a caller
cannot probe which sources exist or why the check failed; the log keeps the
specific reason.

Signatures prove the body came from someone holding the shared secret and was
not altered in transit. They do not encrypt the payload, do not identify a human
operator, and do not replace TLS. They also do not validate the event's own
`occurred_at`; the signed timestamp describes the delivery, not the business
event.

If `HOOKWATCH_REQUIRE_SIGNATURES=true` and no secret is configured, the process
refuses to start. Set it to `false` for local work without signatures; a
signature that is supplied is still verified in that mode, and a signed request
for a source with no secret is still refused.

## Inspecting events

`GET /events` returns stored events newest first. Filters are exact matches and
combine with AND:

| Query | Meaning |
| --- | --- |
| `source` | Sending service, for example `github`. |
| `event_type` | Provider event name, for example `invoice.paid`. |
| `status` | `received`, `processing`, `processed` or `failed`. |
| `dead_lettered` | `true` for events the engine has given up on, `false` for live events. |
| `limit` | Rows to return, 1 to 200. Default 50. |
| `offset` | Rows to skip. Default 0. |

```shell
curl "http://127.0.0.1:8000/events?status=failed&dead_lettered=true"
curl "http://127.0.0.1:8000/events?source=github&limit=20&offset=20"
```

The listing omits `data`; payloads belong to the detail view and can be large.

`GET /events/{id}` returns one event with its payload, processing attempts and
replay history:

```shell
curl http://127.0.0.1:8000/events/1
```

```json
{
  "id": 1,
  "source": "github",
  "event_id": "evt_1",
  "event_type": "invoice.paid",
  "occurred_at": "2026-11-09T12:00:00Z",
  "received_at": "2026-11-09T12:00:01.123456Z",
  "status": "failed",
  "dead_lettered_at": null,
  "replay_count": 0,
  "data": {"amount": 500},
  "attempts": [
    {
      "attempt_number": 1,
      "replay_number": 0,
      "started_at": "2026-11-09T12:00:01.400000Z",
      "finished_at": "2026-11-09T12:00:01.420000Z",
      "outcome": "failed",
      "failure_kind": "retryable",
      "error": "downstream timeout"
    }
  ],
  "replays": []
}
```

Attempts are what makes a failure investigable: each row shows which try ran,
whether it succeeded, whether the failure was retryable or permanent, and the
stored error. An unknown id returns `404`; an invalid filter returns `422`.

## Replay

`POST /events/{id}/replay` queues an existing event for another processing
cycle. Replay is an operator action, not a delivery: it never creates a second
event row, so the unique index on `(source, event_id)` and duplicate detection
are untouched.

Replay is disabled unless `HOOKWATCH_REPLAY_TOKEN` is set. The caller must send
it as a bearer token, and a reason is required:

```shell
curl -i -X POST http://127.0.0.1:8000/events/1/replay \
  -H "Authorization: Bearer $HOOKWATCH_REPLAY_TOKEN" \
  -H "X-Operator: alice" \
  -H "Content-Type: application/json" \
  -d '{"reason": "fixed the downstream timeout"}'
```

```json
{"status": "queued", "id": 1, "replay_number": 1}
```

`202` means queued, not processed. The engine picks the event up like any other
waiting event. `X-Operator` is optional and recorded in the audit trail; it is
not verified.

| Status | Meaning |
| --- | --- |
| `202` | Queued for another cycle. |
| `401` | Token missing or wrong. |
| `403` | Replay is disabled because no token is configured. |
| `404` | No event has that id. |
| `409` | The event is `received` (already queued) or `processing` (in flight). |
| `422` | The reason is missing, shorter than 3 or longer than 500 characters, or the body has unknown fields. |

Safeguards, and why they exist:

- Replay is closed by default. A missing environment variable disables it
  instead of exposing it.
- Only `failed` and `processed` events can be replayed. Queuing an event that is
  already waiting, or replaying one that is mid-flight, is refused.
- The state check is repeated in the conditional `UPDATE` that performs the
  change, so two operators cannot both replay the same state. The second request
  gets `409`.
- Every accepted replay is recorded in the `replays` table with its number,
  timestamp, operator and reason.
- A replay starts a fresh attempt budget (`replay_count`) without deleting the
  earlier attempts. An event that had exhausted its retries can therefore fail
  again up to `max_attempts` times before being dead-lettered again.
- Replay calls the same processor as normal processing. Because processing is
  at-least-once, a processor that is unsafe to repeat is also unsafe to replay.

A shared token is not user authentication. It proves the caller knows the
operator secret, not who they are. Per-user authorization belongs to a later
stage.

## MCP server

Inspection and controlled replay are also available over the Model Context
Protocol, so an MCP-compatible AI client can work with stored events without
going through HTTP. The server lives in `app/mcp_server.py` and speaks stdio:

```shell
uv run python -m app.mcp_server
```

A client launches it as a child process. A typical configuration entry:

```json
{
  "mcpServers": {
    "hookwatch": {
      "command": "uv",
      "args": ["run", "python", "-m", "app.mcp_server"],
      "cwd": "C:/path/to/Hookwatch",
      "env": {
        "HOOKWATCH_DB_PATH": "C:/path/to/Hookwatch/hookwatch.db"
      }
    }
  }
}
```

`cwd` must be the project root so `uv` finds `pyproject.toml`, and
`HOOKWATCH_DB_PATH` must point at the database the web application writes to.
Relative paths resolve against the working directory. Start the web application
separately: the MCP process opens the same file directly and never runs its own
processing engine, so processing still has one owner.

### Hermes configuration

Hermes reads MCP servers from `~/.hermes/config.yaml`. Keep the token in
`~/.hermes/.env` and reference it with `${NAME}`, so the secret never sits in
the YAML:

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  hookwatch:
    command: "uv"
    args: ["run", "python", "-m", "app.mcp_server"]
    cwd: "C:/path/to/Hookwatch"
    env:
      HOOKWATCH_DB_PATH: "C:/path/to/Hookwatch/hookwatch.db"
      HOOKWATCH_REPLAY_TOKEN: "${HOOKWATCH_REPLAY_TOKEN}"
      HOOKWATCH_MCP_OPERATOR: "hermes"
    trust: untrusted
    tools:
      include:
        - list_events
        - get_event
        - search_failed_events
        - inspect_processing_history
        - replay_event
```

```shell
# ~/.hermes/.env
HOOKWATCH_REPLAY_TOKEN=replace-with-a-long-random-value
```

`trust: untrusted` makes Hermes ask before any tool that is not marked
read-only. The four inspection tools carry `readOnlyHint: true`, so they run
without a prompt. `replay_event` carries `readOnlyHint: false` and
`destructiveHint: true`, so Hermes prompts before every call. Removing
`replay_event` from `tools.include`, or unsetting the token, removes replay
entirely.

Restart Hermes after changing the configuration. Tool names are prefixed by the
server name, so they appear as `hookwatch_list_events` and so on.

### Example workflow

With the configuration above, a typical session looks like this:

1. "List the failed webhook events."
   The agent calls `list_events` or `search_failed_events`.
2. "Why did event 12 fail?"
   The agent calls `get_event` or `inspect_processing_history` and reads the
   attempts, failure kinds and stored errors.
3. "The timeout is fixed. Replay event 12."
   The agent calls `replay_event`, and Hermes asks for approval first. After
   the call the event is `received`, the web process's engine picks it up, and
   a later `get_event` shows the new attempt.

### Tools

| Tool | Arguments | Returns |
| --- | --- | --- |
| `list_events` | `source`, `event_type`, `status`, `dead_lettered`, `limit` (1-200, default 50), `offset` | Event summaries, newest first. |
| `get_event` | `record_id` | One event with payload, attempts and replay audit trail. |
| `search_failed_events` | `source`, `event_type`, `dead_lettered`, `limit`, `offset` | Failed events only, newest first. |
| `inspect_processing_history` | `record_id` | Attempts and replays without the payload. |
| `replay_event` | `record_id`, `reason` (3-500 characters) | Queues a failed or processed event for another cycle. Only registered when the process holds a replay token. |

Filters are exact matches and combine with AND, exactly like `GET /events`.
`dead_lettered: true` means the engine has given up on the event;
`dead_lettered: false` means the failure is still eligible for a retry;
omitting it returns both.

Tools return structured content plus text blocks, so clients that read either
form get the same data. The response models are shared with the HTTP API, with
`ProcessingHistory` added for the history-only view, and `app/views.py` builds
them for both interfaces. Every tool calls `EventService`; no SQL or business
rule is duplicated in the MCP module.

### Replay over MCP

Replay is the only mutating tool. It follows the same rules as the HTTP
endpoint:

- Only `failed` or `processed` events can be replayed. A `received` event is
  already queued and a `processing` event is in flight; both are refused.
- The event returns to `received` and keeps its id, so the database still holds
  one row per `(source, event_id)`. The web process's engine picks it up on its
  next pass.
- The call does not process anything. The MCP process still never starts the
  engine.
- Every replay writes an audit row containing the reason and
  `HOOKWATCH_MCP_OPERATOR` (default `mcp-agent`). That value names the
  configured process, not a verified person.

The token is held by the MCP server process and is never a tool argument, never
returned and never logged. When it is unset, `replay_event` is not registered at
all, so it does not appear in tool discovery and the model cannot call it. The
agent therefore cannot see the secret, supply it or work around it.

### Human approval and its limits

Two independent gates protect replay:

| Gate | Enforced by | What it proves |
| --- | --- | --- |
| Replay token | HookWatch | This process is allowed to replay. |
| Approval prompt | The MCP client, for example Hermes | A human chose this call. |

The second gate is client-side. Approvals can be turned off with YOLO mode or
`approvals.mode: off`, and HookWatch receives the same tool call either way. It
cannot tell whether a human clicked approve, because that decision never travels
with the request. The token still prevents an unconfigured process from
replaying, and the status rules still limit what can be replayed, but
server-enforced approval needs a separate design and belongs with the
authorization work in Stage 10.

This matters because `get_event` returns the payload, and payload text is
written by the sender, not by HookWatch. A malicious webhook could contain
instructions aimed at the agent. Keeping approval on is what stands between
untrusted payload text and a repeated side effect.

### Resource

`hookwatch://events/{record_id}` returns the same detail view as `get_event` as
JSON. Tools are the primary interface; the resource exists for clients that
prefer reading a URI.

### Behaviour

- The server applies pending migrations when it starts. Apart from the explicit
  `replay_event` call, no tool inserts, updates or deletes rows.
- An unknown id is a tool error with the message `event N does not exist`.
  Invalid arguments are rejected by the input schema before the service runs.
  Unexpected failures are logged and reported without exposing SQL or a stack
  trace.
- The transport is stdio only. The server is meant to be launched by a local
  client, not exposed on a network socket.

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
| `replay_count` | integer | How many replay cycles the event has had. `0` until the first replay. |

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
| `replay_number` | integer | The event's `replay_count` when this attempt ran. `0` for the original cycle. |
| `started_at` | text | When the attempt began, in UTC. |
| `finished_at` | text | When it ended, or null while it is open. |
| `outcome` | text | `succeeded`, `failed`, or null while it is open. |
| `failure_kind` | text | `retryable`, `permanent`, or null while it is open or after success. |
| `error` | text | The failure message, when there was one. |

Attempts exist so retries leave a history rather than overwriting a single
counter. The processing engine writes one row per try and applies the attempt
limit per replay cycle: it counts rows whose `replay_number` equals the event's
current `replay_count`. Earlier tries stay visible but no longer consume the
budget.

The `replays` table is the audit trail for operator replay requests:

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | integer | Internal replay key. |
| `event_row_id` | integer | The `events.id` that was replayed. |
| `replay_number` | integer | 1, 2, 3 and so on, matching the event's new `replay_count`. |
| `requested_at` | text | When the replay was accepted, in UTC. |
| `requested_by` | text | The `X-Operator` value, or `unspecified`. Not verified. |
| `reason` | text | The operator's reason, required by the API. |

`(event_row_id, replay_number)` is unique, so a replay request cannot be
recorded twice for the same cycle.

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
| `processed` | nothing, it is terminal for ordinary processing |

Any other move raises `InvalidTransitionError`. Starting a second processing run
on a `processed` event, for example, is refused instead of silently reopening
finished work. Finishing an attempt that is missing or already finished raises
`AttemptNotOpenError`, so closing the same attempt twice is a detectable mistake
rather than a silent overwrite.

Replay is the one explicit exception: it moves a `failed` or `processed` event
back to `received` and increments `replay_count`, and the next attempt belongs
to a new cycle. It is not part of `ALLOWED_TRANSITIONS` because it is not an
ordinary processing step. It is an audited operator operation with its own
service method, authorization and safeguards.

A failed attempt is classified as `retryable` or `permanent`:

- A retryable failure leaves the event `failed` and eligible for another try.
- A permanent failure, or a retryable failure that reaches the attempt limit,
  sets `dead_lettered_at`. The event stays `failed`, but the engine stops
  selecting it. The payload and every attempt remain in the database.

The HTTP API only creates `received` events on ingestion. The processing engine
moves them through the rest of the lifecycle using the same `EventService`
methods that the API uses. Replay is the only HTTP operation that moves an
existing event backwards, and it does so through its own audited path.

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
| 5 | Adds `events.replay_count`, `attempts.replay_number` and the `replays` audit table. |

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
{"id": 2, "source": "github", "event_id": "evt_...", "event_type": "invoice.paid", "occurred_at": "2026-11-08T08:24:54+00:00", "received_at": "2026-11-08T08:24:54+00:00", "status": "processed", "dead_lettered_at": null, "replay_count": 0, "data": {}}
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
- Replay uses one shared bearer token. It proves the caller knows the operator
  secret, not who they are, and the `X-Operator` value is recorded but not
  verified.
- Replay re-runs the same processor. It does not make an unsafe side effect
  safe; at-least-once still applies.
- Event filtering is exact match on a few columns. There is no full-text search
  over payloads, dead-letter queue UI or saved filter.
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
and finish time come from the existing `attempts` table, counted within the
current replay cycle (`replay_number` equal to `events.replay_count`), so no
extra retry counter column is needed.

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
distinguishes "failed, will retry" from "failed, given up". Replay clears the
marker and starts a new attempt cycle, so a dead letter can be retried after the
cause is fixed.

### Crash recovery

An open attempt is a lease. If the process dies after claiming an event, the
attempt keeps `finished_at` null and the event stays `processing` forever.

Before each batch the engine looks for open attempts whose `started_at` is older
than `stale_after_seconds`. It closes them as failed with an `abandoned` error
and moves their events to `failed`, so the normal retry path takes over. The
abandoned attempt counts toward the limit, because a crash is a failed attempt.

### Settings

The engine reads its values from `ProcessingSettings`. In the web process they
are filled from the `HOOKWATCH_*` variables listed under
[Configuration](#configuration). The defaults are:

| Internal setting | Environment variable | Default | Meaning |
| --- | --- | --- | --- |
| `poll_interval_seconds` | `HOOKWATCH_POLL_INTERVAL_SECONDS` | `1.0` | Sleep when a batch had no due work. |
| `batch_size` | `HOOKWATCH_BATCH_SIZE` | `10` | Maximum events examined in one call. |
| `max_attempts` | `HOOKWATCH_MAX_ATTEMPTS` | `5` | Attempts per replay cycle before an event is dead-lettered. |
| `backoff_base_seconds` | `HOOKWATCH_BACKOFF_BASE_SECONDS` | `2.0` | Wait after the first failure. |
| `backoff_cap_seconds` | `HOOKWATCH_BACKOFF_CAP_SECONDS` | `60.0` | Largest wait between attempts. |
| `stale_after_seconds` | `HOOKWATCH_STALE_AFTER_SECONDS` | `300.0` | Age at which an open attempt is treated as abandoned. |

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

Signing is automatic when the source has a secret in
`HOOKWATCH_WEBHOOK_SECRETS`; `--secret` overrides the lookup. Without either, the
tool sends unsigned requests, which only works against a server running with
enforcement disabled.

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

The API test fixtures build an explicit `Settings` with `engine_enabled=False`
and `require_signatures=False`, so no test depends on the environment and
assertions about the initial `received` state are deterministic. Engine tests
enable processing explicitly; the signature suite enables enforcement and signs
every delivery.

`tests/test_settings.py` covers environment parsing, boolean spellings, blank
values falling back to defaults, rejected values, webhook secret parsing and
validation, tolerance bounds, and the rule that neither the replay token nor the
webhook secrets appear in the settings repr.

`tests/test_signatures.py` covers valid deliveries, wrong secrets, tampered
bodies, stale and future timestamps, malformed headers, unconfigured sources,
enforcement switched off, and the startup refusal when enforcement has no
secret. `tests/test_simulator.py` proves the simulator signs the exact bytes it
sends and picks up the configured per-source secret.

The MCP tests use the SDK's in-memory client against a temporary seeded
database. They cover tool discovery, structured results, filtering, paging,
unknown ids, invalid arguments, the processing history view and resource reads.
Replay tests cover the closed default, explicit disabling, tool annotations,
operator attribution, refused states and the guarantee that the token never
appears in discovery or tool output.

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
- The event's own `occurred_at` is not checked for freshness. Signature
  verification now rejects stale deliveries, but that timestamp describes the
  delivery rather than the business event, and an event may legitimately arrive
  late. Enforcing `occurred_at` freshness would be a separate policy.
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
|   |   |-- events.py     Inspection, filtering and replay endpoints
|   |   |-- replay_auth.py Replay bearer token check
|   |   |-- signatures.py   HMAC parsing and verification for deliveries
|   |   `-- webhooks.py   Webhook receiver endpoint and error logging
|   |-- db.py             Connections, migrations and row models
|   |-- engine.py         Background processing loop
|   |-- lifecycle.py      Event statuses, allowed transitions and failure kinds
|   |-- main.py           App factory, routers, engine startup and logging
|   |-- mcp_server.py     MCP server over stdio, read-only unless replay is enabled
|   |-- repositories/
|   |   |-- __init__.py
|   |   `-- events.py     EventRepository, the only place with event SQL
|   |-- schemas/
|   |   |-- __init__.py
|   |   |-- event.py      WebhookEvent data contract
|   |   `-- inspection.py Response models for monitoring, replay and MCP
|   |-- services/
|   |   |-- __init__.py
|   |   `-- events.py     EventService and processing policy
|   |-- settings.py       Validated environment configuration
|   `-- views.py          Shared row-to-response mapping for HTTP and MCP
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
|   |-- test_monitoring.py     Listing, filtering and detail endpoint tests
|   |-- test_replay.py         Replay service, authorization and API tests
|   |-- test_settings.py       Configuration parsing and validation tests
|   |-- test_signatures.py     Signature parsing, verification and enforcement tests
|   |-- test_mcp_server.py     MCP tools, replay gating, approval hints and resource tests
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
`app/services/` so the HTTP API and the MCP server call the same methods.
`app/views.py` is the single mapping from stored rows to the response models
both interfaces return.

## Configuration

The web process reads its configuration from environment variables once at
startup, through `app/settings.py`. Values are validated before the application
or the engine starts. An invalid value stops startup with a message that names
the problem, rather than silently falling back to a default.

| Variable | Default | Applies to | Meaning |
| --- | --- | --- | --- |
| `HOOKWATCH_DB_PATH` | `hookwatch.db` | both processes | SQLite database file. |
| `HOOKWATCH_REPLAY_TOKEN` | unset | both processes | Enables replay. Required by `POST /events/{id}/replay` and by the MCP `replay_event` tool. Unset or blank disables replay. |
| `HOOKWATCH_ENGINE_ENABLED` | `true` | web | Start the in-process processing engine. |
| `HOOKWATCH_LOG_LEVEL` | `INFO` | web | Root log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` or `CRITICAL`. |
| `HOOKWATCH_WEBHOOK_SECRETS` | empty | web | `source=secret` pairs used to verify sender signatures. Required while enforcement is on. |
| `HOOKWATCH_REQUIRE_SIGNATURES` | `true` | web | Require a valid signature for every delivery. True with no secrets stops startup. |
| `HOOKWATCH_SIGNATURE_TOLERANCE_SECONDS` | `300` | web | Accepted difference between the signed timestamp and the server clock, 1 to 86400. |
| `HOOKWATCH_POLL_INTERVAL_SECONDS` | `1.0` | web | Wait before the next pass when nothing was due. |
| `HOOKWATCH_BATCH_SIZE` | `10` | web | Maximum events examined per pass. |
| `HOOKWATCH_MAX_ATTEMPTS` | `5` | web | Attempts per replay cycle before an event is dead-lettered. |
| `HOOKWATCH_BACKOFF_BASE_SECONDS` | `2.0` | web | Delay after the first retryable failure. |
| `HOOKWATCH_BACKOFF_CAP_SECONDS` | `60.0` | web | Maximum backoff delay. Must be at least the base. |
| `HOOKWATCH_STALE_AFTER_SECONDS` | `300.0` | web | Age at which an open attempt is treated as abandoned. |
| `HOOKWATCH_MCP_OPERATOR` | `mcp-agent` | MCP | Name recorded as `requested_by` in the replay audit trail. |

Booleans accept `true`, `false`, `1`, `0`, `yes`, `no`, `on` and `off`,
case-insensitive. Blank values fall back to the default. Numeric values must be
finite; `nan` and `inf` are rejected, as are zero or negative intervals, a zero
batch size, zero attempts, a backoff cap below the base, and a signature
tolerance outside 1 to 86400 seconds.

`HOOKWATCH_WEBHOOK_SECRETS` is a comma-separated list of `source=secret` pairs.
Each pair is split at the first `=`, so a secret may contain one. Source names
use the same 1 to 64 character rule as the URL segment. Duplicate sources,
empty pairs and empty secrets are rejected. A comma cannot appear in a secret
because comma separates the pairs; generated secrets do not need one.

Both processes read `HOOKWATCH_DB_PATH`; they must use the same path for the MCP
tools to see the events the API stored. The MCP server also reads
`HOOKWATCH_REPLAY_TOKEN` to decide whether to register `replay_event`, and
`HOOKWATCH_MCP_OPERATOR` to name itself in the audit trail.

### Running more than one worker

If uvicorn runs several workers, each process would otherwise start its own
polling loop. The database claim keeps processing correct, but the loops are
redundant. Choose one shape:

- one worker, engine enabled;
- several workers, plus one process with `HOOKWATCH_ENGINE_ENABLED=true` and the
  rest with it set to `false`.

Configuration is read once at startup. Changing an environment variable requires
a restart; there is no config file and no reload at runtime.

The replay token and the webhook secrets are secrets. They are read from the
environment at startup and are never written to the database, the logs or an MCP
tool argument. The settings object hides both from its `repr`, so logging the
settings does not print them. Sender signatures authenticate a service, not a
person; operator authentication is separate work and is not implemented yet.

## Not implemented yet

These are planned but deliberately absent, so nothing here should be assumed to
exist:

- Per-user authentication and authorization; replay still uses one shared token
- Rate limiting at the HTTP layer
- Payload size limits at the HTTP layer
- Per-source adapters for third-party signature formats such as
  `Stripe-Signature` or `X-Hub-Signature-256`; HookWatch currently defines its
  own header
- Retention, archival or cleanup of stored events
- Operational metrics or dashboards
- Other mutating tools over MCP beyond controlled replay
- MCP transports other than stdio, MCP authentication and resource subscriptions
- Approval enforced by HookWatch itself; approval currently depends on the MCP
  client honouring the tool annotations
- Configuration files (YAML or TOML); only environment variables are supported
- Hot reload of configuration values; a restart is required
