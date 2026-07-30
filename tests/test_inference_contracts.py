import json
import uuid

import pytest

from app.inference.contracts import (
    CompletedEvent,
    CompletedPayload,
    InferenceJobContract,
    UnsupportedContractVersion,
    parse_event_contract,
    parse_job_contract,
)


def test_job_contract_round_trip():
    contract = InferenceJobContract(
        job_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        message_id=uuid.uuid4(),
        question="What is Spark?",
    )

    assert parse_job_contract(contract.model_dump_json()) == contract


def test_event_contract_round_trip():
    event = CompletedEvent(
        event_id="event-1",
        sequence=4,
        job_id=uuid.uuid4(),
        correlation_id=uuid.uuid4(),
        event_type="completed",
        payload=CompletedPayload(answer="done", mode="rag_docs"),
    )

    parsed = parse_event_contract(event.model_dump_json())

    assert parsed.event_type == "completed"
    assert parsed.payload.answer == "done"


def test_unsupported_job_contract_version_is_controlled():
    payload = {
        "contract_version": "99",
        "job_id": str(uuid.uuid4()),
    }

    with pytest.raises(UnsupportedContractVersion):
        parse_job_contract(json.dumps(payload))


def test_unsupported_event_contract_version_is_controlled():
    payload = {
        "event_contract_version": "99",
        "event_type": "completed",
    }

    with pytest.raises(UnsupportedContractVersion):
        parse_event_contract(json.dumps(payload))
