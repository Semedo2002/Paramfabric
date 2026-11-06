import threading
import time
from collections import defaultdict

import pytest

from paramfabric.server import tasks
from paramfabric.server.tasks import LocalTaskQueue, RedisTaskQueue, TaskCancelled, handler


class FakeRedis:
    """The subset of redis-py used by RedisStore/RedisTaskQueue (thread-safe)"""

    def __init__(self) -> None:
        self.hashes: dict[str, dict[str, str]] = defaultdict(dict)
        self.lists: dict[str, list[str]] = defaultdict(list)
        self.sets: dict[str, set[str]] = defaultdict(set)
        self.lock = threading.Condition()

    def hset(self, key, field, value):
        with self.lock:
            self.hashes[key][field] = value

    def hget(self, key, field):
        return self.hashes[key].get(field)

    def hgetall(self, key):
        return dict(self.hashes[key])

    def rpush(self, key, value):
        with self.lock:
            self.lists[key].append(value)
            self.lock.notify_all()

    def lpush(self, key, value):
        with self.lock:
            self.lists[key].insert(0, value)
            self.lock.notify_all()

    def ltrim(self, key, start, end):
        with self.lock:
            items = self.lists[key]
            self.lists[key] = items[start:] if end == -1 else items[start : end + 1]

    def lrange(self, key, start, end):
        items = self.lists[key]
        return items[start:] if end == -1 else items[start : end + 1]

    def llen(self, key):
        return len(self.lists[key])

    def sadd(self, key, value):
        self.sets[key].add(value)

    def sismember(self, key, value):
        return value in self.sets[key]

    def brpop(self, key, timeout=0):
        deadline=time.monotonic() + timeout
        with self.lock:
            while not self.lists[key]:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.lock.wait(remaining)
            return key, self.lists[key].pop()


@handler("test.count")
def _count(payload, context):
    for i in range(payload["steps"]):
        context.progress(i, {"residual": 10.0 ** -i})
        context.log(f"step {i}")
        if payload.get("slow"):
            time.sleep(0.05)
    path = context.artifact("result.txt")
    path.write_text(str(payload["steps"]))
    return {"steps": payload["steps"]}


@handler("test.fail")
def _fail(payload, context):
    raise ValueError("boom")


def _wait(queue, task_id, timeout=10.0):
    deadline=time.monotonic() + timeout
    while queue.get(task_id).status not in tasks.TERMINAL and time.monotonic() < deadline:
        time.sleep(0.01)
    return queue.snapshot(task_id)


def test_local_queue_streams_progress_logs_and_artifacts(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    record = queue.submit("test.count", {"steps": 5}, "alice")
    snapshot=_wait(queue, record.id)
    assert snapshot["status"] == "completed" and snapshot["result"] == {"steps": 5}
    assert [entry["iteration"] for entry in snapshot["progress"]] == [0, 1, 2, 3, 4]
    assert "step 4" in snapshot["log"]
    later = queue.snapshot(record.id, progress_offset=3, log_offset=snapshot["log_count"])
    assert [entry["iteration"] for entry in later["progress"]] == [3, 4] and later["log"] == []
    assert queue.artifact_path(record.id, "result.txt").read_text() == "5"
    with pytest.raises(KeyError):
        queue.artifact_path(record.id, "../../secrets.json")


def test_local_queue_failure_cancellation_and_validation(tmp_path):
    queue = LocalTaskQueue(tmp_path)
    failed = _wait(queue, queue.submit("test.fail", {}, "bob").id)
    assert failed["status"] == "failed" and failed["error"] == "boom"
    running = queue.submit("test.count", {"steps": 200, "slow": True}, "bob")
    time.sleep(0.15)
    queue.cancel(running.id)
    assert _wait(queue, running.id)["status"] == "cancelled"
    with pytest.raises(ValueError, match="unknown task kind"):
        queue.submit("does.not.exist", {}, "bob")
    with pytest.raises(TypeError):
        queue.submit("test.count", {"steps": object()}, "bob")


def test_redis_queue_runs_on_a_separate_worker(tmp_path):
    client = FakeRedis()
    api_side = RedisTaskQueue(client, tmp_path)
    worker_side=RedisTaskQueue(client, tmp_path)
    stop = threading.Event()
    worker=threading.Thread(target=worker_side.work, args=("node-7",), kwargs={"timeout": 1, "stop": stop}, daemon=True)
    worker.start()
    try:
        record = api_side.submit("test.count", {"steps": 3}, "carol")
        snapshot = _wait(api_side, record.id)
        assert snapshot["status"] == "completed" and snapshot["worker"] == "node-7"
        assert snapshot["backend"] == "redis"
        assert [entry["fields"]["residual"] for entry in snapshot["progress"]] == [1.0, 0.1, 0.01]
        assert api_side.artifact_path(record.id, "result.txt").read_text() == "3"
        assert [r.id for r in api_side.list("carol")] == [record.id]
    finally:
        stop.set()
        worker.join(timeout=5)


def test_cancel_before_pickup_is_honoured(tmp_path):
    client = FakeRedis()
    queue = RedisTaskQueue(client, tmp_path)
    record = queue.submit("test.count", {"steps": 3}, "dave")
    queue.cancel(record.id)
    assert queue.work("w", once=True, timeout=1) == 1
    assert queue.get(record.id).status == "cancelled"


def test_context_check_raises_on_cancel(tmp_path):
    store = tasks.MemoryStore()
    record = tasks.TaskRecord(id="t1", kind="test.count", owner="x", payload={})
    store.save(record)
    context = tasks.TaskContext(store, record, tmp_path / "t1")
    store.request_cancel("t1")
    context._last_cancel_check = 0.0
    with pytest.raises(TaskCancelled):
        context.check()
