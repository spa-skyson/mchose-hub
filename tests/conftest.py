"""Общие фикстуры тестов; также добавляет корень репозитория в sys.path."""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mchose_hub.protocol import (
    FEATURE_AA_REPORT_ID,
    build_channel_close,
    build_channel_open,
)


class FakeTransport:
    """In-memory транспорт: ответы выдаются read_report по очереди (FIFO)."""

    path = "/dev/fake"

    def __init__(self) -> None:
        self.written: list[tuple[int, bytes]] = []
        self.reads: list[int] = []
        self.responses: deque[bytes] = deque()
        self.features: list[tuple[int, bytes]] = []
        self.events: list[str] = []
        self.closed = False
        # Точные байты на проводе: bytes([reportId]) + payload — как их видит usbmon.
        self.wire: list[bytes] = []

    def queue_response(self, frame: bytes) -> None:
        self.responses.append(frame)

    def write_report(self, report_id: int, payload: bytes) -> int:
        self.written.append((report_id, payload))
        self.wire.append(bytes([report_id]) + payload)
        self.events.append("write")
        return len(payload) + 1

    def read_report(self, timeout_ms: int) -> bytes | None:
        self.reads.append(timeout_ms)
        self.events.append("read")
        if not self.responses:
            return None
        return self.responses.popleft()

    def send_feature(self, report_id: int, payload: bytes) -> int:
        self.features.append((report_id, payload))
        self.wire.append(bytes([report_id]) + payload)
        return len(payload) + 1

    def get_feature(self, report_id: int, size: int) -> bytes:
        return bytes([report_id]) + bytes(max(size, 1) - 1)

    def unlock_command_channel(self) -> None:
        self.send_feature(FEATURE_AA_REPORT_ID, build_channel_open())
        self.events.append("unlock")

    def lock_command_channel(self) -> None:
        self.send_feature(FEATURE_AA_REPORT_ID, build_channel_close())
        self.events.append("lock")

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_transport() -> FakeTransport:
    return FakeTransport()
