from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.event import WebhookEvent

VALID_EVENT = {
    "event_id": "evt_123",
    "event_type": "invoice.paid",
    "occurred_at": "2026-11-07T12:00:00Z",
    "data": {"amount": 500, "currency": "USD"},
}


def test_accepts_valid_event_with_utc_offset() -> None:
    event = WebhookEvent.model_validate(VALID_EVENT)

    assert event.event_id == "evt_123"
    assert event.event_type == "invoice.paid"
    assert event.occurred_at == datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    assert event.data == {"amount": 500, "currency": "USD"}


def test_accepts_valid_event_with_positive_offset() -> None:
    payload = {**VALID_EVENT, "occurred_at": "2026-11-07T13:00:00+01:00"}

    event = WebhookEvent.model_validate(payload)

    # 13:00+01:00 is the same instant as 12:00Z, and the offset is preserved.
    assert event.occurred_at == datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    assert event.occurred_at.utcoffset() is not None


def test_accepts_documented_json_payload() -> None:
    event = WebhookEvent.model_validate_json(
        '{"event_id": "evt_1", "event_type": "invoice.paid",'
        ' "occurred_at": "2026-11-07T12:00:00Z", "data": {}}'
    )

    assert event.event_id == "evt_1"
    assert event.data == {}


def test_serialized_event_can_be_validated_again() -> None:
    event = WebhookEvent.model_validate(VALID_EVENT)

    reserialized = event.model_dump(mode="json")
    again = WebhookEvent.model_validate(reserialized)

    assert again == event
    assert isinstance(reserialized["occurred_at"], str)


def test_strips_surrounding_whitespace_from_identifiers() -> None:
    payload = {**VALID_EVENT, "event_id": "  evt_1  ", "event_type": " invoice.paid "}

    event = WebhookEvent.model_validate(payload)

    assert event.event_id == "evt_1"
    assert event.event_type == "invoice.paid"


def test_allows_empty_data_object() -> None:
    event = WebhookEvent.model_validate({**VALID_EVENT, "data": {}})

    assert event.data == {}


@pytest.mark.parametrize("field", ["event_id", "event_type", "occurred_at", "data"])
def test_rejects_missing_field(field: str) -> None:
    payload = {key: value for key, value in VALID_EVENT.items() if key != field}

    with pytest.raises(ValidationError):
        WebhookEvent.model_validate(payload)


@pytest.mark.parametrize("value", ["", "   "])
def test_rejects_blank_event_id(value: str) -> None:
    with pytest.raises(ValidationError):
        WebhookEvent.model_validate({**VALID_EVENT, "event_id": value})


def test_rejects_blank_event_type() -> None:
    with pytest.raises(ValidationError):
        WebhookEvent.model_validate({**VALID_EVENT, "event_type": "  "})


@pytest.mark.parametrize("value", [[], "payload", 42, None])
def test_rejects_non_object_data(value: object) -> None:
    with pytest.raises(ValidationError):
        WebhookEvent.model_validate({**VALID_EVENT, "data": value})


def test_rejects_unparseable_occurred_at() -> None:
    with pytest.raises(ValidationError):
        WebhookEvent.model_validate({**VALID_EVENT, "occurred_at": "yesterday"})


def test_rejects_naive_occurred_at() -> None:
    with pytest.raises(ValidationError):
        WebhookEvent.model_validate(
            {**VALID_EVENT, "occurred_at": "2026-11-07T12:00:00"}
        )


def test_rejects_unknown_field() -> None:
    with pytest.raises(ValidationError):
        WebhookEvent.model_validate({**VALID_EVENT, "source": "stripe"})


def test_rejects_non_string_event_id() -> None:
    with pytest.raises(ValidationError):
        WebhookEvent.model_validate({**VALID_EVENT, "event_id": 123})
