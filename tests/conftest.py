"""Общие фикстуры тестов; также добавляет корень репозитория в sys.path."""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class FakeTransport:
    """In-memory транспорт: ответы выдаются read_report по очереди (FIFO)."""

    def __init__(self) -> None:
        self.written: list[tuple[int, bytes]] = []
        self.reads: list[int] = []
        self.responses: deque[bytes] = deque()
        self.closed = False

    def queue_response(self, frame: bytes) -> None:
        self.responses.append(frame)

    def write_report(self, report_id: int, payload: bytes) -> int:
        self.written.append((report_id, payload))
        return len(payload) + 1

    def read_report(self, timeout_ms: int) -> bytes | None:
        self.reads.append(timeout_ms)
        if not self.responses:
            return None
        return self.responses.popleft()

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()
