"""Системный трей: рисуемая батарейка, меню режимов и автоотключения.

Создаётся безусловно (KDE Wayland ложно отчитывается об отсутствии лотка —
см. main_window._build_ui); на системах без лотка иконка не отображается —
функциональность деградирует молча.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
    QPolygonF,
)
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from ..protocol import BatteryState
from .helpers import battery_caption, battery_color

if TYPE_CHECKING:
    from .main_window import MainWindow

_TOOLTIP_PREFIX = "MCHOSE V9 Turbo+"
_TOOLTIP_NO_DATA = "MCHOSE V9 Turbo+ — нет данных"
_BATTERY_NO_DATA = "Батарея: нет данных"
_MODE_SUBMENU = "Режим звука"
_MODE_ACTIONS: tuple[tuple[int, str], ...] = (
    (0, "Игра 1"),
    (1, "Игра 2"),
    (2, "Музыка"),
)
_SHUTDOWN_SUBMENU = "Автоотключение"
_SHUTDOWN_ACTIONS: tuple[tuple[int, str], ...] = (
    (15, "15 минут"),
    (30, "30 минут"),
    (45, "45 минут"),
)
_OPEN_ACTION = "Открыть MCHOSE Hub"
_QUIT_ACTION = "Выход"

_ICON_SIZE = 64
_BODY = QRectF(6.0, 20.0, 44.0, 26.0)
_TIP = QRectF(51.0, 26.0, 7.0, 14.0)
_INSET = 4.0

_LIGHTNING: tuple[tuple[float, float], ...] = (
    (36.0, 20.0),
    (27.0, 35.0),
    (33.0, 35.0),
    (28.0, 47.0),
    (40.0, 31.0),
    (33.0, 31.0),
)


def paint_battery_pixmap(percent: int, charging: bool, asleep: bool) -> QPixmap:
    """Иконка-батарейка: корпус, заливка уровнем, молния при зарядке."""
    pixmap = QPixmap(_ICON_SIZE, _ICON_SIZE)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    outline = QColor("#9e9e9e") if asleep else QColor("#616161")
    painter.setPen(QPen(outline, 3.0))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(_BODY, 6.0, 6.0)
    painter.drawRoundedRect(_TIP, 2.0, 2.0)

    if not asleep and percent > 0:
        usable = _BODY.width() - 2 * _INSET
        fill_width = max(4.0, usable * max(0, min(100, percent)) / 100.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(battery_color(percent)))
        fill = QRectF(
            _BODY.left() + _INSET,
            _BODY.top() + _INSET,
            fill_width,
            _BODY.height() - 2 * _INSET,
        )
        painter.drawRoundedRect(fill, 3.0, 3.0)

    if charging:
        painter.setPen(QPen(QColor("#212121"), 1.5))
        painter.setBrush(QColor("#ffffff"))
        painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in _LIGHTNING]))
    painter.end()
    return pixmap


def _action(menu: QMenu, label: str) -> QAction:
    """addAction с сужением Optional (pyi PyQt6 перестраховывается)."""
    action = menu.addAction(label)
    if action is None:  # pragma: no cover — не случается для строковой перегрузки
        raise RuntimeError(f"не удалось создать пункт меню: {label}")
    return action


def _submenu(menu: QMenu, title: str) -> QMenu:
    submenu = menu.addMenu(title)
    if submenu is None:  # pragma: no cover
        raise RuntimeError(f"не удалось создать подменю: {title}")
    return submenu


class MchoseTray(QSystemTrayIcon):
    """Трей-иконка с меню; колбэки даёт главное окно.

    Родителя нет: время жизни держит ссылка в MainWindow.
    """

    def __init__(
        self,
        window: MainWindow,
        set_mode: Callable[[int], None],
        set_shutdown: Callable[[int], None],
    ) -> None:
        super().__init__()
        self._window = window

        menu = QMenu()
        self._menu = menu  # QMenu без родителя: держим ссылку, иначе соберёт GC
        self._battery_action = _action(menu, _BATTERY_NO_DATA)
        self._battery_action.setEnabled(False)
        menu.addSeparator()

        mode_menu = _submenu(menu, _MODE_SUBMENU)
        mode_group = QActionGroup(menu)
        self._mode_actions: dict[int, QAction] = {}
        for value, label in _MODE_ACTIONS:
            action = _action(mode_menu, label)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, v=value: set_mode(v))
            mode_group.addAction(action)
            self._mode_actions[value] = action

        shutdown_menu = _submenu(menu, _SHUTDOWN_SUBMENU)
        shutdown_group = QActionGroup(menu)
        self._shutdown_actions: dict[int, QAction] = {}
        for minutes, label in _SHUTDOWN_ACTIONS:
            action = _action(shutdown_menu, label)
            action.setCheckable(True)
            action.triggered.connect(lambda _=False, m=minutes: set_shutdown(m))
            shutdown_group.addAction(action)
            self._shutdown_actions[minutes] = action

        menu.addSeparator()
        open_action = _action(menu, _OPEN_ACTION)
        open_action.triggered.connect(self._open_window)
        quit_action = _action(menu, _QUIT_ACTION)
        quit_action.triggered.connect(self._quit_app)

        self.setContextMenu(menu)
        self.setToolTip(_TOOLTIP_NO_DATA)
        self.setIcon(QIcon(paint_battery_pixmap(0, charging=False, asleep=True)))
        self.activated.connect(self._on_activated)

    # --- обновление состояния ---

    def update_battery(self, state: BatteryState | None) -> None:
        """Батарея: иконка, тултип, пункт меню."""
        if state is None:
            return
        caption = battery_caption(state)
        self.setIcon(
            QIcon(paint_battery_pixmap(state.percent, state.charging, state.asleep))
        )
        text = f"{_TOOLTIP_PREFIX} — {state.percent}% ({caption})"
        self.setToolTip(text)
        self._battery_action.setText(text)

    def update_mode(self, mode: int | None) -> None:
        """Активный режим звука в подменю; None — режим неизвестен, снять отметки."""
        if mode is None:
            for action in self._mode_actions.values():
                action.setChecked(False)
            return
        for value, action in self._mode_actions.items():
            action.setChecked(value == mode)

    def update_shutdown(self, minutes: int | None) -> None:
        """Активное автоотключение в подменю."""
        if minutes is None:
            return
        for value, action in self._shutdown_actions.items():
            action.setChecked(value == minutes)

    # --- служебное ---

    def _open_window(self) -> None:
        self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _quit_app(self) -> None:
        """Выход: единый путь остановки; при незавершённых воркерах — отказ.

        Плашку с ошибкой показывает MainWindow.shutdown(); приложение
        детерминированно остаётся в работающем состоянии.
        """
        if not self._window.shutdown():
            return
        app: QApplication | None = QApplication.instance()
        if app is not None:
            app.quit()

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self._open_window()
