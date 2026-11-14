"""Тесты discovery-слоя (скан hidraw в sysfs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from mchose_hub import transport
from mchose_hub.protocol import PID_HEADSET_DONGLE, VID_MCHOSE


def _make_uevent(root: Path, name: str, hid_id: str | None) -> None:
    device_dir = root / "hidraw" / name / "device"
    device_dir.mkdir(parents=True)
    lines = ["DRIVER=hid-generic", "HID_NAME=MCHOSE Device"]
    if hid_id is not None:
        lines.append(f"HID_ID={hid_id}")
    (device_dir / "uevent").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_scan_hidraw_parses_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "_HIDRAW_SYSFS", str(tmp_path / "hidraw"))
    _make_uevent(tmp_path, "hidraw0", "0003:00003837:0000600A")
    _make_uevent(tmp_path, "hidraw1", "0003:00001552:0000A000")
    _make_uevent(tmp_path, "hidraw2", None)
    devices = transport.scan_hidraw()
    assert devices == [
        ("/dev/hidraw0", VID_MCHOSE, PID_HEADSET_DONGLE),
        ("/dev/hidraw1", 0x1552, 0xA000),
    ]


def test_scan_hidraw_missing_sysfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "_HIDRAW_SYSFS", str(tmp_path / "absent"))
    assert transport.scan_hidraw() == []


def test_parse_hid_id() -> None:
    assert transport._parse_hid_id("0003:00003837:0000600A") == (0x3837, 0x600A)
    assert transport._parse_hid_id("0005:1:2") == (1, 2)
    assert transport._parse_hid_id("0003:zz:2") is None
    assert transport._parse_hid_id("0003:1") is None


def test_find_devices_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transport, "_HIDRAW_SYSFS", str(tmp_path / "hidraw"))
    assert transport.find_dongle() is None
    assert transport.find_dock() is None


def test_find_dongle_unopenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(transport, "_HIDRAW_SYSFS", str(tmp_path / "hidraw"))
    _make_uevent(tmp_path, "hidraw9", "0003:00003837:0000600A")
    with pytest.raises(transport.TransportError):
        transport.find_dongle()
