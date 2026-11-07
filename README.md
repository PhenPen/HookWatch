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

Stage 0 (project initialization) is complete. The application starts, serves a
health check and has a tested baseline. No webhook receiving, persistence or
MCP functionality exists yet. Stages are implemented one at a time, and each
stage is committed separately.

| Stage | Scope | Status |
| --- | --- | --- |
| 0 | Repository inspection and project initialization | Complete |
| 1 | Webhook data contract | Not started |
| 2 | Webhook simulator | Not started |
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

## Tests

```shell
uv run pytest
```

## Project layout

```text
.
|-- app/
|   |-- __init__.py
|   `-- main.py           FastAPI application and routes
|-- tests/
|   `-- test_health.py    Health endpoint test
|-- pyproject.toml        Project metadata, dependencies, pytest configuration
|-- uv.lock               Reproducible dependency lock file
|-- .python-version       Python version selected by uv
`-- hookwatch_implementation.md
```

The package layout is intentionally minimal. Directories for schemas,
services, persistence and MCP will be added when a stage needs them, not in
advance.

## Configuration

No configuration is required yet. HookWatch does not read environment
variables, connect to a database or hold any secrets at this stage.

## Not implemented yet

These are planned but deliberately absent, so nothing here should be assumed to
exist:

- Webhook receiver endpoints
- Event schemas and validation of incoming payloads
- Any database or persistence layer
- Automatic database schema migrations
- Background processing, retries or dead-letter handling
- Signature verification, rate limiting or authentication
- MCP tools and resources
