"""Тесты чистых хелперов GUI (без Qt)."""

from __future__ import annotations

import pytest

from mchose_hub.gui.helpers import (
    BATTERY_COLOR_LOW,
    BATTERY_COLOR_MID,
    BATTERY_COLOR_OK,
    battery_caption,
    battery_color,
    clamp_brightness,
    clamp_prompt_volume,
    device_speed_to_slider,
    effect_allows_custom_color,
    slider_to_device_speed,
)
from mchose_hub.protocol import BatteryState

# --- slider_to_device_speed: инверсия 5-slider для режимов 1,2,3,5 ---


@pytest.mark.parametrize(
    ("mode", "slider", "device"),
    [
        (1, 1, 4),
        (1, 3, 2),
        (1, 5, 0),
        (2, 1, 4),
        (2, 4, 1),
        (3, 2, 3),
        (3, 5, 0),
        (5, 1, 4),
        (5, 3, 2),
        (5, 5, 0),
    ],
)
def test_slider_to_device_speed_inverted(mode: int, slider: int, device: int) -> None:
    assert slider_to_device_speed(mode, slider) == device


@pytest.mark.parametrize("slider", [1, 3, 5])
def test_slider_to_device_speed_wave_always_one(slider: int) -> None:
    assert slider_to_device_speed(4, slider) == 1


@pytest.mark.parametrize("mode", [0, 6, -1])
def test_slider_to_device_speed_unsupported_mode(mode: int) -> None:
    with pytest.raises(ValueError):
        slider_to_device_speed(mode, 3)


@pytest.mark.parametrize("slider", [0, 6, -1])
def test_slider_to_device_speed_slider_out_of_range(slider: int) -> None:
    with pytest.raises(ValueError):
        slider_to_device_speed(1, slider)


# --- device_speed_to_slider: обратное преобразование ---


@pytest.mark.parametrize(
    ("mode", "device", "slider"),
    [
        (1, 0, 5),
        (1, 2, 3),
        (1, 4, 1),
        (2, 1, 4),
        (3, 3, 2),
        (5, 4, 1),
    ],
)
def test_device_speed_to_slider(mode: int, device: int, slider: int) -> None:
    assert device_speed_to_slider(mode, device) == slider


def test_device_speed_to_slider_wave_always_one() -> None:
    assert device_speed_to_slider(4, 1) == 1


def test_device_speed_to_slider_clamps_garbage_byte() -> None:
    # device 5 → инверсия даёт 0, зажимается в нижнюю границу слайдера
    assert device_speed_to_slider(2, 5) == 1


@pytest.mark.parametrize("mode", [0, 7])
def test_device_speed_to_slider_unsupported_mode(mode: int) -> None:
    with pytest.raises(ValueError):
        device_speed_to_slider(mode, 2)


@pytest.mark.parametrize("device", [-1, 6])
def test_device_speed_to_slider_device_out_of_range(device: int) -> None:
    with pytest.raises(ValueError):
        device_speed_to_slider(1, device)


@pytest.mark.parametrize("mode", [1, 2, 3, 5])
@pytest.mark.parametrize("slider", [1, 2, 3, 4, 5])
def test_speed_roundtrip(mode: int, slider: int) -> None:
    device = slider_to_device_speed(mode, slider)
    assert device_speed_to_slider(mode, device) == slider


# --- clamp_brightness / clamp_prompt_volume ---


@pytest.mark.parametrize(
    ("value", "clamped"),
    [
        (0, 0),
        (4, 4),
        (-5, 0),
        (150, 4),
        (42, 4),
        (3, 3),
        (1, 1),
        (5, 4),  # устройство отсекает >=5 до 4 — clamp повторяет это поведение
    ],
)
def test_clamp_brightness(value: int, clamped: int) -> None:
    assert clamp_brightness(value) == clamped


@pytest.mark.parametrize(
    ("value", "clamped"),
    [(0, 0), (15, 15), (-1, 0), (16, 15), (10, 10), (1, 1), (14, 14)],
)
def test_clamp_prompt_volume(value: int, clamped: int) -> None:
    assert clamp_prompt_volume(value) == clamped


# --- battery_color: пороги 50/20 ---


@pytest.mark.parametrize(
    ("percent", "color"),
    [
        (100, BATTERY_COLOR_OK),
        (87, BATTERY_COLOR_OK),
        (50, BATTERY_COLOR_OK),
        (49, BATTERY_COLOR_MID),
        (20, BATTERY_COLOR_MID),
        (19, BATTERY_COLOR_LOW),
        (1, BATTERY_COLOR_LOW),
        (0, BATTERY_COLOR_LOW),
    ],
)
def test_battery_color(percent: int, color: str) -> None:
    assert battery_color(percent) == color


def test_battery_color_returns_hex() -> None:
    assert battery_color(10).startswith("#")


# --- effect_allows_custom_color: 0,1,2,5 — цвет; 3,4 — нет ---


@pytest.mark.parametrize("mode", [0, 1, 2, 5])
def test_effect_allows_custom_color_true(mode: int) -> None:
    assert effect_allows_custom_color(mode) is True


@pytest.mark.parametrize("mode", [3, 4])
def test_effect_allows_custom_color_false(mode: int) -> None:
    assert effect_allows_custom_color(mode) is False


# --- battery_caption ---


@pytest.mark.parametrize(
    ("state", "caption"),
    [
        (BatteryState(percent=50, charging=False, full=False, asleep=False), "Разряд"),
        (BatteryState(percent=50, charging=True, full=False, asleep=False), "Зарядка"),
        (BatteryState(percent=100, charging=False, full=True, asleep=False), "Полная"),
        (BatteryState(percent=10, charging=False, full=False, asleep=True), "Сон"),
    ],
)
def test_battery_caption(state: BatteryState, caption: str) -> None:
    assert battery_caption(state) == caption
