from __future__ import annotations

import concurrent.futures
import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import sessionmaker

from app.api.dependencies import get_principal, get_runtime_state
from app.api.routes import jobs
from app.auth.security import Principal
from app.state.models import InferenceJob, Message, Tenant, User
from app.state.repositories import (
    ApplicationRepository,
    AsyncApplicationRepository,
    IdempotencyConflictError,
)

pytestmark = pytest.mark.integration


def _client(runtime, principal):
    app = FastAPI()
    app.include_router(jobs.router)
    app.dependency_overrides[get_runtime_state] = lambda: runtime
    app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app)


def test_postgres_concurrency_tenant_isolation_and_job_lease():
    database_url = os.getenv("INTEGRATION_DATABASE_URL")
    if not database_url:
        pytest.skip("Integration PostgreSQL URL is not configured.")

    suffix = uuid.uuid4().hex
    engine = create_engine(database_url, pool_size=5)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as session:
        tenant = Tenant(slug=f"auth-{suffix}", name="Auth integration")
        other_tenant = Tenant(slug=f"auth-other-{suffix}", name="Other")
        session.add_all([tenant, other_tenant])
        session.flush()
        owner = User(
            tenant_id=tenant.id,
            username=f"owner-{suffix}",
            display_name="Owner",
        )
        same_tenant_user = User(
            tenant_id=tenant.id,
            username=f"peer-{suffix}",
            display_name="Peer",
        )
        outsider = User(
            tenant_id=other_tenant.id,
            username=f"outsider-{suffix}",
            display_name="Outsider",
        )
        session.add_all([owner, same_tenant_user, outsider])
        session.flush()

    repository = ApplicationRepository(factory)
    payload = {"question": "same", "conversation_id": None}

    def create_same_job():
        return repository.create_job(
            tenant_id=tenant.id,
            user_id=owner.id,
            question="same",
            request_payload=payload,
            contract_version="1.0",
            max_attempts=3,
            idempotency_key=f"same-{suffix}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: create_same_job(), range(2)))
    assert sum(result.created for result in results) == 1
    job_id = results[0].job.id
    conversation_id = results[0].job.conversation_id

    def create_message(index):
        return repository.create_job(
            tenant_id=tenant.id,
            user_id=owner.id,
            question=f"message-{index}",
            request_payload={"question": f"message-{index}"},
            contract_version="1.0",
            max_attempts=3,
            conversation_id=conversation_id,
            idempotency_key=f"message-{suffix}-{index}",
        )

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(create_message, range(3)))

    with factory() as session:
        sequences = list(
            session.scalars(
                select(Message.sequence_number)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.sequence_number)
            )
        )
        assert sequences == [1, 2, 3, 4]
        assert (
            session.scalar(
                select(func.count(InferenceJob.id)).where(
                    InferenceJob.idempotency_key == f"same-{suffix}"
                )
            )
            == 1
        )

    with pytest.raises(IdempotencyConflictError):
        repository.create_job(
            tenant_id=tenant.id,
            user_id=owner.id,
            question="different",
            request_payload={"question": "different"},
            contract_version="1.0",
            max_attempts=3,
            idempotency_key=f"same-{suffix}",
        )

    claimed = repository.claim_job(job_id, "worker-a", 300)
    assert claimed is not None
    _, lease_token = claimed
    assert repository.claim_job(job_id, "worker-b", 300) is None
    assert repository.renew_job_lease(job_id, lease_token, 300) is True

    runtime = {
        "execution_mode": "queued",
        "repository": AsyncApplicationRepository(repository),
    }
    owner_principal = Principal(
        tenant_id=tenant.id,
        user_id=owner.id,
        username=owner.username,
        role="user",
        auth_method="test",
        scopes=frozenset({"*"}),
    )
    peer_principal = Principal(
        tenant_id=tenant.id,
        user_id=same_tenant_user.id,
        username=same_tenant_user.username,
        role="user",
        auth_method="test",
        scopes=frozenset({"*"}),
    )
    outsider_principal = Principal(
        tenant_id=other_tenant.id,
        user_id=outsider.id,
        username=outsider.username,
        role="user",
        auth_method="test",
        scopes=frozenset({"*"}),
    )
    assert _client(runtime, owner_principal).get(f"/v1/inference-jobs/{job_id}").status_code == 200
    for principal in (peer_principal, outsider_principal):
        client = _client(runtime, principal)
        assert client.get(f"/v1/inference-jobs/{job_id}").status_code == 404
        assert client.get(f"/v1/inference-jobs/{job_id}/events").status_code == 404
        assert client.get(f"/v1/conversations/{conversation_id}").status_code == 404

    with factory.begin() as session:
        session.execute(delete(Tenant).where(Tenant.id.in_([tenant.id, other_tenant.id])))
    engine.dispose()
