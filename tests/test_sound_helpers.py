"""Тесты чистой логики вкладки «Звук» и авто-восстановления транспорта (без Qt)."""

from __future__ import annotations

import pytest

from mchose_hub.audio.eq import PRESETS
from mchose_hub.gui.helpers import (
    CUSTOM_PRESET,
    EQ_SLIDER_MAX,
    EQ_SLIDER_MIN,
    HEADSET_SILENT_HINT,
    Debounce,
    gain_to_slider,
    headset_silent_hint,
    is_recoverable_transport,
    preset_for_gains,
    recovery_due,
    slider_to_gain,
    with_transport_retry,
)
from mchose_hub.transport import TransportError

# --- маппинг слайдер↔гейн (полушаги 0.5 дБ, диапазон -12..+12) ---


def test_gain_to_slider_half_steps() -> None:
    """0.5 дБ шаг: +6 дБ → 12 позиций; границы точно -24..24."""
    assert gain_to_slider(0.0) == 0
    assert gain_to_slider(6.0) == 12
    assert gain_to_slider(-2.5) == -5
    assert gain_to_slider(-12.0) == EQ_SLIDER_MIN
    assert gain_to_slider(12.0) == EQ_SLIDER_MAX


def test_gain_to_slider_clamps_out_of_range() -> None:
    """Гейн вне -12..+12 зажимается в границы слайдера."""
    assert gain_to_slider(-20.0) == EQ_SLIDER_MIN
    assert gain_to_slider(20.0) == EQ_SLIDER_MAX


def test_slider_to_gain_roundtrip() -> None:
    """Любая позиция слайдера даёт гейн, обратимый в ту же позицию."""
    for slider in range(EQ_SLIDER_MIN, EQ_SLIDER_MAX + 1):
        assert gain_to_slider(slider_to_gain(slider)) == slider


def test_presets_survive_slider_roundtrip() -> None:
    """Все гейны пресетов отражаются на слайдерах без искажения."""
    for gains in PRESETS.values():
        assert [slider_to_gain(gain_to_slider(g)) for g in gains] == [
            float(g) for g in gains
        ]


# --- пресеты: точное совпадение или «Свои» ---


def test_preset_for_gains_matches_exact() -> None:
    """Точное совпадение с пресетом — его имя."""
    for name, gains in PRESETS.items():
        assert preset_for_gains(gains) == name


def test_preset_for_gains_custom_on_deviation() -> None:
    """Любое отклонение (включая 0.5 дБ) — «Свои»."""
    gains = [float(g) for g in PRESETS["Gaming"]]
    gains[3] += 0.5
    assert preset_for_gains(gains) == CUSTOM_PRESET
    assert preset_for_gains([0.0] * 9 + [1.5]) == CUSTOM_PRESET


# --- дебаунс: один отправ после последнего движения ---


def test_debounce_not_due_before_interval() -> None:
    """До истечения 600 мс отправка не готова."""
    d = Debounce()
    assert not d.due(0.0)
    d.touch(10.0)
    assert not d.due(10.3)
    assert not d.take(10.59)


def test_debounce_due_after_interval_and_take_resets() -> None:
    """После 600 мс тишины — одна отправка; следующий burst начинается заново."""
    d = Debounce()
    d.touch(1.0)
    assert d.take(1.6)
    assert not d.due(1.7)  # сброшена: без нового движения отправок нет
    d.touch(2.0)
    assert d.take(2.6)


def test_debounce_rearm_during_burst() -> None:
    """Каждое движение сдвигает момент отправки (слайдинг-дебаунс)."""
    d = Debounce()
    d.touch(0.0)
    d.touch(0.5)
    assert not d.take(0.6)  # прошло 0.1 с после последнего движения
    assert d.take(1.1)  # 0.6 с после последнего движения


# --- распознавание восстанавливаемых транспортных ошибок ---


@pytest.mark.parametrize(
    "message",
    [
        "/dev/hidraw12: транспорт отравлен незавершённой записью",
        "/dev/hidraw12: устройство отключено (EOF)",
        "/dev/hidraw12: устройство отключено (переподключено)",
        (
            "ошибка чтения /dev/hidraw1: [Errno 5] Ошибка ввода/вывода; "
            "донгл будет найден заново"
        ),
    ],
)
def test_is_recoverable_transport_true(message: str) -> None:
    assert is_recoverable_transport(message)


@pytest.mark.parametrize(
    "message",
    [
        "наушники не ответили (ETIMEDOUT)",
        "[Errno 13] Permission denied",
        "",
    ],
)
def test_is_recoverable_transport_false(message: str) -> None:
    assert not is_recoverable_transport(message)


# --- бюджет discover: не чаще одного в секунду ---


def test_recovery_due_first_call_allowed() -> None:
    assert recovery_due(None, 123.0)


def test_recovery_due_within_interval_blocked() -> None:
    assert not recovery_due(100.0, 100.9)
    assert recovery_due(100.0, 101.0)
    assert recovery_due(100.0, 105.0)


# --- одноразовый ретрай команды после recover ---


def test_with_transport_retry_recovers_and_retries() -> None:
    """Отравленный транспорт: recover вызван, команда повторена, результат — как обычно."""
    attempts = 0
    recoveries = 0

    def job() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransportError("транспорт отравлен незавершённой записью")
        return "ok"

    def recover() -> None:
        nonlocal recoveries
        recoveries += 1

    assert with_transport_retry(job, recover)() == "ok"
    assert (attempts, recoveries) == (2, 1)


def test_with_transport_retry_recovers_enodev() -> None:
    """ENODEV (пере-перечисление узла): recover вызван, команда повторена."""
    attempts = 0
    recoveries = 0

    def job() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TransportError("/dev/hidraw0: устройство отключено (переподключено)")
        return "ok"

    def recover() -> None:
        nonlocal recoveries
        recoveries += 1

    assert with_transport_retry(job, recover)() == "ok"
    assert (attempts, recoveries) == (2, 1)


def test_with_transport_retry_single_shot() -> None:
    """Повторная ошибка после recover уходит наружу — ретрай не зацикливается."""

    def job() -> None:
        raise TransportError("устройство отключено (EOF)")

    with pytest.raises(TransportError):
        with_transport_retry(job, lambda: None)()


def test_with_transport_retry_passes_other_errors() -> None:
    """Иные ошибки (не отравление/EOF) — наружу без recover."""

    def job() -> None:
        raise TransportError("наушники не ответили", "подсказка")

    with pytest.raises(TransportError, match="не ответили"):
        with_transport_retry(job, pytest.fail)()  # recover не должен вызываться


# --- подсказка при тишине наушников (NAK-ступени опроса) ---


def test_headset_silent_hint_silent_first_two() -> None:
    """Первая и вторая NAK подряд — тихий текст (None)."""
    assert headset_silent_hint(1) is None
    assert headset_silent_hint(2) is None


def test_headset_silent_hint_from_third() -> None:
    """Третья (и далее) NAK подряд — подсказка проверить наушники."""
    third = headset_silent_hint(3)
    assert third == HEADSET_SILENT_HINT
    assert "включены и рядом с донглом" in third
    assert headset_silent_hint(4) == third
    assert headset_silent_hint(50) == third
