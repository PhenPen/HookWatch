import json
from datetime import datetime, timezone
from typing import Any

import httpx2
import pytest
from pydantic import ValidationError

from app.api.signatures import (
    SIGNATURE_HEADER,
    parse_signature_header,
    sign,
)
from tools.simulate_webhook import (
    SendResult,
    build_event,
    main,
    post_json,
    resolve_secret,
    source_from_url,
)


def test_build_event_satisfies_the_contract() -> None:
    event = build_event("invoice.paid", {"amount": 500})

    assert event.event_id.startswith("evt_")
    assert event.event_type == "invoice.paid"
    assert event.data == {"amount": 500}
    assert event.occurred_at.tzinfo is not None
    assert abs((datetime.now(timezone.utc) - event.occurred_at).total_seconds()) < 5


def test_build_event_generates_a_unique_id_each_time() -> None:
    assert build_event("a", {}).event_id != build_event("a", {}).event_id


def test_build_event_reuses_an_explicit_event_id() -> None:
    event = build_event("a", {}, event_id="evt_fixed")

    assert event.event_id == "evt_fixed"


def test_build_event_rejects_a_blank_event_type() -> None:
    with pytest.raises(ValidationError):
        build_event("   ", {})


def make_client(
    status_code: int, body: str
) -> tuple[httpx2.Client, list[httpx2.Request]]:
    """Return a client whose requests are recorded instead of sent."""
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        return httpx2.Response(status_code, text=body)

    return httpx2.Client(transport=httpx2.MockTransport(handler)), requests


def test_post_json_posts_the_serialized_event() -> None:
    client, requests = make_client(202, '{"status": "accepted"}')
    event = build_event("invoice.paid", {"amount": 500})

    with client:
        post_json(client, "http://hookwatch.test/webhooks/demo", event.model_dump(mode="json"))

    assert len(requests) == 1
    sent = requests[0]
    assert sent.method == "POST"
    assert str(sent.url) == "http://hookwatch.test/webhooks/demo"
    body = json.loads(sent.content)
    assert body["event_id"] == event.event_id
    assert body["event_type"] == "invoice.paid"
    assert body["data"] == {"amount": 500}
    assert isinstance(body["occurred_at"], str)


def test_post_json_reports_acceptance() -> None:
    client, _ = make_client(202, '{"status": "accepted"}')

    with client:
        result = post_json(client, "http://hookwatch.test/hook", {"event_id": "evt_1"})

    assert result == SendResult(status_code=202, body='{"status": "accepted"}')
    assert result.ok


@pytest.mark.parametrize("status_code", [400, 404, 422, 500])
def test_post_json_reports_failure_without_raising(status_code: int) -> None:
    client, _ = make_client(status_code, '{"detail": "rejected"}')

    with client:
        result = post_json(client, "http://hookwatch.test/hook", {"event_id": "evt_1"})

    assert result.status_code == status_code
    assert result.body == '{"detail": "rejected"}'
    assert not result.ok


def test_main_rejects_invalid_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--data", "{not json"]) == 2
    assert "--data is not valid JSON" in capsys.readouterr().err


def test_main_rejects_data_that_is_not_an_object(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--data", "[1, 2]"]) == 2
    assert "--data must be a JSON object" in capsys.readouterr().err


def test_main_rejects_invalid_raw_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--raw", "not json"]) == 2
    assert "--raw is not valid JSON" in capsys.readouterr().err


def test_main_rejects_a_count_below_one(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--count", "0"]) == 2
    assert "--count must be at least 1" in capsys.readouterr().err


def test_main_rejects_an_invalid_event_type(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--event-type", "   "]) == 2
    assert "generated event is invalid" in capsys.readouterr().err


def test_main_reports_an_unreachable_target(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "--url",
            "http://127.0.0.1:1/webhooks/demo",
            "--timeout",
            "1",
        ]
    )

    assert code == 1
    assert "request failed" in capsys.readouterr().err


def test_main_returns_zero_when_the_target_accepts(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = make_client(202, '{"status": "accepted"}')
    monkeypatch.setattr("tools.simulate_webhook.httpx2.Client", lambda **_: client)

    assert main([]) == 0

    output = capsys.readouterr().out
    assert "202" in output
    assert "event_id=evt_" in output


def test_main_returns_one_when_the_target_rejects(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = make_client(422, '{"detail": "invalid"}')
    monkeypatch.setattr("tools.simulate_webhook.httpx2.Client", lambda **_: client)

    assert main([]) == 1

    assert "422" in capsys.readouterr().out


def test_main_sends_the_requested_number_of_deliveries(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, requests = make_client(202, "{}")
    monkeypatch.setattr("tools.simulate_webhook.httpx2.Client", lambda **_: client)

    assert main(["--count", "3"]) == 0

    assert len(requests) == 3
    event_ids = {json.loads(request.content)["event_id"] for request in requests}
    assert len(event_ids) == 3


def test_main_reuses_a_fixed_event_id(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, requests = make_client(202, "{}")
    monkeypatch.setattr("tools.simulate_webhook.httpx2.Client", lambda **_: client)

    assert main(["--count", "2", "--event-id", "evt_fixed"]) == 0

    event_ids = {json.loads(request.content)["event_id"] for request in requests}
    assert event_ids == {"evt_fixed"}


def test_main_sends_the_provided_data(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, requests = make_client(202, "{}")
    monkeypatch.setattr("tools.simulate_webhook.httpx2.Client", lambda **_: client)

    assert main(["--data", '{"amount": 900, "currency": "EUR"}']) == 0

    payload: dict[str, Any] = json.loads(requests[0].content)
    assert payload["data"] == {"amount": 900, "currency": "EUR"}


def test_raw_payload_bypasses_contract_validation(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, requests = make_client(422, '{"detail": "invalid event"}')
    monkeypatch.setattr("tools.simulate_webhook.httpx2.Client", lambda **_: client)

    # An empty event_id would be refused by WebhookEvent, which is the point.
    assert main(["--raw", '{"event_id": "", "event_type": "x"}']) == 1

    assert json.loads(requests[0].content) == {"event_id": "", "event_type": "x"}
    assert "422" in capsys.readouterr().out


def test_source_from_url_uses_the_last_path_segment() -> None:
    assert source_from_url("http://hookwatch.test/webhooks/github") == "github"
    assert source_from_url("http://hookwatch.test/webhooks/github/") == "github"


def test_post_json_signs_the_exact_bytes_it_sends() -> None:
    client, requests = make_client(202, '{"status": "accepted"}')

    with client:
        post_json(client, "http://hookwatch.test/webhooks/demo", {"a": 1}, "secret")

    sent = requests[0]
    parts = parse_signature_header(sent.headers[SIGNATURE_HEADER])
    assert parts.digest == sign("secret", sent.content, parts.timestamp)


def test_post_json_stays_unsigned_without_a_secret() -> None:
    client, requests = make_client(202, "{}")

    with client:
        post_json(client, "http://hookwatch.test/webhooks/demo", {"a": 1})

    assert SIGNATURE_HEADER not in requests[0].headers


def test_resolve_secret_prefers_the_explicit_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOOKWATCH_WEBHOOK_SECRETS", "github=from-env")

    assert resolve_secret("http://x/webhooks/github", "explicit") == "explicit"


def test_resolve_secret_reads_the_configured_map(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "HOOKWATCH_WEBHOOK_SECRETS",
        "github=from-env,stripe=other",
    )

    assert resolve_secret("http://x/webhooks/stripe", None) == "other"
    assert resolve_secret("http://x/webhooks/unknown", None) is None


def test_resolve_secret_returns_none_without_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HOOKWATCH_WEBHOOK_SECRETS", raising=False)

    assert resolve_secret("http://x/webhooks/github", None) is None


def test_resolve_secret_warns_about_a_malformed_map(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("HOOKWATCH_WEBHOOK_SECRETS", "github")

    assert resolve_secret("http://x/webhooks/github", None) is None
    assert "warning:" in capsys.readouterr().err
