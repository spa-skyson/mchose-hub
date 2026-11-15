"""Тесты dsp.py: PipeWireDSP поверх FakeSubprocess (состояния pactl/wpctl/pw-cli)."""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

from mchose_hub.audio import AudioError, PipeWireDSP
from mchose_hub.audio.dsp import NR_NODE_OUTPUT, RNNOISE_HINT
from mchose_hub.audio.eq import EQ_NODE_INPUT

PHYSICAL_SINK = (
    "alsa_output.usb-C-Media_Electronics_Inc_MCHOSE_V9_Turbo__0123456789AB-01."
    "analog-stereo"
)
PHYSICAL_SOURCE = (
    "alsa_input.usb-C-Media_Electronics_Inc_MCHOSE_V9_Turbo__0123456789AB-01."
    "mono-fallback"
)


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
        self.holder_pid: int | None = None
        self.nodes: dict[str, int] = {PHYSICAL_SINK: 619, PHYSICAL_SOURCE: 620}
        self.enum_gains: list[float] = [0.0] * 10
        self.volume: float = 0.50
        self.ldconfig = ldconfig
        self.apply_setparam = apply_setparam
        self.next_module_idx = 100

    # --- точки подмены dsp ---

    def run(self, cmd: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, self._handle(cmd), "")

    def spawn(self, command: str) -> int:
        self.holder_commands.append(command)
        self.holder_pid = 42424
        for name in (EQ_NODE_INPUT, "effect_output.mchose_eq", NR_NODE_OUTPUT):
            self.nodes[name] = 900 + len(self.nodes)
        self.sinks.append(EQ_NODE_INPUT)
        self.sources.append(NR_NODE_OUTPUT)
        return self.holder_pid

    def kill(self) -> None:
        self.holder_pid = None
        self.sinks = [s for s in self.sinks if s != EQ_NODE_INPUT]
        self.sources = [s for s in self.sources if s != NR_NODE_OUTPUT]
        for name in (EQ_NODE_INPUT, "effect_output.mchose_eq"):
            self.nodes.pop(name, None)
        self.nodes.pop(NR_NODE_OUTPUT, None)

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
                idx = self.next_module_idx
                self.next_module_idx += 1
                self.modules.append((idx, "module-loopback", " ".join(args)))
                return f"New module index: {idx}\n"
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
                return json.dumps(
                    [
                        {"id": node_id, "info": {"props": {"node.name": name}}}
                        for name, node_id in self.nodes.items()
                    ]
                )
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

    def kill() -> None:
        fake.kill()
        dsp._set_state(holder_pid=None)

    dsp._kill_holder = kill  # type: ignore[method-assign]
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
    assert dsp.state["holder_pid"] == 42424


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
