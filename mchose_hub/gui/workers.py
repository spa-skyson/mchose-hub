"""QThread-воркеры: весь I/O к HID-сервисам выполняется вне GUI-потока.

Разовый воркер на операцию: job() вызывается в фоновом потоке, результат
и ошибки доставляются сигналами в главный поток. Результат None — штатный
таймаут устройства, TransportError — проблема доступа/записи.

Одно устройство — один активный job: новый запрос с тем же ключом устройства
становится в pending-слот (поток под него не создаётся), повторный запрос
заменяет pending. По завершении активного job'а запускается последний
неустаревший pending.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from PyQt6.QtCore import QObject, QThread, pyqtSignal

from ..audio import AudioError
from ..transport import TransportError

# Бюджет ожидания воркеров при выходе: worst-case — две полные сессии
# наушников (write-бюджет + grace + чтение), с запасом.
JOIN_TIMEOUT_MS = 15_000


class ServiceWorker(QThread):
    """Одноразовый поток под один вызов сервиса."""

    succeeded = pyqtSignal(object, arguments=[])  # результат job(), включая None
    failed = pyqtSignal(str, str, arguments=[])  # message, hint

    def __init__(self, job: Callable[[], Any]) -> None:
        super().__init__()
        self._job = job
        self.stale = False  # True — результат устарел, наружу не посылать

    def run(self) -> None:
        try:
            outcome = self._job()
        except TransportError as exc:
            if not self.stale:
                self.failed.emit(exc.message, exc.hint)
        except AudioError as exc:
            if not self.stale:
                self.failed.emit(exc.message, exc.hint)
        except Exception as exc:  # noqa: BLE001 — наверх уходит текст ошибки
            if not self.stale:
                self.failed.emit(str(exc), "")
        else:
            if not self.stale:
                self.succeeded.emit(outcome)


@dataclass
class _PendingJob:
    """Отложенный запрос устройства: выполняется после завершения активного."""

    job: Callable[[], Any]
    on_success: Callable[[Any], None] | None
    on_failure: Callable[[str, str], None] | None
    stale: bool = False  # заменён более новым запросом — не выполнять


class WorkerHost(QObject):
    """Владелец активных воркеров; pending-слоты на устройство, уборка по finished."""

    def __init__(self) -> None:
        super().__init__()
        self._workers: list[ServiceWorker] = []
        self._running: dict[str, ServiceWorker] = {}
        self._pending: dict[str, _PendingJob] = {}

    def run(
        self,
        job: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_failure: Callable[[str, str], None] | None = None,
        *,
        device: str | None = None,
    ) -> ServiceWorker | None:
        """Запустить job в фоновом потоке; обработчики вызовутся в GUI-потоке.

        device — ключ устройства: при активном job'е того же ключа новый
        запрос кладётся в pending-слот (поток не создаётся), заменяя прежний
        pending; возвращается None. Без ключа — всегда новый поток.
        """
        if device is not None:
            pending = self._pending.get(device)
            if pending is not None:
                pending.stale = True  # заменён более новым запросом
            if device in self._running:
                self._pending[device] = _PendingJob(job, on_success, on_failure)
                return None
        return self._spawn(job, on_success, on_failure, device)

    def _spawn(
        self,
        job: Callable[[], Any],
        on_success: Callable[[Any], None] | None,
        on_failure: Callable[[str, str], None] | None,
        device: str | None,
    ) -> ServiceWorker:
        worker = ServiceWorker(job)
        if on_success is not None:
            worker.succeeded.connect(on_success)
        if on_failure is not None:
            worker.failed.connect(on_failure)
        worker.finished.connect(lambda w=worker: self._retire(w))
        self._workers.append(worker)
        if device is not None:
            self._running[device] = worker
        worker.start()
        return worker

    def _retire(self, worker: ServiceWorker) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        for key, running in list(self._running.items()):
            if running is worker:
                del self._running[key]
                pending = self._pending.pop(key, None)
                if pending is not None and not pending.stale:
                    # Запуск отложенного job'а того же устройства.
                    self._spawn(
                        pending.job, pending.on_success, pending.on_failure, key
                    )
                break
        worker.deleteLater()

    def shutdown(self, timeout_ms: int = JOIN_TIMEOUT_MS) -> bool:
        """Дождаться активных воркеров. False — кто-то не завершился за таймаут.

        Отложенные pending-запросы при выходе отбрасываются. Незавершённые
        воркеры остаются под владением хоста: повторный shutdown ждёт их
        заново и возвращает True только когда живых ноль — приложение не
        гаснет с работающим QThread. Завершившийся воркер удаляется из
        списка стандартным finished-cleanup (_retire).
        """
        self._pending.clear()
        finished = True
        for worker in list(self._workers):
            if not worker.wait(timeout_ms):
                finished = False
        return finished
