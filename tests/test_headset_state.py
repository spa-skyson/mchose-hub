"""Давность кэша и очистка состояния наушников при отсутствии донгла.

Qt-free: MainWindow создаётся через __new__ с ручными заглушками атрибутов
(конвенция проекта — без QApplication).
"""

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mchose_hub.gui.app import ManagerSnapshot
from mchose_hub.gui.helpers import staleness_note
from mchose_hub.gui.main_window import MainWindow
from mchose_hub.protocol import AudioMode, BatteryState

_BATTERY = BatteryState(percent=80, charging=False, full=True, asleep=False)


def _stub_window() -> MainWindow:
    """MainWindow без Qt: заглушки вкладок/трея, записи вызовов в events."""
    window = MainWindow.__new__(MainWindow)
    events: list[str] = []
    window.report_error = lambda *a, **k: events.append("report_error")  # type: ignore[method-assign]
    window.clear_error = lambda: events.append("clear_error")  # type: ignore[method-assign]
    window.poll_now = lambda: events.append("poll_now")  # type: ignore[method-assign]
    window._headset_tab = SimpleNamespace(
        set_available=lambda ok: events.append(f"tab:{ok}"),
        apply_battery=lambda battery, note="": events.append(
            f"battery:{battery.percent}{note}"
        ),
        apply_mode=lambda mode: events.append(f"mode:{int(mode)}"),
        mark_mode_unknown=lambda: events.append("mode:unknown"),
        reload_settings=lambda: events.append("reload_settings"),
    )
    window._dock_tab = SimpleNamespace(
        set_available=lambda ok: events.append(f"dock:{ok}"),
        refresh=lambda: None,
    )
    window._device_labels = {
        key: SimpleNamespace(setText=lambda _text: None)
        for key in (
            "Донгл",
            "Док",
            "Док (имя / прошивка)",
            "Наушники (прошивка)",
            "hidraw-устройства",
        )
    }
    window._hint_label = SimpleNamespace(setVisible=lambda _v: None)
    window._tray = SimpleNamespace(
        clear_headset=lambda: events.append("tray:clear"),
        update_battery=lambda _state: events.append("tray:battery"),
        update_mode=lambda _mode: events.append("tray:mode"),
    )
    window._last_confirmed_at = None
    window._nak_streak = 0
    window._settings_loaded = False
    window._pending_refresh = False
    window._battery_queried_at = None
    window._last_recovery_discover = None
    window._pending_snapshot = None
    window._snapshot_seq = 0
    window.note = lambda _message: events.append("note")  # type: ignore[attr-defined]
    window.events = events  # type: ignore[attr-defined]
    return window


# --- staleness_note ---


def test_staleness_note_fresh_cache() -> None:
    """Возраст меньше порога — кэш считаем свежим, без пометки."""
    now = 1000.0
    assert staleness_note(now - 60.0, now, threshold_s=300.0) == ""


def test_staleness_note_stale_minutes() -> None:
    """Кэш старше порога: « · N мин назад» («100% · 7 мин назад»)."""
    now = 1000.0
    assert staleness_note(now - 7 * 60.0, now, threshold_s=300.0) == " · 7 мин назад"
    assert staleness_note(now - 5 * 60.0, now, threshold_s=300.0) == " · 5 мин назад"


def test_staleness_note_stale_under_minute() -> None:
    """Целые минуты всегда: 301 с при пороге 300 с — «5 мин назад»."""
    now = 1000.0
    assert staleness_note(now - 301.0, now, threshold_s=300.0) == " · 5 мин назад"


def test_staleness_note_no_confirmation() -> None:
    """Подтверждений не было — пометка не рисуется (показывать нечего)."""
    assert staleness_note(None, now=1000.0, threshold_s=300.0) == ""


# --- _clear_headset_state ---


def test_clear_headset_state_resets_counters_and_widgets() -> None:
    """Сброс: пометка давности, NAK-серия, вкладка и трей."""
    window = _stub_window()
    window._last_confirmed_at = time.monotonic()
    window._nak_streak = 2
    window._clear_headset_state()
    assert window._last_confirmed_at is None
    assert window._nak_streak == 0
    assert window.events == ["tab:False", "tray:clear"]


def test_clear_headset_state_without_tray() -> None:
    """Трей отсутствует (не создался) — очистка не падает."""
    window = _stub_window()
    window._tray = None
    window._last_confirmed_at = time.monotonic()
    window._clear_headset_state()
    assert window._last_confirmed_at is None
    assert window.events == ["tab:False"]


# --- отключение после успешного опроса очищает состояние ---


def test_disconnect_after_successful_poll_clears_state() -> None:
    """Успешный опрос подтвердил канал; снимок без донгла очищает показ."""
    window = _stub_window()
    # 1) успешный опрос: подтверждённый кадр
    window._last_confirmed_at = time.monotonic()
    # 2) устройство отключили: discover вернулся без донгла
    window._apply_snapshot(ManagerSnapshot())
    assert window._last_confirmed_at is None
    assert window._nak_streak == 0
    assert window.events == [
        "clear_error",
        "tab:False",
        "tray:clear",
        "dock:False",
        "poll_now",
    ]


# --- _poll_headset: подтверждённость канала за тик ---


class _FakeService:
    """Минимальный сервис: pump отдаёт заданное число пушей, кэш непуст."""

    def __init__(self, pushes: int = 0, fresh_answer: bool = False) -> None:
        self._pushes = pushes
        self._fresh = fresh_answer
        self._frames = 0

    def pump_passive(self, timeout_ms: int = 10) -> int:
        self._frames += self._pushes
        return self._pushes

    def frames_received(self) -> int:
        return self._frames

    def poll_battery(self) -> BatteryState:
        if self._fresh:
            self._frames += 1  # свежий ответ разобран (_ingest реального сервиса)
        return _BATTERY

    def poll_audio_mode(self) -> AudioMode:
        if self._fresh:
            self._frames += 1
        return AudioMode.MUSIC

    def cached_battery(self) -> BatteryState:
        return _BATTERY

    def cached_audio_mode(self) -> AudioMode:
        return AudioMode.MUSIC


class _FakeManager:
    def __init__(
        self,
        service: _FakeService | None = None,
        discovered: list[_FakeService | None] | None = None,
    ) -> None:
        self._service = service
        self._discovered = iter(discovered or [])
        self.discovers = 0

    def headset_service(self) -> _FakeService | None:
        return self._service

    def discover(self) -> ManagerSnapshot:
        """Фейковый пере-скан: находит донгл, если он заявлен в discovered."""
        self.discovers += 1
        service = next(self._discovered, None)
        if service is not None:
            self._service = service
            return ManagerSnapshot(dongle_path="/dev/hidraw8")
        return ManagerSnapshot()

    def command_session(self, device: str):
        @contextmanager
        def cm():
            yield self._service

        return cm()


def _window_for_poll(service: _FakeService | None) -> MainWindow:
    window = _stub_window()
    window._manager = _FakeManager(service)  # type: ignore[attr-defined]
    window._pending_refresh = False
    window._battery_queried_at = time.monotonic()  # бюджет не наступил: кэш
    return window


def test_poll_headset_confirmed_by_push() -> None:
    """Пуш-кадр за тик: confirmed=True."""
    window = _window_for_poll(_FakeService(pushes=2))
    battery, mode, confirmed = window._poll_headset()
    assert confirmed is True
    assert battery == _BATTERY
    assert mode == AudioMode.MUSIC


def test_poll_headset_not_confirmed_without_frames() -> None:
    """Тишина (пушей нет, явных запросов нет): confirmed=False."""
    window = _window_for_poll(_FakeService(pushes=0))
    battery, mode, confirmed = window._poll_headset()
    assert confirmed is False
    assert battery == _BATTERY  # кэш показан, но не подтверждён
    assert mode == AudioMode.MUSIC


def test_poll_headset_without_dongle() -> None:
    """Донгла нет: (None, None, False)."""
    window = _window_for_poll(None)
    assert window._poll_headset() == (None, None, False)


# --- confirmed по свежему явному ответу (находка ревью) ---


def test_poll_headset_confirmed_by_fresh_answer() -> None:
    """Пушей нет, но явный запрос получил ответ: confirmed=True.

    Счётчик кадров сравнивается после ВСЕГО тика: свежий явный ответ
    снимает пометку давности этим же тиком.
    """
    window = _window_for_poll(_FakeService(pushes=0, fresh_answer=True))
    window._pending_refresh = True  # ветка явных запросов батареи+режима
    battery, mode, confirmed = window._poll_headset()
    assert confirmed is True
    assert battery == _BATTERY
    assert mode == AudioMode.MUSIC


def test_poll_headset_cache_branch_not_confirmed_without_frames() -> None:
    """Кэш-ветка без пушей и без явных запросов: confirmed=False."""
    window = _window_for_poll(_FakeService(pushes=0, fresh_answer=False))
    _, _, confirmed = window._poll_headset()
    assert confirmed is False


# --- последовательность: успех -> EIO -> перескан без узла -> новый узел ---


def test_retry_without_dongle_clears_then_new_node_refreshes() -> None:
    """EIO без нового узла очищает показ; появление узла даёт полное обновление."""
    window = _stub_window()
    service = _FakeService(pushes=1)
    window._manager = _FakeManager(service)  # type: ignore[attr-defined]

    # 1) Успешный опрос: канал подтверждён пушем.
    window._poll_headset()
    window._apply_poll_result((service.cached_battery(), None, True))
    assert window._last_confirmed_at is not None
    assert window._nak_streak == 0

    # 2) EIO: ретрай пере-сканировал, но нового узла ещё нет.
    window._on_poll_failure(
        "ошибка чтения /dev/hidraw1: [Errno 5] Ошибка ввода/вывода; "
        "донгл будет найден заново",
        "устройство пере-перечислилось: приложение выполнит пере-скан hidraw "
        "и подключится автоматически",
    )
    assert window._last_confirmed_at is None  # очищено при failure
    window._manager._service = None  # перескан не нашёл узел
    window._apply_poll_result((None, None, False))
    assert window._last_confirmed_at is None  # повторно очищено при успехе-пустом
    assert window._nak_streak == 0  # NAK-серия не копится: донгла просто нет
    assert "tab:False" in window.events

    # 3) Донгл появился на новом узле: полное обновление.
    new_service = _FakeService(pushes=0, fresh_answer=True)
    window._manager._service = new_service
    window._apply_snapshot(ManagerSnapshot(dongle_path="/dev/hidraw8"))
    assert window._pending_refresh is True
    battery, _mode, confirmed = window._poll_headset()
    assert window._pending_refresh is False  # полное обновление выполнено
    window._apply_poll_result((battery, _mode, confirmed))
    assert window._last_confirmed_at is not None  # свежий ответ подтвердил канал
    assert window._nak_streak == 0
    assert "tab:True" in window.events


# --- пере-скан из тика без донгла: подхват нового узла без ручных действий ---


def test_rescan_on_tick_picks_up_dongle_without_manual_action() -> None:
    """Тик без донгла пересканирует; донгл из фейкового discover подхватывается.

    Первое пересканирование пустое; донгл появляется лишь ко второму тику
    (через фейковый discover менеджера, не через прямой _apply_snapshot) —
    и подхватывается: снимок применён, полное обновление выполнено.
    """
    window = _stub_window()
    manager = _FakeManager()
    window._manager = manager  # type: ignore[attr-defined]

    # Тик 1: донгла нет → воркер сделал discover (пусто), снимок применён.
    window._apply_poll_result(window._poll_headset())
    assert manager.discovers == 1
    assert manager.headset_service() is None
    assert window._last_confirmed_at is None  # состояние очищено
    assert "tab:False" in window.events

    # Реальный тик — 15 с: бюджет recovery_due (1 с) давно истёк.
    assert window._last_recovery_discover is not None
    window._last_recovery_discover -= 16.0

    # Тик 2: фейковый discover находит донгл на новом узле.
    fresh = _FakeService(pushes=0, fresh_answer=True)
    manager._discovered = iter([fresh])
    window._apply_poll_result(window._poll_headset())
    assert manager.discovers == 2
    assert manager.headset_service() is fresh
    assert "tab:True" in window.events  # снимок применён автоматически
    assert window._pending_refresh is True  # полное обновление запланировано

    # Подхват без ручных действий: следующий тик (в production его
    # планирует _apply_snapshot через poll_now) делает полное обновление.
    battery, _mode, confirmed = window._poll_headset()
    assert window._pending_refresh is False
    window._apply_poll_result((battery, _mode, confirmed))
    assert "battery:80" in window.events
    assert window._last_confirmed_at is not None
    assert window._nak_streak == 0


# --- снимок тика старше снимка reconnect: не перебивает свежее состояние ---


def test_stale_tick_snapshot_discarded_after_reconnect() -> None:
    """Пустой снимок тика не перебивает применённый reconnect'ом снимок с донглом.

    Порядок: тик получил пустой снимок → reconnect применил снимок с донглом
    → пришёл колбэк тика → донгл остаётся доступным.
    """
    window = _stub_window()
    manager = _FakeManager(service=None)
    window._manager = manager  # type: ignore[attr-defined]

    # Воркер тика: сервис нет, поколение запомнено, скан вернул пустоту.
    assert window._poll_headset() == (None, None, False)
    assert window._pending_snapshot is not None
    seq, snapshot = window._pending_snapshot
    assert seq == 0
    assert snapshot.dongle_path is None

    # Параллельный reconnect нашёл донгл и применил свой снимок: поколение +1.
    fresh = _FakeService(pushes=0, fresh_answer=True)
    manager._service = fresh
    window._apply_snapshot(ManagerSnapshot(dongle_path="/dev/hidraw8"))
    assert window._snapshot_seq == seq + 1
    assert "tab:True" in window.events  # донгл показан доступным

    # Колбэк тика: поколение тика устарело — снимок и результат отброшены.
    window._apply_poll_result((None, None, False))
    assert "tab:False" not in window.events  # донгл НЕ помечен недоступным
    assert window.events.count("tab:True") == 1  # состояние reconnect сохранено
    assert window._last_confirmed_at is None  # тик ничего не подтвердил
