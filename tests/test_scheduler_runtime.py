from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.scheduler.main import Scheduler


class DirectCalls:
    async def call(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class FakeRedis:
    def __init__(self, leader=True):
        self.leader = leader
        self.deleted = False

    async def set(self, *_args, **_kwargs):
        if self.leader:
            self.leader = False
            return True
        return False

    async def eval(self, script, *_args):
        if "DEL" in script:
            self.deleted = True
        return 1


class Operations:
    def __init__(self):
        self.attempt_id = uuid.uuid4()
        self.token = uuid.uuid4()
        self.schedule_id = uuid.uuid4()
        self.tenant_id = uuid.uuid4()
        self.source_id = uuid.uuid4()
        self.completed = []
        self.failed = []

    def claim_due_schedules(self, *_args, **_kwargs):
        return [
            (
                self.attempt_id,
                self.token,
                self.schedule_id,
                self.tenant_id,
                self.source_id,
                datetime.now(UTC),
            )
        ]

    def complete_schedule_attempt(self, *args):
        self.completed.append(args)
        return True

    def fail_schedule_attempt(self, *args):
        self.failed.append(args)
        return True


class Catalog:
    def create_refresh_job(self, *_args, **_kwargs):
        return SimpleNamespace(id=uuid.uuid4()), SimpleNamespace(id=uuid.uuid4()), True


class Audit:
    def __init__(self):
        self.events = []

    def audit(self, **kwargs):
        self.events.append(kwargs)


class Queue:
    def __init__(self, fail=False):
        self.fail = fail
        self.jobs = []
        self.events = []
        self.group = False

    async def enqueue(self, contract):
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.jobs.append(contract)

    async def publish_event(self, event):
        self.events.append(event)

    async def ensure_group(self):
        self.group = True


@pytest.mark.asyncio
async def test_scheduler_records_successful_durable_attempt():
    operations = Operations()
    audit = Audit()
    queue = Queue()
    scheduler = Scheduler(
        operations, Catalog(), audit, queue, FakeRedis(), blocking_io=DirectCalls()
    )

    await scheduler.tick()

    assert len(queue.jobs) == 1
    assert operations.completed[0][0:2] == (operations.attempt_id, operations.token)
    assert operations.failed == []
    assert audit.events[0]["action"] == "schedule.trigger"


@pytest.mark.asyncio
async def test_scheduler_retries_enqueue_failure_without_losing_slot():
    operations = Operations()
    scheduler = Scheduler(
        operations, Catalog(), Audit(), Queue(fail=True), FakeRedis(), blocking_io=DirectCalls()
    )

    await scheduler.tick()

    assert operations.completed == []
    assert operations.failed[0][0:2] == (operations.attempt_id, operations.token)
    assert operations.failed[0][2] == "enqueue_failed"


@pytest.mark.asyncio
async def test_scheduler_non_leader_does_not_claim(monkeypatch):
    operations = Operations()
    redis = FakeRedis(leader=False)

    async def lost(*_args):
        return 0

    redis.eval = lost
    scheduler = Scheduler(operations, Catalog(), Audit(), Queue(), redis, blocking_io=DirectCalls())
    monkeypatch.setattr(operations, "claim_due_schedules", lambda *_args, **_kwargs: pytest.fail())

    await scheduler.tick()
