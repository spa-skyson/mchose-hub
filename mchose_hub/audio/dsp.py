"""Исполнение аудио-DSP через PipeWire (pactl/wpctl/pw-cli/pw-dump).

Фильтры EQ/NR живут в процессе-холдере `pw-cli`: PipeWire грузит
libpipewire-module-filter-chain только как модуль клиента, поэтому процесс
держится живым, пока эффект нужен; завершение холдера снимает эффект
объекты графа. pactl умеет грузить только pulse-модули (loopback — да,
filter-chain — нет), поэтому для filter-chain используется холдер.

Никаких print: библиотечный слой; ошибки — AudioError(message, hint).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from pathlib import Path

from .eq import (
    BAND_COUNT,
    EQ_NODE_INPUT,
    build_filter_chain_args,
    sanitize_gains,
)

STATE_DIR = Path.home() / ".config" / "mchose-hub"
STATE_PATH = STATE_DIR / "audio.json"

_SINK_POLL_TIMEOUT_S = 3.0
_SINK_POLL_STEP_S = 0.1
_SUBPROCESS_TIMEOUT_S = 10.0

NR_NODE_INPUT = "effect_input.mchose_nr"
NR_NODE_OUTPUT = "effect_output.mchose_nr"
_LADSPA_PLUGIN = "librnnoise_ladspa"
_LADSPA_DIRS = ("/usr/lib/ladspa", "/usr/lib64/ladspa", "/usr/local/lib/ladspa")

RNNOISE_HINT = (
    "установите noise-suppression-for-voice (пакет LADSPA-обёртки "
    "librnnoise_ladspa; в Arch — из AUR)"
)


class AudioError(Exception):
    """Ошибка аудио-DSP слоя с человекочитаемым сообщением и подсказкой."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message


def _default_state() -> dict[str, object]:
    return {
        "eq_enabled": False,
        "gains": [0.0] * BAND_COUNT,
        "nr_enabled": False,
        "monitor_enabled": False,
        "holder_pid": None,
        "monitor_module_idx": None,
    }


class PipeWireDSP:
    """Управление EQ, шумоподавлением и мониторингом микрофона в PipeWire."""

    def __init__(
        self,
        sink_hint: str = "MCHOSE",
        source_hint: str = "MCHOSE",
        *,
        state_path: Path | None = None,
    ) -> None:
        self._sink_hint = sink_hint
        self._source_hint = source_hint
        self.state_path = state_path if state_path is not None else STATE_PATH
        self.state = self.load_state()

    # ---------- subprocess helpers ----------

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        """Выполнить команду с таймаутом; отсутствие утилиты — AudioError."""
        try:
            return subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_SUBPROCESS_TIMEOUT_S,
                check=False,  # код выхода разбирают вызывающие
            )
        except FileNotFoundError as exc:
            raise AudioError(
                f"утилита {cmd[0]} не найдена",
                "установите pipewire (pactl/wpctl/pw-cli)",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioError(
                f"{cmd[0]} не ответил за {_SUBPROCESS_TIMEOUT_S} с"
            ) from exc

    def _check(self, cmd: list[str], what: str) -> subprocess.CompletedProcess[str]:
        """Выполнить команду и потребовать нулевой код выхода."""
        result = self._run(cmd)
        if result.returncode != 0:
            raise AudioError(f"{what} не удалось: {result.stderr.strip()}")
        return result

    def _spawn_holder(self, command: str) -> int:
        """Запустить процесс-холдер и выполнить в нём команду load-module.

        Холдер держит stdin открытым: pw-cli ждёт следующую команду, модуль
        и созданные им узлы графа живут, пока холдер жив.
        """
        proc = subprocess.Popen(
            ["pw-cli"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(command + "\n")
        proc.stdin.flush()
        return proc.pid

    def _kill_holder(self) -> None:
        """Завершить холдер: его фильтр-объекты исчезают из графа."""
        pid = self.state.get("holder_pid")
        if isinstance(pid, int) and self._is_our_holder(pid):
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    time.sleep(0.05)
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._set_state(holder_pid=None)

    @staticmethod
    def _is_our_holder(pid: int) -> bool:
        """Чужой pid мог переиспользоваться: холдер обязан быть pw-cli."""
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            return False
        return b"pw-cli" in cmdline

    # ---------- discovery ----------

    def _short_list(self, kind: str) -> list[str]:
        """Имена из `pactl list short <kind>` (второе tab-поле)."""
        result = self._run(["pactl", "list", "short", kind])
        names = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1]:
                names.append(fields[1])
        return names

    @staticmethod
    def _is_physical(name: str, hint: str) -> bool:
        """Совпадение с подсказкой без виртуальных нод фильтров и мониторов."""
        low = name.lower()
        return (
            hint.lower() in low
            and not low.startswith("effect_")
            and not low.endswith(".monitor")
        )

    def find_sink(self) -> str | None:
        """Физический MCHOSE sink по подсказке (без индексов)."""
        for name in self._short_list("sinks"):
            if self._is_physical(name, self._sink_hint):
                return name
        return None

    def find_source(self) -> str | None:
        """Физический MCHOSE source по подсказке; None — микрофон не активен."""
        for name in self._short_list("sources"):
            if self._is_physical(name, self._source_hint):
                return name
        return None

    def _default_of(self, kind: str) -> str | None:
        """Текущий default sink/source из `pactl info`."""
        result = self._run(["pactl", "info"])
        prefix = f"Default {kind.capitalize()}: "
        for line in result.stdout.splitlines():
            if line.startswith(prefix):
                return line[len(prefix) :].strip()
        return None

    def _set_default(self, kind: str, name: str) -> None:
        """Переключить default sink/source: wpctl по id, fallback pactl по имени."""
        node_id = self._node_id(name)
        if node_id is not None:
            result = self._run(["wpctl", "set-default", str(node_id)])
            if result.returncode == 0:
                return
        self._check(
            [
                "pactl",
                "set-default-sink" if kind == "sink" else "set-default-source",
                name,
            ],
            f"переключение default {kind}",
        )

    def _node_id(self, node_name: str) -> int | None:
        """PipeWire id ноды по node.name через pw-dump."""
        result = self._run(["pw-dump"])
        if result.returncode != 0:
            return None
        try:
            dump = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        for obj in dump:
            props = (obj.get("info") or {}).get("props") or {}
            if props.get("node.name") == node_name:
                return obj.get("id")
        return None

    # ---------- persistence ----------

    def load_state(self) -> dict[str, object]:
        """Состояние из ~/.config/mchose-hub/audio.json (или дефолт)."""
        state = _default_state()
        try:
            stored = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return state
        if not isinstance(stored, dict):
            return state
        state.update({k: v for k, v in stored.items() if k in state})
        return state

    def save_state(self) -> None:
        """Записать состояние (гейны, флаги, pid холдера)."""
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def _set_state(self, **changes: object) -> None:
        self.state.update(changes)
        self.save_state()

    # ---------- EQ ----------

    def eq_loaded(self) -> bool:
        """EQ-синк присутствует в графе."""
        return any(name.startswith(EQ_NODE_INPUT) for name in self._short_list("sinks"))

    def enable_eq(self, gains: list[float]) -> str:
        """Включить EQ: холдер с filter-chain, default на виртуальный sink.

        Активные sink-inputs физического sink переносятся на виртуальный.
        Возвращает имя виртуального sink'а.
        """
        gains = sanitize_gains(gains)
        physical = self.find_sink()
        if physical is None:
            raise AudioError(
                "MCHOSE аудиокарта не найдена",
                "подключите наушники по 2.4G (донгл = аудиокарта V9 Turbo+)",
            )
        if self.eq_loaded():
            self.disable_eq()
        inputs = self._sink_inputs_of(physical)
        self._set_state(
            holder_pid=self._spawn_holder(
                self._holder_command(build_filter_chain_args(gains, physical))
            ),
            gains=[float(g) for g in gains],
        )
        if not self._wait_for_sink():
            self._kill_holder()
            raise AudioError(
                "PipeWire не создал EQ-синк",
                "проверьте журнал: journalctl --user -u pipewire",
            )
        self._set_default("sink", EQ_NODE_INPUT)
        for index in inputs:
            self._run(["pactl", "move-sink-input", str(index), EQ_NODE_INPUT])
        self._set_state(eq_enabled=True)
        return EQ_NODE_INPUT

    def disable_eq(self) -> str:
        """Выключить EQ: default и потоки — на физический sink, холдер завершён."""
        physical = self.find_sink()
        if physical is None:
            raise AudioError(
                "MCHOSE аудиокарта не найдена",
                "EQ-синк останется в графе: подключите карту и повторите",
            )
        if self._default_of("sink") == EQ_NODE_INPUT:
            self._set_default("sink", physical)
        for index in self._sink_inputs_of(EQ_NODE_INPUT):
            self._run(["pactl", "move-sink-input", str(index), physical])
        self._kill_holder()
        self._wait_until_gone("sinks", EQ_NODE_INPUT)
        self._set_state(eq_enabled=False)
        return physical

    def set_eq_gains(self, gains: list[float]) -> None:
        """Обновить гейны живого EQ без пересоздания графа.

        Сначала pw-cli set-param Props с фактическими именами контролов
        ("eq_band_N:Gain"); применимость проверяется чтением Props через
        enum-params — если свойство не применилось (старый PipeWire /
        session-manager), фолбэк: пересоздание фильтра с новыми гейнами.
        """
        gains = sanitize_gains(gains)
        node_id = self._node_id(EQ_NODE_INPUT)
        if node_id is not None:
            controls = {
                f"eq_band_{i}:Gain": float(gains[i - 1])
                for i in range(1, BAND_COUNT + 1)
            }
            self._run(
                ["pw-cli", "set-param", str(node_id), "Props", json.dumps(controls)]
            )
            if self._read_gains(node_id) == [float(g) for g in gains]:
                self._set_state(gains=[float(g) for g in gains])
                return
        # Не применилось: пересоздание фильтра с новыми гейнами.
        if self.eq_loaded():
            self.enable_eq(gains)
        else:
            self._set_state(gains=[float(g) for g in gains])

    def _read_gains(self, node_id: int) -> list[float] | None:
        """Текущие гейны ноды из Props (enum-params); None — контролы не читаются."""
        result = self._run(["pw-cli", "enum-params", str(node_id), "Props"])
        values: dict[str, float] = {}
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("String ") and ":Gain" in stripped:
                name = stripped[len('String "') : -1]
                if i + 1 < len(lines):
                    values[name] = _parse_pod_float(lines[i + 1])
        if not values:
            return None
        gains = []
        for i in range(1, BAND_COUNT + 1):
            value = values.get(f"eq_band_{i}:Gain")
            if value is None:
                return None
            gains.append(value)
        return gains

    def _wait_for_sink(self) -> bool:
        """Появление EQ-синка в графе (poll ≤3 c)."""
        deadline = time.monotonic() + _SINK_POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.eq_loaded():
                return True
            time.sleep(_SINK_POLL_STEP_S)
        return False

    def _wait_until_gone(self, kind: str, needle: str) -> None:
        """Исчезновение сущности из `pactl list short <kind>` (poll ≤3 c)."""
        deadline = time.monotonic() + _SINK_POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            if not any(needle in name for name in self._short_list(kind)):
                return
            time.sleep(_SINK_POLL_STEP_S)

    def _sink_inputs_of(self, sink_name: str) -> list[str]:
        """Индексы sink-inputs, подключённых к sink (по индексу sink)."""
        listing = self._run(["pactl", "list", "short", "sinks"])
        sink_index: str | None = None
        for line in listing.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1] == sink_name:
                sink_index = fields[0]
                break
        if sink_index is None:
            return []
        result = self._run(["pactl", "list", "short", "sink-inputs"])
        indices = []
        for line in result.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) >= 2 and fields[1] == sink_index:
                indices.append(fields[0])
        return indices

    @staticmethod
    def _holder_command(args_json: str) -> str:
        """Строка команды холдера: load-module с свойствами из args-JSON."""
        obj = json.loads(args_json)
        parts = ["load-module", "libpipewire-module-filter-chain"]
        for key, value in obj.items():
            parts.append(f"{key}={json.dumps(value, separators=(',', ':'))}")
        return " ".join(parts)

    # ---------- NR (RNNoise) ----------

    def nr_supported(self) -> bool:
        """LADSPA-обёртка RNNoise (librnnoise_ladspa) доступна системе?"""
        result = self._run(["ldconfig", "-p"])
        if _LADSPA_PLUGIN in result.stdout:
            return True
        for directory in _LADSPA_DIRS:
            if (Path(directory) / f"{_LADSPA_PLUGIN}.so").exists():
                return True
        return False

    def enable_nr(self) -> str:
        """Включить шумоподавление микрофона: filter-chain RNNoise на source.

        Возвращает имя виртуального source. AudioError — плагина нет или
        MCHOSE микрофон не активен (SUSPENDED/нет ссылки).
        """
        if not self.nr_supported():
            raise AudioError(f"LADSPA-плагин {_LADSPA_PLUGIN} не найден", RNNOISE_HINT)
        if self.nr_loaded():
            self.disable_nr()
        source = self.find_source()
        if source is None:
            raise AudioError(
                "MCHOSE микрофон не найден (не активен)",
                "проверьте, что наушники включены и микрофон не отключён в системе",
            )
        graph = {
            "nodes": [
                {
                    "type": "ladspa",
                    "name": "rnnoise",
                    "plugin": _LADSPA_PLUGIN,
                    "label": "noise_suppressor_mono",
                }
            ],
            "capture.props": {"node.passive": True, "target.object": source},
            "playback.props": {
                "node.name": NR_NODE_OUTPUT,
                "media.class": "Audio/Source",
            },
        }
        args = {
            "node.description": "MCHOSE NR Mic",
            "media.name": "MCHOSE NR Mic",
            "filter.graph": graph,
            "audio.position": ["MONO"],
        }
        self._set_state(
            holder_pid=self._spawn_holder(
                self._holder_command(json.dumps(args, separators=(",", ":")))
            )
        )
        if not self._wait_for_node("sources", NR_NODE_OUTPUT):
            self._kill_holder()
            raise AudioError(
                "PipeWire не создал NR-source",
                "проверьте журнал: journalctl --user -u pipewire",
            )
        self._set_default("source", NR_NODE_OUTPUT)
        self._set_state(nr_enabled=True)
        return NR_NODE_OUTPUT

    def disable_nr(self) -> str:
        """Выключить шумоподавление: default source — физический, холдер завершён."""
        physical = self.find_source()
        if physical is None:
            raise AudioError(
                "MCHOSE микрофон не найден",
                "включите микрофон и повторите; виртуальный source останется",
            )
        if self._default_of("source") == NR_NODE_OUTPUT:
            self._set_default("source", physical)
        self._kill_holder()
        self._wait_until_gone("sources", NR_NODE_OUTPUT)
        self._set_state(nr_enabled=False)
        return physical

    def nr_loaded(self) -> bool:
        """NR-source присутствует в графе."""
        return any(
            name.startswith(NR_NODE_OUTPUT) for name in self._short_list("sources")
        )

    def _wait_for_node(self, kind: str, node_name: str) -> bool:
        """Появление ноды среди pactl-сущностей (poll ≤3 c)."""
        deadline = time.monotonic() + _SINK_POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            if any(node_name in name for name in self._short_list(kind)):
                return True
            time.sleep(_SINK_POLL_STEP_S)
        return False

    # ---------- микрофон ----------

    def mic_volume(self, percent: int | None = None) -> int:
        """Текущая громкость MCHOSE-микрофона в %; с percent — установить."""
        source = self.find_source()
        if source is None:
            raise AudioError(
                "MCHOSE микрофон не найден (не активен)",
                "проверьте, что наушники включены и микрофон не отключён в системе",
            )
        node_id = self._node_id(source)
        if node_id is None:
            raise AudioError(f"нода микрофона {source} не найдена в PipeWire")
        if percent is not None:
            if not 0 <= percent <= 100:
                raise AudioError("громкость микрофона: ожидается 0..100")
            self._check(
                ["wpctl", "set-volume", str(node_id), f"{percent}%"],
                "установка громкости микрофона",
            )
        result = self._check(["wpctl", "get-volume", str(node_id)], "чтение громкости")
        token = result.stdout.replace("[MUTED]", "").split(":", maxsplit=1)[-1].strip()
        try:
            return round(float(token) * 100)
        except ValueError as exc:
            raise AudioError(
                f"не удалось разобрать громкость: {result.stdout!r}"
            ) from exc

    def mic_mute(self) -> str:
        """Заглушить MCHOSE-микрофон; возвращает имя source."""
        return self._mic_set_mute(True)

    def mic_unmute(self) -> str:
        """Снять заглушение MCHOSE-микрофона; возвращает имя source."""
        return self._mic_set_mute(False)

    def _mic_set_mute(self, mute: bool) -> str:
        source = self.find_source()
        if source is None:
            raise AudioError(
                "MCHOSE микрофон не найден (не активен)",
                "проверьте, что наушники включены и микрофон не отключён в системе",
            )
        node_id = self._node_id(source)
        if node_id is None:
            raise AudioError(f"нода микрофона {source} не найдена в PipeWire")
        self._check(
            ["wpctl", "set-mute", str(node_id), "1" if mute else "0"],
            "переключение mute микрофона",
        )
        return source

    # ---------- мониторинг ----------

    def monitoring_enable(self) -> str:
        """Мониторинг микрофона: module-loopback source -> default sink.

        Возвращает описание маршрута. Требует активный MCHOSE source.
        """
        if self.monitoring_active():
            self.monitoring_disable()
        source = self.find_source()
        if source is None:
            raise AudioError(
                "MCHOSE микрофон не найден (не активен)",
                "мониторинг требует активный микрофон наушников",
            )
        sink = self._default_of("sink")
        if sink is None:
            raise AudioError("default sink не определён", "проверьте вывод pactl info")
        result = self._check(
            [
                "pactl",
                "load-module",
                "module-loopback",
                f"source={source}",
                f"sink={sink}",
            ],
            "загрузка module-loopback",
        )
        index = _parse_module_index(result.stdout)
        self._set_state(monitor_enabled=True, monitor_module_idx=index)
        return f"{source} -> {sink}"

    def monitoring_disable(self) -> str:
        """Выключить мониторинг: unload module-loopback."""
        index = self.state.get("monitor_module_idx")
        if not isinstance(index, int) or not self._module_exists(index):
            index = self._find_loopback_module()
        if index is None:
            self._set_state(monitor_enabled=False, monitor_module_idx=None)
            return "мониторинг не был активен"
        self._check(["pactl", "unload-module", str(index)], "выгрузка module-loopback")
        self._set_state(monitor_enabled=False, monitor_module_idx=None)
        return "мониторинг выключен"

    def monitoring_active(self) -> bool:
        """module-loopback для MCHOSE-микрофона загружен?"""
        index = self.state.get("monitor_module_idx")
        if isinstance(index, int) and self._module_exists(index):
            return True
        return self._find_loopback_module() is not None

    def _module_exists(self, index: int) -> bool:
        listing = self._run(["pactl", "list", "short", "modules"])
        return any(
            line.split("\t")[0] == str(index) for line in listing.stdout.splitlines()
        )

    def _find_loopback_module(self) -> int | None:
        """Индекс module-loopback с MCHOSE-источником (после рестарта pipewire)."""
        listing = self._run(["pactl", "list", "short", "modules"])
        for line in listing.stdout.splitlines():
            fields = line.split("\t")
            if (
                len(fields) >= 3
                and "module-loopback" in fields[1]
                and self._sink_hint.lower() in fields[2].lower()
            ):
                try:
                    return int(fields[0])
                except ValueError:
                    continue
        return None


def _parse_pod_float(line: str) -> float:
    """Float из строки POD-вывода pw-cli («Float 3,500000» / «Float 3.500000»)."""
    token = line.replace(",", ".").split()[-1]
    return float(token)


def _parse_module_index(stdout: str) -> int | None:
    """Индекс модуля из вывода pactl load-module («New module index: N»)."""
    for line in stdout.splitlines():
        if "index" in line.lower():
            digits = "".join(ch for ch in line if ch.isdigit())
            if digits:
                return int(digits)
    return None
