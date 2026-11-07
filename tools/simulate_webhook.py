"""Send simulated webhook events to a running HookWatch instance.

This is a development tool, not part of the application. It exists so the
receiver can be exercised without a real third-party service. By default every
event it builds is validated against the same contract the application uses, so
a simulated delivery is always a valid delivery. The --raw option bypasses that
validation on purpose, which is how rejection paths are exercised.

Examples:

    uv run python -m tools.simulate_webhook
    uv run python -m tools.simulate_webhook --event-type user.created --count 3
    uv run python -m tools.simulate_webhook --data '{"amount": 500}'
    uv run python -m tools.simulate_webhook --event-id evt_fixed --count 2
    uv run python -m tools.simulate_webhook --raw '{"event_id": ""}'

Sending the same event id twice is how duplicate deliveries will be simulated
once duplicate detection exists.

Exit codes:

    0  every delivery returned a 2xx response
    1  at least one delivery failed or the request could not be sent
    2  the arguments or the generated event were invalid
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Sequence

import httpx2
from pydantic import ValidationError

from app.schemas.event import WebhookEvent

DEFAULT_URL = "http://127.0.0.1:8000/webhooks/simulator"
DEFAULT_EVENT_TYPE = "simulator.ping"


@dataclass(frozen=True)
class SendResult:
    """What the target application answered for one delivery."""

    status_code: int
    body: str

    @property
    def ok(self) -> bool:
        """Whether the target accepted the event."""
        return 200 <= self.status_code < 300


def build_event(
    event_type: str,
    data: dict[str, Any],
    event_id: str | None = None,
) -> WebhookEvent:
    """Build a valid event with a fresh id and the current UTC time.

    Passing an explicit event_id reuses it, which is how the same event can be
    delivered more than once.
    """
    return WebhookEvent(
        event_id=event_id or f"evt_{uuid.uuid4().hex}",
        event_type=event_type,
        occurred_at=datetime.now(timezone.utc),
        data=data,
    )


def post_json(client: httpx2.Client, url: str, payload: Any) -> SendResult:
    """Post one JSON payload and return the response instead of raising.

    Error responses are data here: the simulator exists to inspect them, so a
    400 or 500 is reported rather than turned into an exception.
    """
    response = client.post(url, json=payload)
    return SendResult(status_code=response.status_code, body=response.text)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="simulate_webhook",
        description="Send simulated webhook events to HookWatch.",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"target URL (default: {DEFAULT_URL})",
    )
    parser.add_argument(
        "--event-type",
        default=DEFAULT_EVENT_TYPE,
        help=f"event type to send (default: {DEFAULT_EVENT_TYPE})",
    )
    parser.add_argument(
        "--data",
        default="{}",
        help='JSON object sent as the event data (default: {})',
    )
    parser.add_argument(
        "--raw",
        default=None,
        metavar="JSON",
        help="send this JSON as-is, without building or validating an event",
    )
    parser.add_argument(
        "--event-id",
        default=None,
        help="reuse a fixed event id instead of generating one per delivery",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=1,
        help="number of deliveries to send (default: 1)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5.0,
        help="request timeout in seconds (default: 5)",
    )
    return parser.parse_args(argv)


def parse_json(text: str, option: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        print(f"error: {option} is not valid JSON: {error}", file=sys.stderr)
        raise SystemExit(2) from error


def build_payloads(args: argparse.Namespace) -> list[Any]:
    """Return one payload per delivery, validating unless --raw was used."""
    if args.raw is not None:
        return [parse_json(args.raw, "--raw")] * args.count

    data = parse_json(args.data, "--data")
    if not isinstance(data, dict):
        print("error: --data must be a JSON object", file=sys.stderr)
        raise SystemExit(2)

    payloads: list[Any] = []
    for _ in range(args.count):
        try:
            event = build_event(args.event_type, data, args.event_id)
        except ValidationError as error:
            print("error: generated event is invalid:", file=sys.stderr)
            print(f"  {error}", file=sys.stderr)
            raise SystemExit(2) from error
        payloads.append(event.model_dump(mode="json"))
    return payloads


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)

    if args.count < 1:
        print("error: --count must be at least 1", file=sys.stderr)
        return 2

    try:
        payloads = build_payloads(args)
    except SystemExit as exit_request:
        return int(exit_request.code or 0)

    print(f"POST {args.url}")
    if args.raw is not None:
        print(f"  raw payload, sent without validation, count={args.count}")
    else:
        print(f"  event_type={args.event_type} count={args.count}")

    exit_code = 0
    with httpx2.Client(timeout=args.timeout) as client:
        for index, payload in enumerate(payloads, start=1):
            try:
                result = post_json(client, args.url, payload)
            except httpx2.HTTPError as error:
                print(
                    f"  [{index}/{len(payloads)}] request failed: {error}",
                    file=sys.stderr,
                )
                exit_code = 1
                continue

            label = ""
            if isinstance(payload, dict) and "event_id" in payload:
                label = f" event_id={payload['event_id']}"
            print(f"  [{index}/{len(payloads)}]{label}")
            print(f"    -> {result.status_code} {result.body}".rstrip())
            if not result.ok:
                exit_code = 1

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
