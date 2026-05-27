"""In-memory task store for background operations (upload, fetch, retag)."""

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

MAX_TASKS = 200


@dataclass
class AppTask:
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: str = ""          # "upload" | "fetch" | "retag"
    label: str = ""         # human-readable description
    status: Literal["running", "success", "error"] = "running"
    log: list[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)


_lock = threading.Lock()
_tasks: list[AppTask] = []


def create_task(type_: str, label: str) -> AppTask:
    t = AppTask(type=type_, label=label)
    with _lock:
        _tasks.insert(0, t)
        while len(_tasks) > MAX_TASKS:
            _tasks.pop()
    return t


def get_task(task_id: str) -> AppTask | None:
    with _lock:
        return next((t for t in _tasks if t.id == task_id), None)


def list_tasks(limit: int = 50) -> list[AppTask]:
    with _lock:
        return list(_tasks[:limit])


def any_running() -> bool:
    with _lock:
        return any(t.status == "running" for t in _tasks)


def task_log(task: AppTask, msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        task.log.append(f"[{ts}] {msg}")
        task.updated_at = datetime.now()


def task_done(task: AppTask, success: bool, msg: str = "") -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _lock:
        task.status = "success" if success else "error"
        task.updated_at = datetime.now()
        if msg:
            task.log.append(f"[{ts}] {msg}")


def clear_completed() -> None:
    with _lock:
        done = [t for t in _tasks if t.status != "running"]
        for t in done:
            _tasks.remove(t)
