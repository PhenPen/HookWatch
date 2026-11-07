"""The data contract for webhook events.

Every incoming webhook is expected to carry an envelope shaped like this:

    {
        "event_id": "evt_123",
        "event_type": "invoice.paid",
        "occurred_at": "2026-11-07T12:00:00Z",
        "data": {"amount": 500}
    }

The envelope is ours to define. The contents of "data" belong to the sending
service, so they are stored as-is and deliberately not validated here.
"""

from typing import Annotated, Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, StringConstraints

# Identifiers are provider-defined strings, not UUIDs. Stripe uses "evt_..."
# while GitHub sends a GUID in a header, so a UUID type would reject valid
# senders. The constraints only reject identifiers that are blank or absurdly
# long.
EventId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=255),
]

# Event names are provider-defined too ("invoice.paid", "push", ...), so an
# enum would reject senders we have not seen yet.
EventType = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=100),
]


class WebhookEvent(BaseModel):
    """A single webhook event as received from an external service.

    Fields produced by HookWatch itself (the source, the moment we received the
    event, processing status and attempt counts) are intentionally absent. They
    are added by the receiver and the persistence layer in later stages, not
    supplied by the sender.
    """

    # Reject unknown top-level fields rather than silently discarding them, so
    # that a misspelled field name fails loudly while the contract is still
    # small and under our control.
    model_config = ConfigDict(extra="forbid")

    event_id: EventId
    event_type: EventType
    occurred_at: AwareDatetime
    data: dict[str, Any]
