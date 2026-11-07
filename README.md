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

Stage 2 (webhook simulator) is complete. The application starts, serves a
health check and validates webhook event envelopes against a tested Pydantic
model. A development tool can send simulated events to any URL and report the
response, but the application still has no receiver endpoint, so simulated
events currently receive a 404. Stages are implemented one at a time, and each
stage is committed separately.

| Stage | Scope | Status |
| --- | --- | --- |
| 0 | Repository inspection and project initialization | Complete |
| 1 | Webhook data contract | Complete |
| 2 | Webhook simulator | Complete |
| 3 | Webhook receiver | Not started |
| 4 | Persistence | Not started |
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

This is expected to fail against the application today: the receiver endpoint
arrives in Stage 3, so requests to `/webhooks/...` return `404 Not Found` and the
tool exits with `1`. Against any server that answers 2xx it exits with `0`.

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
|   |-- main.py           FastAPI application and routes
|   `-- schemas/
|       |-- __init__.py
|       `-- event.py      WebhookEvent data contract
|-- tests/
|   |-- test_health.py         Health endpoint test
|   |-- test_event_schema.py   Event contract validation tests
|   `-- test_simulator.py      Webhook simulator tests
|-- tools/
|   `-- simulate_webhook.py    Development tool that sends simulated events
|-- pyproject.toml        Project metadata, dependencies, pytest configuration
|-- uv.lock               Reproducible dependency lock file
|-- .python-version       Python version selected by uv
`-- hookwatch_implementation.md   Local specification, not tracked by Git
```

The package layout is intentionally minimal. Directories for services,
persistence and MCP will be added when a stage needs them, not in advance.

## Configuration

No configuration is required yet. HookWatch does not read environment
variables, connect to a database or hold any secrets at this stage.

## Not implemented yet

These are planned but deliberately absent, so nothing here should be assumed to
exist:

- Webhook receiver endpoints
- Any database or persistence layer
- Automatic database schema migrations
- Background processing, retries or dead-letter handling
- Signature verification, rate limiting or authentication
- MCP tools and resources
