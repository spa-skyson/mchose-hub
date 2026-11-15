"""Главное окно: вкладки, плашка ошибок, опросный цикл, вкладка «Устройство»."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..protocol import AudioMode, BatteryState
from ..transport import ETIMEDOUT_HEADSET_HINT, UDEV_HINT
from .app import ManagerSnapshot
from .dock_tab import DockTab
from .headset_tab import HeadsetTab
from .helpers import (
    headset_silent_hint,
    is_recoverable_transport,
    recovery_due,
    staleness_note,
    with_transport_retry,
)
from .sound_tab import SoundTab
from .tray import MchoseTray
from .workers import WorkerHost

if TYPE_CHECKING:
    from PyQt6.QtGui import QCloseEvent

# Тик цикла опроса: пассивный приём пуш-кадров донгла и показ кэша —
# без записи в устройство. Явные команды уходят только после (пере)подключения
# и по бюджету батареи (эталон M HUB: периодического опроса нет, ~10 команд
# за 3 часа, все — действия пользователя).
_POLL_INTERVAL_MS = 15_000
# Явный запрос батареи — не чаще раза в 5 минут (захват M HUB: батарея
# запрошена один раз через 54 с после старта, дальше 3 часа — сохранённое
# значение; актуализацию делает пассивный приём push-кадров).
_BATTERY_REFRESH_INTERVAL_S = 5 * 60
_WINDOW_TITLE = "MCHOSE Hub — V9 Turbo+"
_TAB_HEADSET = "Наушники"
_TAB_SOUND = "Звук"
_TAB_DOCK = "Подсветка дока"
_TAB_DEVICE = "Устройство"
_ERROR_HINT_PREFIX = "Подсказка: "
_POLL_NO_ANSWER = "Наушники не ответили — повторим при следующем опросе."
_POLL_BATTERY_SILENT = "Батарея не ответила — повторим при следующем опросе."
_POLL_MODE_SILENT = "Режим звука не ответил — повторим при следующем опросе."
_HIDDEN_NOTE = "Окно скрыто в трей. Выход — через меню трея."
_DONGLE_ROW = "Донгл"
_DOCK_ROW = "Док"
_DOCK_INFO_ROW = "Док (имя / прошивка)"
_HEADSET_INFO_ROW = "Наушники (прошивка)"
_FOUND = "подключён"
_NOT_FOUND = "не найден"
_HIDRAW_ROW = "hidraw-устройства"
_RECONNECT_BUTTON = "Переподключить"
_DOCK_RESET_BUTTON = "Сброс дока"
_DOCK_RESET_CONFIRM = "Сброс вернёт подсветку дока к заводским значениям. Продолжить?"
_DOCK_RESET_DONE = "Сброс дока выполнен."
_DOCK_FW_NO_DATA = "нет данных"
_HEADSET_FW_NOTE = "нет данных (протокол не предоставляет версию)"
_CLOSE_TO_TRAY = "Сворачивать в трей при закрытии окна"
_SHUTDOWN_STUCK = "Фоновая операция не завершилась — повторите выход."
_HINT_TITLE = (
    "Устройства не найдены или доступ запрещён. Установите правила udev:\n"
    "скопируйте packaging/99-mchose-hub.rules в /etc/udev/rules.d/, затем:\n"
)
_SETTINGS_GEOMETRY = "geometry"
_SETTINGS_CLOSE_TO_TRAY = "close_to_tray"


class MainWindow(QMainWindow):
    """Окно-хаб: держит DeviceManager, воркеров, таймер опроса и трей."""

    def __init__(self, autostart: bool = True) -> None:
        super().__init__()
        from .app import DeviceManager  # локально: разрываем цикл импортов

        self._manager = DeviceManager()
        self._workers = WorkerHost()
        self._settings = QSettings("mchose-hub", "mchose-hub")
        self._poll_active = False
        self._settings_loaded = False
        self._tray: MchoseTray | None = None
        self._reconnect_generation = 0
        self._shutdown_done = False
        self._last_recovery_discover: float | None = None
        self._nak_streak = 0
        # Бюджет явных запросов: полное обновление после (пере)подключения,
        # батарея — не чаще _BATTERY_REFRESH_INTERVAL_S. Флаги из воркеров и
        # GUI-потока: присваивание атомарно, гонка даёт лишний/пропущенный
        # один запрос до следующего тика — безвредно.
        self._pending_refresh = True
        self._battery_queried_at: float | None = None
        # Время последнего подтверждённого кадра (пуш или свежий ответ):
        # отделяет живой канал от показа кэша. None — подтверждений не было.
        self._last_confirmed_at: float | None = None
        # Снимок discover() из тика без донгла (воркер) к применению в
        # GUI-потоке: пара (поколение снимков на момент скана, снимок).
        # Пока воркер сканировал, reconnect мог применить свой снимок —
        # устаревший снимок тика при применении отбрасывается.
        self._pending_snapshot: tuple[int, ManagerSnapshot] | None = None
        # Поколение применённых снимков: растёт на каждый _apply_snapshot.
        self._snapshot_seq = 0

        self.setWindowTitle(_WINDOW_TITLE)
        self.resize(760, 560)
        self._build_ui()
        self._restore_geometry()

        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(_POLL_INTERVAL_MS)
        self._poll_timer.timeout.connect(self.poll_now)

        app: QApplication | None = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.shutdown)

        if autostart:
            self.reconnect()
            self._poll_timer.start()

    # --- построение интерфейса ---

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)
        self._banner = QLabel(root)
        self._banner.setObjectName("error_banner")
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        self._banner.setStyleSheet(
            "QLabel#error_banner { background: #fdecea; color: #b71c1c; "
            "border: 1px solid #e53935; border-radius: 4px; padding: 6px; }"
        )
        layout.addWidget(self._banner)

        self.tabs = QTabWidget(root)
        self._headset_tab = HeadsetTab(self._manager, self._workers, self)
        self._sound_tab = SoundTab(self._workers, self)
        self._dock_tab = DockTab(self._manager, self._workers, self)
        self._device_tab = self._build_device_tab(self.tabs)
        self.tabs.addTab(self._headset_tab, _TAB_HEADSET)
        self.tabs.addTab(self._sound_tab, _TAB_SOUND)
        self.tabs.addTab(self._dock_tab, _TAB_DOCK)
        self.tabs.addTab(self._device_tab, _TAB_DEVICE)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        layout.addWidget(self.tabs, 1)
        self.setCentralWidget(root)

        status = self.statusBar()
        if status is None:  # pragma: no cover — statusBar() всегда создаёт панель
            raise RuntimeError("не удалось создать строку состояния")
        self._status = status

        # Трей создаётся безусловно: на KDE Plasma + Wayland (XWayland-приложение)
        # isSystemTrayAvailable() ложно сообщает об отсутствии лотка, хотя SNI-
        # иконки других приложений в нём отображаются (проверено на live).
        # На системах без лотка иконка не появится — тихая деградация без падения.
        try:
            tray = MchoseTray(
                self,
                set_mode=self._headset_tab.request_audio_mode,
                set_shutdown=self._headset_tab.request_shutdown,
            )
            tray.show()
            self._tray = tray
        except Exception:  # noqa: BLE001 — лоток не критичен для работы окна
            self._tray = None

    def _build_device_tab(self, parent: QWidget) -> QWidget:
        widget = QWidget(parent)
        layout = QVBoxLayout(widget)
        form = QFormLayout()
        self._device_labels: dict[str, QLabel] = {}
        rows = (
            (_DONGLE_ROW, _NOT_FOUND),
            (_DOCK_ROW, _NOT_FOUND),
            (_DOCK_INFO_ROW, _DOCK_FW_NO_DATA),
            (_HEADSET_INFO_ROW, _HEADSET_FW_NOTE),
            (_HIDRAW_ROW, "—"),
        )
        for title, initial in rows:
            label = QLabel(initial, widget)
            label.setWordWrap(True)
            form.addRow(QLabel(f"<b>{title}:</b>", widget), label)
            self._device_labels[title] = label
        layout.addLayout(form)

        self._hint_label = QLabel(_HINT_TITLE + UDEV_HINT, widget)
        self._hint_label.setWordWrap(True)
        self._hint_label.setStyleSheet(
            "QLabel { background: #fff8e1; color: #795548; "
            "border: 1px solid #ffb300; border-radius: 4px; padding: 6px; }"
        )
        self._hint_label.setVisible(False)
        layout.addWidget(self._hint_label)

        buttons = QHBoxLayout()
        reconnect = QPushButton(_RECONNECT_BUTTON, widget)
        reconnect.clicked.connect(lambda _: self.reconnect())
        dock_reset = QPushButton(_DOCK_RESET_BUTTON, widget)
        dock_reset.clicked.connect(self._on_dock_reset)
        buttons.addWidget(reconnect)
        buttons.addWidget(dock_reset)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        self._close_to_tray_box = QCheckBox(_CLOSE_TO_TRAY, widget)
        self._close_to_tray_box.setChecked(self._close_to_tray_enabled())
        self._close_to_tray_box.toggled.connect(self._on_close_to_tray_toggled)
        layout.addWidget(self._close_to_tray_box)
        layout.addStretch(1)
        return widget

    # --- сообщения GUI-потоку от вкладок/воркеров ---

    def report_error(self, message: str, hint: str = "") -> None:
        """Красная плашка: TransportError с message + hint.

        Для ETIMEDOUT подсказка про 2.4G-линк добавляется, только если
        транспорт её ещё не приложил.
        """
        if (
            "ETIMEDOUT" in message
            and ETIMEDOUT_HEADSET_HINT not in message
            and ETIMEDOUT_HEADSET_HINT not in hint
        ):
            hint = (
                f"{hint}; {ETIMEDOUT_HEADSET_HINT}" if hint else ETIMEDOUT_HEADSET_HINT
            )
        text = message if not hint else f"{message}\n{_ERROR_HINT_PREFIX}{hint}"
        self._banner.setText(text)
        self._banner.setVisible(True)
        self._status.showMessage(message, 5000)

    def clear_error(self) -> None:
        self._banner.setVisible(False)

    def note(self, message: str) -> None:
        """Тихое уведомление в статусбар (таймауты устройств, без диалогов)."""
        self._status.showMessage(message, 5000)

    def poll_now(self) -> None:
        """Тик цикла: пассивный приём + явные запросы по бюджету.

        Таймер не останавливается: зависший воркер не блокирует цикл
        навсегда, следующий тик повторит попытку, а таймаут записи на
        транспорте делает воркеры конечными. Отравленный транспорт,
        EOF/ENODEV/EIO лечатся на месте: пере-скан и одноразовый ретрай
        (recover_transport). Запись в донгл — только по бюджету: полное
        обновление после (пере)подключения, батарея не чаще
        _BATTERY_REFRESH_INTERVAL_S; между ними GUI показывает кэш,
        обновлённый пассивным приёмом.
        """
        if self._poll_active:
            return
        self._poll_active = True
        if not self._poll_timer.isActive():
            self._poll_timer.start()

        self._workers.run(
            with_transport_retry(self._poll_headset, self.recover_transport),
            self._apply_poll_result,
            self._on_poll_failure,
            device="headset-poll",
        )

    def _apply_poll_result(
        self, result: tuple[BatteryState | None, AudioMode | None, bool]
    ) -> None:
        """Показ результата тика опроса; при подтверждении — сброс давности."""
        self._poll_active = False
        pending = self._pending_snapshot
        self._pending_snapshot = None
        if pending is not None:
            seq, snapshot = pending
            if seq == self._snapshot_seq:
                # Снимок тика свежий: применяем (находка донгла планирует
                # полное обновление через poll_now), результат неактуален.
                self._apply_snapshot(snapshot)
                return
            # Пока воркер сканировал, reconnect применил более свежий
            # снимок (поколение ушло вперёд): снимок тика и его результат
            # отбрасываем — состояние UI не трогаем.
            return
        battery, mode, confirmed = result
        now = time.monotonic()
        if confirmed:
            # Кадр получен за тик (пуш или свежий ответ): канал жив.
            self._last_confirmed_at = now
        note = staleness_note(self._last_confirmed_at, now, _BATTERY_REFRESH_INTERVAL_S)
        if battery is None and mode is None:
            if self._manager.headset_service() is None:
                # Ретрай после восстанавливаемой ошибки закончился без донгла
                # (новый узел ещё не появился): показанное состояние ничем
                # не подтверждено — очистить до появления нового узла.
                self._clear_headset_state()
                return
            # Канал не ответил: пара первых NAK — тишина, с третьего
            # подряд — подсказка проверить сами наушники.
            self._nak_streak += 1
            hint = headset_silent_hint(self._nak_streak)
            self.note(hint if hint is not None else _POLL_NO_ANSWER)
            return
        self._nak_streak = 0  # любой полученный ответ — канал жив
        if battery is not None:
            self._headset_tab.apply_battery(battery, note=note)
            if self._tray is not None:
                self._tray.update_battery(battery)
            self.clear_error()
            if not self._settings_loaded:
                self._settings_loaded = True
                self._headset_tab.reload_settings()
        else:
            self.note(_POLL_BATTERY_SILENT)
        if mode is not None:
            self._headset_tab.apply_mode(mode)
            if self._tray is not None:
                self._tray.update_mode(mode)
        else:
            self._headset_tab.mark_mode_unknown()
            if battery is not None:
                self.note(_POLL_MODE_SILENT)

    def _on_poll_failure(self, message: str, hint: str) -> None:
        """Ошибка тика; восстановимая — очистить показ до пере-скана."""
        self._poll_active = False
        if is_recoverable_transport(message):
            # Транспорт умер (отравлен/EOF/ENODEV/EIO): показанное
            # состояние больше ничем не подтверждено — очистим до
            # пере-скана (на следующем тике или после ретрая).
            self._clear_headset_state()
        self.report_error(message, hint)

    def _poll_headset(self) -> tuple[BatteryState | None, AudioMode | None, bool]:
        """Один тик опроса в command_session (ретрай открывает новую).

        Пассивный приём — каждый тик (только чтение). Явные команды: после
        (пере)подключения — батарея и режим; дальше батарея не чаще
        _BATTERY_REFRESH_INTERVAL_S (режим приходит пуш-кадром). Иначе —
        значения из кэша. Третий элемент — подтверждён ли канал за тик:
        счётчик принятых кадров вырос за ВЕСЬ тик (пуш или свежий ответ на
        явный запрос — тогда пометка давности снимается этим же тиком).
        """
        with self._manager.command_session("headset") as service:
            if service is None:
                # Донгла нет: повторный пере-скан прямо в воркере (не в GUI),
                # с существующим бюджетом частоты discovery (recovery_due,
                # общий с recover_transport) — не чаще раза в секунду и
                # раза за тик (тип тика 15 с). Снимок применит GUI-поток.
                # Поколение запоминаем ДО скана: если пока мы сканировали
                # reconnect применил свой снимок, наш устареет.
                now = time.monotonic()
                if recovery_due(self._last_recovery_discover, now):
                    self._last_recovery_discover = now
                    seq = self._snapshot_seq
                    self._pending_snapshot = (seq, self._manager.discover())
                return None, None, False
            frames_before = service.frames_received()
            service.pump_passive()
            now = time.monotonic()
            if self._pending_refresh:
                self._pending_refresh = False
                self._battery_queried_at = now
                # Раздельные запросы: батарея видна, даже если режим молчит.
                battery, mode = service.poll_battery(), service.poll_audio_mode()
            elif (
                self._battery_queried_at is None
                or now - self._battery_queried_at >= _BATTERY_REFRESH_INTERVAL_S
            ):
                self._battery_queried_at = now
                battery = service.poll_battery()
                mode = service.cached_audio_mode()
            else:
                battery = service.cached_battery()
                mode = service.cached_audio_mode()
            confirmed = service.frames_received() > frames_before
            return battery, mode, confirmed

    def _clear_headset_state(self) -> None:
        """Донгла нет (не найден или транспорт умер): очистить показ состояния.

        Кэш без живого канала не подтверждает присутствие наушников:
        сбрасываем пометку давности, счётчик NAK, виджеты вкладки и трей.
        """
        self._last_confirmed_at = None
        self._nak_streak = 0
        self._headset_tab.set_available(False)
        if self._tray is not None:
            self._tray.clear_headset()

    def recover_transport(self) -> None:
        """Пере-скан устройств для авто-восстановления (вызов из воркер-потоков).

        Бюджет: не чаще одного discover() в секунду — иначе шторм NAK
        устраивает цикл пересканирования. Вне бюджета пропуск: ретрай
        повторит ошибку, восстановление случится на следующем тике.
        После пере-скана сервисы пересозданы (кэш пуст) — планируем
        полное обновление состояния.
        """
        now = time.monotonic()
        if not recovery_due(self._last_recovery_discover, now):
            return
        self._last_recovery_discover = now
        self._manager.discover()
        self._pending_refresh = True
        self._settings_loaded = False

    def shutdown(self) -> bool:
        """Единый путь остановки: таймер опроса, воркеры, менеджер. Идемпотентен.

        Используется из трея, из closeEvent и по aboutToQuit: при любом
        способе завершения команды не обрываются на середине. False —
        воркеры не завершились за таймаут: приложение остаётся в работе,
        менеджер не закрывается, выход нужно повторить.
        """
        if self._shutdown_done:
            return True
        self._poll_timer.stop()
        if not self._workers.shutdown():
            self._poll_timer.start()
            self.report_error(_SHUTDOWN_STUCK)
            return False
        self._shutdown_done = True
        self._manager.close()
        return True

    def tray_update_shutdown(self, minutes: int | None) -> None:
        if self._tray is not None:
            self._tray.update_shutdown(minutes)

    # --- обнаружение/переподключение ---

    def reconnect(self) -> None:
        """Пересканировать устройства (кнопка и старт приложения).

        Каждое переподключение — новое поколение: колбэк устаревшего
        поколения отбрасывается, снимок не перезаписывает свежий.
        """
        self._reconnect_generation += 1
        generation = self._reconnect_generation

        def job() -> ManagerSnapshot:
            return self._manager.discover()

        self._workers.run(
            job,
            lambda snapshot: self._apply_snapshot_if_current(generation, snapshot),
            lambda message, hint: self._report_error_if_current(
                generation, message, hint
            ),
            device="reconnect",
        )

    def _apply_snapshot_if_current(
        self, generation: int, snapshot: ManagerSnapshot
    ) -> None:
        """Колбэк reconnect: снимок устаревшего поколения игнорируется."""
        if generation != self._reconnect_generation:
            return
        self._apply_snapshot(snapshot)

    def _report_error_if_current(
        self, generation: int, message: str, hint: str
    ) -> None:
        """Колбэк ошибки reconnect: устаревшее поколение игнорируется."""
        if generation != self._reconnect_generation:
            return
        self.report_error(message, hint)

    def _apply_snapshot(self, snapshot: ManagerSnapshot) -> None:
        # Новое поколение снимков: pending-снимки тиков, записанные до этого
        # применения, устаревают и отбрасываются при доставке.
        self._snapshot_seq += 1
        errors = [e for e in (snapshot.dongle_error, snapshot.dock_error) if e]
        if errors:
            self.report_error("\n".join(errors), snapshot.hint)
        else:
            self.clear_error()
        if snapshot.has_dongle:
            # (Пере)подключение донгла: сервис пересоздан, кэш пуст —
            # полное обновление состояния на ближайшем тике.
            self._pending_refresh = True
            self._settings_loaded = False
            self._headset_tab.set_available(True)
        else:
            # Донгл не найден: показанное состояние наушников не подтверждено.
            self._clear_headset_state()
        self._dock_tab.set_available(snapshot.has_dock)
        self._device_labels[_DONGLE_ROW].setText(
            f"{snapshot.dongle_path} — {_FOUND}" if snapshot.has_dongle else _NOT_FOUND
        )
        self._device_labels[_DOCK_ROW].setText(
            f"{snapshot.dock_path} — {_FOUND}" if snapshot.has_dock else _NOT_FOUND
        )
        self._device_labels[_HIDRAW_ROW].setText(
            ", ".join(
                f"{path} [{vid:04x}:{pid:04x}]" for path, vid, pid in snapshot.devices
            )
            or "—"
        )
        needs_hint = bool(errors) or not snapshot.has_dongle or not snapshot.has_dock
        self._hint_label.setVisible(needs_hint)
        self._dock_tab.refresh()
        if snapshot.has_dock:
            self._refresh_dock_info()
        self.poll_now()

    def _refresh_dock_info(self) -> None:
        def job() -> dict[str, str] | None:
            with self._manager.command_session("dock") as service:
                if service is None:
                    return None
                return service.read_device_info()

        def on_success(info: dict[str, str] | None) -> None:
            if info is None:
                self._device_labels[_DOCK_INFO_ROW].setText(_DOCK_FW_NO_DATA)
            else:
                name = info.get("name", _DOCK_FW_NO_DATA)
                version = info.get("version", _DOCK_FW_NO_DATA)
                self._device_labels[_DOCK_INFO_ROW].setText(f"{name} / {version}")

        self._workers.run(job, on_success)

    def _on_dock_reset(self) -> None:
        answer = QMessageBox.question(
            self,
            _DOCK_RESET_BUTTON,
            _DOCK_RESET_CONFIRM,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        def job() -> bool:
            with self._manager.command_session("dock") as service:
                if service is None:
                    return False
                service.reset()
                return True

        def on_success(ok: bool) -> None:
            if ok:
                self.note(_DOCK_RESET_DONE)
            self._dock_tab.refresh()
            self._refresh_dock_info()

        self._workers.run(job, on_success, self.report_error, device="dock")

    # --- служебное ---

    def _on_tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self._dock_tab:
            self._dock_tab.refresh()

    def _on_close_to_tray_toggled(self, checked: bool) -> None:
        self._settings.setValue(_SETTINGS_CLOSE_TO_TRAY, checked)

    def _close_to_tray_enabled(self) -> bool:
        value = self._settings.value(_SETTINGS_CLOSE_TO_TRAY, True)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)

    def _restore_geometry(self) -> None:
        geometry = self._settings.value(_SETTINGS_GEOMETRY)
        if geometry is not None:
            self.restoreGeometry(geometry)

    def _save_geometry(self) -> None:
        self._settings.setValue(_SETTINGS_GEOMETRY, self.saveGeometry())

    def closeEvent(self, event: QCloseEvent | None) -> None:
        """Свернуть в трей, если он есть и разрешён; иначе полный выход."""
        if event is None:  # pyi допускает None; у Qt событие всегда приходит
            return
        self._save_geometry()
        if self._tray is not None and self._close_to_tray_enabled():
            event.ignore()
            self.hide()
            self.note(_HIDDEN_NOTE)
            return
        if not self.shutdown():
            # Фоновая операция не завершилась: остаёмся в работающем состоянии.
            event.ignore()
            return
        event.accept()
