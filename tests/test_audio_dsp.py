"""Тесты dsp.py: PipeWireDSP поверх FakeSubprocess (состояния pactl/wpctl/pw-cli)."""

from __future__ import annotations

import json
import subprocess
import threading
from typing import Any

import pytest

from mchose_hub.audio import AudioError, PipeWireDSP
from mchose_hub.audio.dsp import NR_NODE_OUTPUT, RNNOISE_HINT, _default_state
from mchose_hub.audio.eq import EQ_NODE_INPUT, EQ_NODE_OUTPUT

PHYSICAL_SINK = (
    "alsa_output.usb-C-Media_Electronics_Inc_MCHOSE_V9_Turbo__0123456789AB-01."
    "analog-stereo"
)
PHYSICAL_SOURCE = (
    "alsa_input.usb-C-Media_Electronics_Inc_MCHOSE_V9_Turbo__0123456789AB-01."
    "mono-fallback"
)


class FakeStdin:
    """stdin холдера: close() = EOF = pw-cli завершается и снимает фильтр."""

    def __init__(self, fake: FakeSubprocess, proc: FakeProcess) -> None:
        self._fake = fake
        self._proc = proc
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return  # как файловый close: идемпотентен
        self.closed = True
        if not self._proc.stuck:
            self._fake._process_exit(self._proc)


class FakeProcess:
    """Popen-двойник: жив, пока stdin открыт; stuck — игнорирует EOF (нужен kill)."""

    def __init__(self, fake: FakeSubprocess, pid: int, *, stuck: bool = False) -> None:
        self._fake = fake
        self.pid = pid
        self.stdin = FakeStdin(fake, self)
        self.returncode: int | None = None
        self.stuck = stuck
        self.owned: list[str] = []  # имена нод, созданных этим холдером

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        if self.stuck or not self.stdin.closed:
            raise subprocess.TimeoutExpired("pw-cli", timeout)
        self.returncode = 0
        return 0

    def kill(self) -> None:
        self.stuck = False
        self._fake._process_exit(self)
        self.returncode = -9  # SIGKILL

    def poll(self) -> int | None:
        return self.returncode


class FakeSubprocess:
    """Состояния аудио-системы в памяти: pactl/wpctl/pw-dump/pw-cli."""

    def __init__(self, *, ldconfig: str = "", apply_setparam: bool = False) -> None:
        self.sinks: list[str] = [PHYSICAL_SINK]
        self.sources: list[str] = [PHYSICAL_SOURCE]
        self.modules: list[tuple[int, str, str]] = []
        self.sink_inputs: list[tuple[int, int]] = []  # (input idx, sink idx)
        self.default_sink = PHYSICAL_SINK
        self.default_source = "alsa_input.usb-046d_webcam.mono-fallback"
        self.calls: list[list[str]] = []
        self.holder_commands: list[str] = []
        self.holders: list[FakeProcess] = []
        self.next_holder_pid = 42424
        self.nodes: dict[str, int] = {PHYSICAL_SINK: 619, PHYSICAL_SOURCE: 620}
        self.node_clients: dict[str, int] = {}  # имя ноды → pipeWire-id клиента
        self.clients: dict[int, int] = {}  # id клиента → OS-pid владельца
        self.next_client_id = 6001
        self.enum_gains: list[float] = [0.0] * 10
        self.volume: float = 0.50
        self.ldconfig = ldconfig
        self.apply_setparam = apply_setparam
        self.next_module_idx = 100
        self.load_module_stdout: str | None = None
        self.load_module_spawn = 1  # сколько модулей создаёт одна команда

    # --- точки подмены dsp ---

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, self._handle(cmd), "")

    def spawn(self, command: str) -> FakeProcess:
        """Холдер pw-cli: load-module создаёт фильтр-ноды, живёт до EOF/kill."""
        self.holder_commands.append(command)
        proc = FakeProcess(self, self.next_holder_pid)
        self.next_holder_pid += 1
        self.holders.append(proc)
        client_id = self.next_client_id
        self.next_client_id += 2
        # pw-cli-холдер держит два client-подключения: модульный (владелец
        # нод) и главный REPL — уничтожаются вместе, иначе процесс живёт.
        self.clients[client_id] = proc.pid  # модульный
        self.clients[client_id + 1] = proc.pid  # главный
        proc.owned = self._add_filter_nodes("nr" if "rnnoise" in command else "eq")
        for name in proc.owned:
            self.node_clients[name] = client_id
        return proc

    def _add_filter_nodes(self, kind: str) -> list[str]:
        if kind == "nr":
            names = [NR_NODE_OUTPUT]
            self.nodes[NR_NODE_OUTPUT] = 900 + len(self.nodes)
            self.sources.append(NR_NODE_OUTPUT)
        else:
            names = [EQ_NODE_INPUT, EQ_NODE_OUTPUT]
            for name in names:
                self.nodes[name] = 900 + len(self.nodes)
            self.sinks.append(EQ_NODE_INPUT)
        return names

    def _process_exit(self, proc: FakeProcess) -> None:
        """Холдер умер: только его filter-chain-ноды исчезают из графа."""
        if proc in self.holders:
            self.holders.remove(proc)
        for name in proc.owned:
            if name in self.nodes:
                del self.nodes[name]
            self.node_clients.pop(name, None)
            self.sinks = [s for s in self.sinks if s != name]
            self.sources = [s for s in self.sources if s != name]

    # --- имитация команд ---

    def _handle(self, cmd: list[str]) -> str:
        match cmd:
            case ["pactl", "list", "short", "modules"]:
                return "".join(
                    f"{i}\t{name}\t{args}\n" for i, name, args in self.modules
                )
            case ["pactl", "list", "short", "sink-inputs"]:
                return "".join(f"{i}\t{s}\tclient\n" for i, s in self.sink_inputs)
            case ["pactl", "list", "short", kind]:
                names = {"sinks": self.sinks, "sources": self.sources}[kind]
                return "".join(f"{i}\t{n}\textra\n" for i, n in enumerate(names))
            case ["pactl", "info"]:
                return f"Default Sink: {self.default_sink}\nDefault Source: {self.default_source}\n"
            case ["pactl", "move-sink-input", _index, _sink_name]:
                return ""
            case ["pactl", "load-module", "module-loopback", *args]:
                created: list[int] = []
                for _ in range(self.load_module_spawn):
                    idx = self.next_module_idx
                    self.next_module_idx += 1
                    self.modules.append((idx, "module-loopback", " ".join(args)))
                    created.append(idx)
                if self.load_module_stdout is not None:
                    return self.load_module_stdout
                if created:
                    return f"New module index: {created[0]}\n"
                return ""
            case ["pactl", "unload-module", index]:
                self.modules = [m for m in self.modules if m[0] != int(index)]
                return ""
            case ["pactl", "set-default-sink", name]:
                self.default_sink = name
                return ""
            case ["pactl", "set-default-source", name]:
                self.default_source = name
                return ""
            case ["pw-dump"]:
                entries: list[dict[str, Any]] = []
                for name, node_id in self.nodes.items():
                    props: dict[str, Any] = {"node.name": name}
                    if name in self.node_clients:
                        props["client.id"] = self.node_clients[name]
                    entries.append(
                        {
                            "id": node_id,
                            "type": "PipeWire:Interface:Node",
                            "info": {"props": props},
                        }
                    )
                for cid, pid in self.clients.items():
                    entries.append(
                        {
                            "id": cid,
                            "type": "PipeWire:Interface:Client",
                            "info": {"props": {"application.process.id": pid}},
                        }
                    )
                return json.dumps(entries)
            case ["wpctl", "set-default", node_id]:
                name = self._name_by_id(int(node_id))
                if name in self.sinks:
                    self.default_sink = name
                elif name in self.sources:
                    self.default_source = name
                return ""
            case ["wpctl", "set-volume", _id, _vol]:
                return ""
            case ["wpctl", "get-volume", _id]:
                return f"Volume: {self.volume:.2f}\n"
            case ["wpctl", "set-mute", _id, _flag]:
                return ""
            case ["ldconfig", "-p"]:
                return self.ldconfig
            case ["pw-cli", "set-param", _id, "Props", payload]:
                if self.apply_setparam:
                    self.enum_gains = list(json.loads(payload).values())
                return ""
            case ["pw-cli", "enum-params", _id, "Props"]:
                return "".join(
                    f'    String "eq_band_{i}:Gain"\n    Float {g:.6f}\n'
                    for i, g in enumerate(self.enum_gains, start=1)
                )
            case _:
                raise AssertionError(f"неожиданная команда: {cmd}")

    def _name_by_id(self, node_id: int) -> str:
        for name, pid in self.nodes.items():
            if pid == node_id:
                return name
        raise AssertionError(f"id {node_id} не найден")


def _make_dsp(tmp_path: Any, fake: FakeSubprocess) -> PipeWireDSP:
    dsp = PipeWireDSP(state_path=tmp_path / "audio.json")
    dsp._run = fake.run  # type: ignore[method-assign]
    dsp._spawn_holder = fake.spawn  # type: ignore[method-assign]
    return dsp


def test_find_sink_by_hint(tmp_path: Any) -> None:
    """Физический MCHOSE sink найден по подстроке, виртуальные игнорируются."""
    fake = FakeSubprocess()
    fake.sinks.append(EQ_NODE_INPUT)  # чужой виртуальный EQ не должен маскироваться
    dsp = _make_dsp(tmp_path, fake)
    assert dsp.find_sink() == PHYSICAL_SINK


def test_find_source_skips_monitor_and_virtual(tmp_path: Any) -> None:
    """Source: .monitor и effect_* не считаются физическим микрофоном."""
    fake = FakeSubprocess()
    fake.sources = [
        f"{PHYSICAL_SINK}.monitor",
        PHYSICAL_SOURCE,
        NR_NODE_OUTPUT,
    ]
    dsp = _make_dsp(tmp_path, fake)
    assert dsp.find_source() == PHYSICAL_SOURCE


def test_find_sink_absent(tmp_path: Any) -> None:
    """Нет MCHOSE-карты: find_sink/find_source → None."""
    fake = FakeSubprocess()
    fake.sinks = ["alsa_output.pci-0000.analog-stereo"]
    fake.sources = []
    dsp = _make_dsp(tmp_path, fake)
    assert dsp.find_sink() is None
    assert dsp.find_source() is None


def test_enable_eq_loads_filter_chain_and_sets_default(tmp_path: Any) -> None:
    """enable_eq: холдер получает filter-chain args, default — виртуальный sink."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    name = dsp.enable_eq([0.0] * 10)
    assert name == EQ_NODE_INPUT
    assert fake.default_sink == EQ_NODE_INPUT
    assert len(fake.holder_commands) == 1
    command = fake.holder_commands[0]
    assert command.startswith("load-module libpipewire-module-filter-chain")
    graph_part = next(
        part for part in command.split() if part.startswith("filter.graph=")
    )
    graph = json.loads(graph_part.removeprefix("filter.graph="))
    assert len(graph["nodes"]) == 10
    assert all(node["label"] == "bq_peaking" for node in graph["nodes"])
    binding = next(
        part for part in command.split() if part.startswith("playback.props=")
    )
    assert (
        json.loads(binding.removeprefix("playback.props="))["target.object"]
        == PHYSICAL_SINK
    )
    assert dsp.state["eq_enabled"] is True
    assert dsp.state["eq_holder_pid"] == 42424


def test_enable_eq_moves_active_sink_inputs(tmp_path: Any) -> None:
    """Активные sink-inputs физического sink переносятся на виртуальный."""
    fake = FakeSubprocess()
    fake.sink_inputs = [(77, 0), (78, 0)]  # оба играют в MCHOSE sink
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_eq([0.0] * 10)
    moves = [call for call in fake.calls if call[:2] == ["pactl", "move-sink-input"]]
    assert moves == [
        ["pactl", "move-sink-input", "77", EQ_NODE_INPUT],
        ["pactl", "move-sink-input", "78", EQ_NODE_INPUT],
    ]


def test_enable_eq_without_mchose_sink_raises(tmp_path: Any) -> None:
    """Нет MCHOSE-карты: AudioError с подсказкой, холдер не создаётся."""
    fake = FakeSubprocess()
    fake.sinks = ["alsa_output.pci-0000.analog-stereo"]
    dsp = _make_dsp(tmp_path, fake)
    with pytest.raises(AudioError, match="MCHOSE"):
        dsp.enable_eq([0.0] * 10)
    assert fake.holder_commands == []


def test_disable_eq_restores_default_and_inputs(tmp_path: Any) -> None:
    """disable_eq: default и потоки возвращены на физический sink, sink исчез."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_eq([0.0] * 10)
    fake.sink_inputs = [(90, 1)]  # input уже на виртуальном (idx 1)
    physical = dsp.disable_eq()
    assert physical == PHYSICAL_SINK
    assert fake.default_sink == PHYSICAL_SINK
    assert dsp.state["eq_enabled"] is False
    assert EQ_NODE_INPUT not in fake.sinks
    moves = [call for call in fake.calls if call[:2] == ["pactl", "move-sink-input"]]
    assert ["pactl", "move-sink-input", "90", PHYSICAL_SINK] in moves


def test_set_eq_gains_falls_back_to_rebuild(tmp_path: Any) -> None:
    """set-param не применился: EQ пересоздаётся с новыми гейнами."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_eq([0.0] * 10)
    dsp.set_eq_gains([6.0, 0, 0, 0, 0, 0, 0, 0, 0, -3.0])
    assert any(
        call[:2] == ["pw-cli", "set-param"] for call in fake.calls
    )  # попытка set-param была
    assert len(fake.holder_commands) == 2  # фолбэк: пересоздание
    graph_part = next(
        part
        for part in fake.holder_commands[1].split()
        if part.startswith("filter.graph=")
    )
    graph = json.loads(graph_part.removeprefix("filter.graph="))
    assert graph["nodes"][0]["control"]["Gain"] == 6.0
    assert graph["nodes"][9]["control"]["Gain"] == -3.0
    assert dsp.state["gains"] == [6.0, 0, 0, 0, 0, 0, 0, 0, 0, -3.0]


def test_set_eq_gains_in_place_when_applied(tmp_path: Any) -> None:
    """set-param применился (enum подтверждает): без пересоздания."""
    fake = FakeSubprocess(apply_setparam=True)
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_eq([0.0] * 10)
    dsp.set_eq_gains([2.0] * 10)
    assert len(fake.holder_commands) == 1  # пересоздания нет
    assert dsp.state["gains"] == [2.0] * 10


def test_nr_supported_false_without_plugin(tmp_path: Any, monkeypatch: Any) -> None:
    """librnnoise_ladspa нет ни в ldconfig, ни в стандартных путях → False."""
    monkeypatch.setattr(
        "mchose_hub.audio.dsp._LADSPA_DIRS", (str(tmp_path / "ladspa"),)
    )
    fake = FakeSubprocess(ldconfig="librnnoise.so (libc)\n")
    dsp = _make_dsp(tmp_path, fake)
    assert dsp.nr_supported() is False


def test_nr_supported_true_via_ldconfig(tmp_path: Any) -> None:
    """Плагин найден в ldconfig → True."""
    fake = FakeSubprocess(ldconfig="\tlibrnnoise_ladspa.so (libc,x86_64)\n")
    dsp = _make_dsp(tmp_path, fake)
    assert dsp.nr_supported() is True


def test_enable_nr_missing_plugin_hint(tmp_path: Any, monkeypatch: Any) -> None:
    """Плагина нет: AudioError с подсказкой установки noise-suppression-for-voice."""
    monkeypatch.setattr(
        "mchose_hub.audio.dsp._LADSPA_DIRS", (str(tmp_path / "ladspa"),)
    )
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    with pytest.raises(AudioError, match="librnnoise_ladspa") as excinfo:
        dsp.enable_nr()
    assert "noise-suppression-for-voice" in excinfo.value.hint
    assert excinfo.value.hint == RNNOISE_HINT
    assert fake.holder_commands == []


def test_enable_nr_without_source_raises(tmp_path: Any) -> None:
    """Плагин есть, микрофон не активен: AudioError, холдер не создаётся."""
    fake = FakeSubprocess(ldconfig="\tlibrnnoise_ladspa.so (libc)\n")
    fake.sources = []
    dsp = _make_dsp(tmp_path, fake)
    with pytest.raises(AudioError, match="микрофон"):
        dsp.enable_nr()
    assert fake.holder_commands == []


def test_enable_nr_loads_rnnoise_and_sets_default(tmp_path: Any) -> None:
    """NR: filter-chain ladspa на source, default source — виртуальный."""
    fake = FakeSubprocess(ldconfig="\tlibrnnoise_ladspa.so (libc)\n")
    dsp = _make_dsp(tmp_path, fake)
    name = dsp.enable_nr()
    assert name == NR_NODE_OUTPUT
    assert fake.default_source == NR_NODE_OUTPUT
    command = fake.holder_commands[0]
    assert '"plugin":"librnnoise_ladspa"' in command
    assert "noise_suppressor_mono" in command
    assert "target.object" in command
    assert dsp.state["nr_enabled"] is True
    physical = dsp.disable_nr()
    assert physical == PHYSICAL_SOURCE
    assert fake.default_source == PHYSICAL_SOURCE


def test_persistence_roundtrip(tmp_path: Any) -> None:
    """Состояние audio.json: сохранение → чтение новым экземпляром."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    dsp._set_state(
        eq_enabled=True,
        gains=[1.0] * 10,
        nr_enabled=False,
        monitor_enabled=True,
    )
    fresh = PipeWireDSP(state_path=tmp_path / "audio.json")
    assert fresh.state["eq_enabled"] is True
    assert fresh.state["gains"] == [1.0] * 10
    assert fresh.state["monitor_enabled"] is True


def test_mic_volume_set_and_read(tmp_path: Any) -> None:
    """mic_volume: установка через wpctl set-volume и чтение get-volume."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    assert dsp.mic_volume(50) == 50
    assert ["wpctl", "set-volume", "620", "50%"] in fake.calls
    assert dsp.mic_volume() == 50


def test_mic_volume_out_of_range(tmp_path: Any) -> None:
    """Громкость вне 0..100 — AudioError."""
    dsp = _make_dsp(tmp_path, FakeSubprocess())
    with pytest.raises(AudioError, match="0..100"):
        dsp.mic_volume(150)


def test_mic_mute_unmute(tmp_path: Any) -> None:
    """mic_mute/mic_unmute: wpctl set-mute на MCHOSE source."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    assert dsp.mic_mute() == PHYSICAL_SOURCE
    assert dsp.mic_unmute() == PHYSICAL_SOURCE
    assert ["wpctl", "set-mute", "620", "1"] in fake.calls
    assert ["wpctl", "set-mute", "620", "0"] in fake.calls


def test_monitoring_enable_disable(tmp_path: Any) -> None:
    """Мониторинг: module-loopback source -> default sink, unload по idx."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    route = dsp.monitoring_enable()
    assert route == f"{PHYSICAL_SOURCE} -> {PHYSICAL_SINK}"
    assert any(m[1] == "module-loopback" for m in fake.modules)
    assert dsp.state["monitor_module_idx"] == 100
    dsp.monitoring_disable()
    assert fake.modules == []
    assert dsp.state["monitor_enabled"] is False


def test_eq_loaded_reflects_sink_presence(tmp_path: Any) -> None:
    """eq_loaded: False до включения, True после, False после выключения."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    assert dsp.eq_loaded() is False
    dsp.enable_eq([0.0] * 10)
    assert dsp.eq_loaded() is True
    dsp.disable_eq()
    assert dsp.eq_loaded() is False


# ---------- M1: жизненный цикл холдера ----------


def test_holder_alive_after_enable_dead_after_disable(tmp_path: Any) -> None:
    """M1: enable держит живой Popen (stdin открыт), disable завершает его."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_eq([0.0] * 10)
    holder = dsp._eq_holder
    assert holder is not None
    assert holder.poll() is None
    assert holder.stdin.closed is False
    dsp.disable_eq()
    assert dsp._eq_holder is None
    assert holder.poll() is not None
    assert holder.stdin.closed is True


def test_holder_stuck_gets_sigkill_after_eof_timeout(tmp_path: Any) -> None:
    """M1 kill-ветка: холдер игнорирует EOF → wait истекает → kill()."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_eq([0.0] * 10)
    holder = dsp._eq_holder
    assert holder is not None
    holder.stuck = True  # pw-cli «завис»: EOF не завершает
    dsp.disable_eq()
    assert holder.poll() == -9  # убит сигналом
    assert EQ_NODE_INPUT not in fake.sinks  # ноды сняты


# ---------- HIGH-1/HIGH-2: посмертный граф и неудачное снятие ----------


def test_disable_eq_stale_graph_raises_and_leaves_all_alone(
    tmp_path: Any, monkeypatch: Any
) -> None:
    """HIGH-1: фильтр прошлого сеанса — AudioError, ничьё не тронуто.

    Никаких destroy/signals: чужой client с тем же/любым PID остаётся жив,
    state не фиксирует «выключено».
    """
    import os

    signals: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: signals.append((pid, sig)))
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    # Граф от «прошлого запуска»: ноды в графе, владелец — чужой процесс,
    # живого холдера у dsp нет; в state лежит посторонний pid.
    foreign = fake.spawn("load-module libpipewire-module-filter-chain stale")
    dsp._eq_holder = None
    dsp._set_state(eq_holder_pid=999999, eq_enabled=True)
    clients_before = dict(fake.clients)

    with pytest.raises(AudioError, match="прошлого сеанса") as excinfo:
        dsp.disable_eq()

    assert signals == []  # сигналов нет вовсе
    assert "pw-cli (pid" not in excinfo.value.hint  # непроверенный PID не называется
    assert "pw-dump" in excinfo.value.hint  # безопасная ручная инструкция на месте
    assert fake.clients == clients_before  # чужие client-глобалы не тронуты
    assert foreign.poll() is None  # чужой процесс жив
    assert EQ_NODE_INPUT in fake.sinks  # граф не тронут
    assert dsp.state["eq_enabled"] is True  # state не «выключен»
    assert dsp.state["eq_holder_pid"] == 999999


def test_disable_nr_stale_graph_raises_and_leaves_all_alone(tmp_path: Any) -> None:
    """HIGH-1 (NR): посмертный граф — AudioError с ручной инструкцией, state цел."""
    fake = FakeSubprocess(ldconfig="\tlibrnnoise_ladspa.so (libc)\n")
    dsp = _make_dsp(tmp_path, fake)
    foreign = fake.spawn("load-module libpipewire-module-filter-chain rnnoise stale")
    dsp._nr_holder = None
    dsp._set_state(nr_holder_pid=999999, nr_enabled=True)

    with pytest.raises(AudioError, match="прошлого сеанса") as excinfo:
        dsp.disable_nr()

    assert "pw-cli (pid" not in excinfo.value.hint
    assert "systemctl --user restart pipewire" in excinfo.value.hint
    assert foreign.poll() is None
    assert NR_NODE_OUTPUT in fake.sources
    assert dsp.state["nr_enabled"] is True
    assert dsp.state["nr_holder_pid"] == 999999


def test_disable_eq_node_left_behind_raises(tmp_path: Any, monkeypatch: Any) -> None:
    """HIGH-2: холдер убит, нода осталась в графе → AudioError, state цел.

    Сервер «не убрал» ноды умершего клиента (фейк-процесс перестаёт снимать
    ноды) — disable обязан НЕ писать enabled=False.
    """
    monkeypatch.setattr("mchose_hub.audio.dsp._SINK_POLL_TIMEOUT_S", 0.2)
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_eq([0.0] * 10)
    holder_pid = dsp._eq_holder.pid if dsp._eq_holder else None
    fake._process_exit = lambda proc: None  # имитация: ноды не исчезли

    with pytest.raises(AudioError, match="не снят"):
        dsp.disable_eq()

    assert dsp._eq_holder is None  # холдер остановлен
    assert EQ_NODE_INPUT in fake.sinks  # нода осталась
    assert dsp.state["eq_enabled"] is True  # «выключено» не зафиксировано
    assert dsp.state["eq_holder_pid"] == holder_pid


def test_disable_nr_node_left_behind_raises(tmp_path: Any, monkeypatch: Any) -> None:
    """HIGH-2 (NR): нода не исчезла после остановки холдера → AudioError."""
    monkeypatch.setattr("mchose_hub.audio.dsp._SINK_POLL_TIMEOUT_S", 0.2)
    fake = FakeSubprocess(ldconfig="\tlibrnnoise_ladspa.so (libc)\n")
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_nr()
    holder_pid = dsp._nr_holder.pid if dsp._nr_holder else None
    fake._process_exit = lambda proc: None  # ноды «залипли» в графе

    with pytest.raises(AudioError, match="не снят"):
        dsp.disable_nr()

    assert dsp.state["nr_enabled"] is True
    assert dsp.state["nr_holder_pid"] == holder_pid


def test_enable_eq_over_stale_graph_raises_without_spawning(tmp_path: Any) -> None:
    """HIGH-1: включение поверх посмертного графа — AudioError, холдер не создан."""
    fake = FakeSubprocess()
    dsp = _make_dsp(tmp_path, fake)
    fake.spawn("load-module libpipewire-module-filter-chain stale")
    dsp._eq_holder = None
    spawns_before = len(fake.holder_commands)

    with pytest.raises(AudioError, match="прошлого сеанса"):
        dsp.enable_eq([0.0] * 10)

    assert len(fake.holder_commands) == spawns_before  # холдер поверх сироты не запущен


def test_monitoring_failure_spares_all_modules(tmp_path: Any) -> None:
    """MEDIUM-2 финал: при сбое парсинга не выгружается НИЧЬИ модули."""
    fake = FakeSubprocess()
    fake.modules.append((500, "module-loopback", "source=other.source sink=other.sink"))
    fake.load_module_stdout = "неожиданный вывод pactl\n"
    dsp = _make_dsp(tmp_path, fake)
    with pytest.raises(AudioError, match="не удалось разобрать индекс модуля"):
        dsp.monitoring_enable()
    assert [m[0] for m in fake.modules] == [500, 100]  # чужой и наш живы
    assert not [c for c in fake.calls if c[:2] == ["pactl", "unload-module"]]
    assert dsp.state["monitor_enabled"] is False


def test_write_state_leaves_no_tmp_file(tmp_path: Any) -> None:
    """MEDIUM-1: атомарная запись не оставляет .tmp-мусор рядом с audio.json."""
    dsp = _make_dsp(tmp_path, FakeSubprocess())
    dsp._set_state(eq_enabled=True)
    state_dir = tmp_path
    assert (state_dir / "audio.json").exists()
    assert list(state_dir.glob("*.tmp")) == []


def test_stale_filter_hint_without_pid(tmp_path: Any) -> None:
    """HIGH-1: подсказка никогда не называет непроверенный PID."""
    dsp = _make_dsp(tmp_path, FakeSubprocess())
    hint = dsp._stale_filter_hint()
    assert "systemctl --user restart pipewire" in hint
    assert "pw-dump" in hint
    assert "pw-cli (pid" not in hint


# ---------- M3: раздельные холдеры EQ и NR ----------


def test_disable_eq_leaves_nr_holder_alive(tmp_path: Any) -> None:
    """M3: enable_eq → enable_nr → disable_eq: NR-холдер жив, снят только EQ."""
    fake = FakeSubprocess(ldconfig="\tlibrnnoise_ladspa.so (libc)\n")
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_eq([0.0] * 10)
    dsp.enable_nr()
    eq_holder = dsp._eq_holder
    nr_holder = dsp._nr_holder
    assert eq_holder is not None and nr_holder is not None
    assert eq_holder is not nr_holder
    dsp.disable_eq()
    assert nr_holder.poll() is None  # NR жив
    assert nr_holder.stdin.closed is False
    assert eq_holder.poll() is not None
    assert NR_NODE_OUTPUT in fake.sources  # NR-нода в графе
    assert dsp.state["nr_enabled"] is True
    assert dsp.state["eq_enabled"] is False


def test_disable_nr_leaves_eq_holder_alive(tmp_path: Any) -> None:
    """M3 (обратный порядок): enable_nr → enable_eq → disable_nr: EQ жив."""
    fake = FakeSubprocess(ldconfig="\tlibrnnoise_ladspa.so (libc)\n")
    dsp = _make_dsp(tmp_path, fake)
    dsp.enable_nr()
    dsp.enable_eq([0.0] * 10)
    eq_holder = dsp._eq_holder
    nr_holder = dsp._nr_holder
    assert eq_holder is not None and nr_holder is not None
    dsp.disable_nr()
    assert eq_holder.poll() is None  # EQ жив
    assert eq_holder.stdin.closed is False
    assert nr_holder.poll() is not None
    assert EQ_NODE_INPUT in fake.sinks  # EQ-нода в графе
    assert dsp.state["eq_enabled"] is True
    assert dsp.state["nr_enabled"] is False


# ---------- MI1: мониторинг без индекса модуля ----------


def test_monitoring_enable_unexpected_stdout_raises(tmp_path: Any) -> None:
    """MEDIUM-2 финал: сбой парсинга → AudioError с ручной инструкцией."""
    fake = FakeSubprocess()
    fake.load_module_stdout = "неожиданный вывод pactl\n"
    dsp = _make_dsp(tmp_path, fake)
    with pytest.raises(AudioError, match="не удалось разобрать индекс модуля") as exc:
        dsp.monitoring_enable()
    assert "pactl unload-module" in exc.value.message  # ручная инструкция
    assert "systemctl --user restart pipewire" in exc.value.message
    assert not [c for c in fake.calls if c[:2] == ["pactl", "unload-module"]]
    assert dsp.state["monitor_enabled"] is False
    assert dsp.state["monitor_module_idx"] is None


def test_monitoring_enable_no_unload_when_module_not_created(tmp_path: Any) -> None:
    """MEDIUM-2: модуль не создался (ноль новых) — всё равно без unload."""
    fake = FakeSubprocess()
    fake.load_module_spawn = 0
    fake.load_module_stdout = "неожиданный вывод pactl\n"
    dsp = _make_dsp(tmp_path, fake)
    with pytest.raises(AudioError, match="не удалось разобрать индекс модуля"):
        dsp.monitoring_enable()
    assert not [c for c in fake.calls if c[:2] == ["pactl", "unload-module"]]
    assert fake.modules == []
    assert dsp.state["monitor_enabled"] is False


def test_monitoring_enable_no_unload_when_multiple_created(tmp_path: Any) -> None:
    """MEDIUM-2: несколько новых модулей — без выгрузки (чужие не трогаем)."""
    fake = FakeSubprocess()
    fake.load_module_spawn = 2
    fake.load_module_stdout = "неожиданный вывод pactl\n"
    dsp = _make_dsp(tmp_path, fake)
    with pytest.raises(AudioError, match="не удалось разобрать индекс модуля"):
        dsp.monitoring_enable()
    assert [m[0] for m in fake.modules] == [100, 101]  # оба остались на месте
    assert not [c for c in fake.calls if c[:2] == ["pactl", "unload-module"]]
    assert dsp.state["monitor_enabled"] is False


# ---------- MI2: битый audio.json ----------


@pytest.mark.parametrize(
    ("overrides", "broken_key"),
    [
        ({"gains": ["a"] * 10}, "gains-строки"),
        ({"gains": [0.0] * 9}, "gains-длина"),
        ({"gains": [13.0] * 10}, "gains-диапазон"),
        ({"gains": [True, 0, 0, 0, 0, 0, 0, 0, 0, 0]}, "gains-bool"),
        ({"eq_enabled": "yes"}, "флаг-строка"),
        ({"eq_holder_pid": "4242"}, "pid-строка"),
        ({"nr_holder_pid": True}, "pid-bool"),
    ],
)
def test_load_state_corrupt_resets_and_rewrites(
    tmp_path: Any, overrides: dict[str, object], broken_key: str
) -> None:
    """MI2: любое нарушение схемы → дефолт в памяти и в перезаписанном файле."""
    stored = {**_default_state(), **overrides}
    path = tmp_path / "audio.json"
    path.write_text(json.dumps(stored), encoding="utf-8")
    dsp = PipeWireDSP(state_path=path)
    assert dsp.state == _default_state(), broken_key
    assert json.loads(path.read_text(encoding="utf-8")) == _default_state()


def test_load_state_valid_file_passes_through(tmp_path: Any) -> None:
    """MI2: валидный файл читается как есть (дефолтная перезапись не нужна)."""
    stored = {**_default_state(), "eq_enabled": True, "gains": [3.5] * 10}
    path = tmp_path / "audio.json"
    path.write_text(json.dumps(stored), encoding="utf-8")
    dsp = PipeWireDSP(state_path=path)
    assert dsp.state["eq_enabled"] is True
    assert dsp.state["gains"] == [3.5] * 10


# ---------- MEDIUM-1: конкурентная запись audio.json ----------


def test_concurrent_eq_nr_keep_state_file_valid(tmp_path: Any) -> None:
    """MEDIUM-1: конкурентные enable_eq + enable_nr — файл всегда валиден.

    Два воркера (audio.eq / audio.nr) пишут один audio.json: RLock
    сериализует read-modify-write, os.replace делает запись атомарной.
    20 итераций на свежем state-файле.
    """
    for i in range(20):
        fake = FakeSubprocess(ldconfig="\tlibrnnoise_ladspa.so (libc)\n")
        dsp = PipeWireDSP(state_path=tmp_path / str(i) / "audio.json")
        dsp._run = fake.run  # type: ignore[method-assign]
        dsp._spawn_holder = fake.spawn  # type: ignore[method-assign]
        threads = [
            threading.Thread(target=dsp.enable_eq, args=([0.0] * 10,)),
            threading.Thread(target=dsp.enable_nr),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        state_file = tmp_path / str(i) / "audio.json"
        stored = json.loads(state_file.read_text(encoding="utf-8"))
        assert stored["eq_enabled"] is True
        assert stored["nr_enabled"] is True
        assert isinstance(stored["eq_holder_pid"], int)
        assert isinstance(stored["nr_holder_pid"], int)
