"""Bounded, durable provider attempts. A late HTTP response cannot mutate adopted evidence."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

import httpx

from .providers import ProviderResponseError, is_private_endpoint


# Shared by foreground and fallback queues (and the offline runner) in this process.
# Fallback stages remain sequential; this is a ceiling, not model fan-out.
def worker_count(fallback=False):
    value = int(os.environ.get("FDE_FALLBACK_WORKERS" if fallback else "FDE_PRIMARY_WORKERS", "2"))
    if not 1 <= value <= 8:
        raise ValueError("识别并发数必须在 1 到 8 之间")
    return value


_POOLS = {}
_POOL_LOCK = threading.Lock()
_QUEUE = threading.local()


def request_pool():
    fallback = getattr(_QUEUE, "fallback", False)
    with _POOL_LOCK:
        if fallback not in _POOLS:
            _POOLS[fallback] = ThreadPoolExecutor(max_workers=worker_count(fallback),
                thread_name_prefix="fallback-request" if fallback else "primary-request")
        return _POOLS[fallback]


def set_request_queue(fallback, stop=None):
    _QUEUE.fallback = fallback
    _QUEUE.stop = stop
_LOCK = threading.Lock()
_ACTIVE = set()


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temp.replace(path)


class AttemptRunner:
    def __init__(self, folder, budget, *, transport=None, retry_failed=False, retry_unknown=False):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.deadline = time.monotonic() + max(0, budget)
        self.transport = transport
        self.retry_failed = retry_failed
        self.retry_unknown = retry_unknown
        self.external_calls = 0
        self.calls = []

    def remaining(self):
        return max(0, self.deadline - time.monotonic())

    def call(self, key, stage, profile, invoke, limit=120):
        stop = getattr(_QUEUE, "stop", None)
        if stop is not None and stop.is_set():
            return {"stage": stage, "status": "interrupted", "sent": False, "new_calls": 0}
        cache = self.folder / (key + ".json")
        marker = self.folder / (key + ".attempt.json")
        if cache.exists():
            response = json.loads(cache.read_text())
            return {**response, "stage": stage, "status": "completed", "mode": "resumed_evidence", "new_calls": 0}
        previous = json.loads(marker.read_text()) if marker.exists() else {}
        unknown = previous.get("outcome_unknown") or previous.get("status") in {"running", "unknown"}
        if previous and ((unknown and not self.retry_unknown)
                         or (not unknown and previous.get("status") == "failed" and not self.retry_failed)):
            return {**previous, "stage": stage, "new_calls": 0, "mode": "previous_attempt",
                    "status": "unknown" if unknown else "failed", "outcome_unknown": bool(unknown),
                    "reason": "上次请求结果未知，需明确确认后才能重发" if unknown else previous.get("reason")}
        timeout = min(limit, self.remaining())
        if timeout < 0.05:
            return {"stage": stage, "status": "budget_exhausted", "new_calls": 0}
        active_key = str(marker.resolve())
        with _LOCK:
            if active_key in _ACTIVE:
                return {"stage": stage, "status": "unknown", "outcome_unknown": True,
                        "new_calls": 0, "reason": "同一请求仍在执行；保留证据，不重复发送"}
            _ACTIVE.add(active_key)
        attempt = int(previous.get("attempt", 0)) + 1
        history = self.folder / f"{key}.attempt-{attempt}.json"
        record = {"stage": stage, "attempt": attempt, "status": "queued", "sent": False,
                  "provider": profile["provider"], "requested_model": profile["model_id"],
                  "started_at": time.time(), "outcome_unknown": False}
        gate = threading.Lock()
        cancelled = threading.Event()
        started = time.monotonic()
        dispatched = threading.Event()

        def persist():
            atomic_json(history, record)
            atomic_json(marker, record)

        def request():
            try:
                with gate:
                    if cancelled.is_set():
                        return None
                    remaining = min(limit, self.remaining())
                    if remaining < 0.05:
                        return None
                    record.update(status="running", sent=True, queue_wait_seconds=round(time.monotonic() - started, 3))
                    dispatched.set()
                    persist()
                record["request_started_at"] = time.time()
                return invoke(remaining)
            finally:
                with _LOCK:
                    _ACTIVE.discard(active_key)

        persist()
        future = request_pool().submit(request)
        try:
            if self.deadline == float("inf"):
                while not dispatched.wait(.05):
                    if stop is not None and stop.is_set() and future.cancel():
                        with _LOCK:
                            _ACTIVE.discard(active_key)
                        record.update(status="interrupted", sent=False, new_calls=0,
                                      elapsed_seconds=round(time.monotonic() - started, 3))
                        persist()
                        return dict(record)
                    if future.done():
                        break
            response = future.result(timeout=timeout)
            with gate:
                if response is None:
                    record.update(status="budget_exhausted")
                else:
                    atomic_json(cache, response)
                    record.update(status="completed", response_id=response.get("response_id"),
                                  usage=response.get("usage"), finish_reason=response.get("finish_reason"))
        except FutureTimeout:
            with gate:
                cancelled.set()
                if future.cancel():
                    with _LOCK:
                        _ACTIVE.discard(active_key)
                record.update(status="unknown" if record["sent"] else "budget_exhausted",
                              outcome_unknown=record["sent"], reason="调用等待预算耗尽；已发送请求可能仍计费")
            response = None
        except Exception as error:
            # A read/write/transport failure does not establish whether the server completed/billed.
            ambiguous = isinstance(error, (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError,
                                            httpx.WriteError, httpx.RemoteProtocolError))
            if getattr(error, "task_id", None):
                ambiguous = True
                record["task_id"] = error.task_id
            if getattr(error, "stage", None) == "submit" and "Timeout" in str(getattr(error, "code", "")):
                ambiguous = True
            record.update(status="failed", outcome_unknown=ambiguous, error_type=type(error).__name__,
                          reason=str(error)[:250] if isinstance(error, ValueError) else type(error).__name__)
            if isinstance(error, ProviderResponseError):
                evidence = self.folder / f"{key}.attempt-{attempt}.response.json"
                atomic_json(evidence, error.evidence)
                record["response_evidence"] = evidence.name
            response = None
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        record["new_calls"] = int(record["sent"])
        persist()
        self.external_calls += int(record["sent"] and not self.transport and not is_private_endpoint(profile))
        self.calls.append(dict(record))
        return {**(response or {}), **record}
