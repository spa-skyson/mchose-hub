"""CLI для отладки: python -m mchose_hub.cli {status,mode,shutdown,prompt,light,eq,mic}."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence

from .audio import (
    EQ_BANDS,
    GAIN_MAX,
    GAIN_MIN,
    PRESETS,
    AudioError,
    PipeWireDSP,
)
from .protocol import (
    AUDIO_MODE_NAMES,
    LIGHT_MODE_NAMES,
    PID_DOCK,
    PID_HEADSET_DONGLE,
    PID_HEADSET_WIRED,
    PROMPT_VOLUME_MAX,
    VID_MCHOSE,
    AudioMode,
    BatteryState,
    DockLighting,
)
from .service import DockService, HeadsetService
from .transport import TransportError, find_dock, find_dongle, scan_hidraw

_CLI_AUDIO_MODES = {
    "game1": AudioMode.GAME_1,
    "game2": AudioMode.GAME_2,
    "music": AudioMode.MUSIC,
}

_KNOWN_IDS: dict[tuple[int, int], str] = {
    (VID_MCHOSE, PID_DOCK): "док RGB (управляется)",
    (VID_MCHOSE, PID_HEADSET_DONGLE): "донгл 2.4G (управляется)",
    (VID_MCHOSE, PID_HEADSET_WIRED): "донгл V9 Turbo (управляется)",
    (0x291D, 0x385D): "донгл V9 Pro (другой протокол, не управляется)",
}


def _byte(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 255:
        raise argparse.ArgumentTypeError("ожидается число 0..255")
    return number


def _volume(value: str) -> int:
    number = int(value)
    if not 0 <= number <= PROMPT_VOLUME_MAX:
        raise argparse.ArgumentTypeError(f"ожидается число 0..{PROMPT_VOLUME_MAX}")
    return number


def _battery_label(battery: BatteryState) -> str:
    if battery.charging:
        return "заряжается"
    if battery.full:
        return "полный заряд"
    if battery.asleep:
        return "спит"
    return "разряд"


def _print_no_device(device: str) -> None:
    print(
        f"Ошибка: {device} не найден. Подключите устройство; при проблемах "
        "с доступом установите правила из packaging/99-mchose-hub.rules.",
        file=sys.stderr,
    )


def _cmd_status(_args: argparse.Namespace) -> int:
    devices = [
        (path, vid, pid) for path, vid, pid in scan_hidraw() if (vid, pid) in _KNOWN_IDS
    ]
    if not devices:
        print(
            "Устройство не найдено: не обнаружено ни донгла, ни дока MCHOSE.",
            file=sys.stderr,
        )
        return 1
    for path, vid, pid in devices:
        print(f"Найдено: {path} [{vid:04x}:{pid:04x}] — {_KNOWN_IDS[(vid, pid)]}")

    exit_code = 1
    dongle = find_dongle()
    if dongle is not None:
        try:
            service = HeadsetService(dongle)
            battery = service.poll_battery()
            mode = service.poll_audio_mode()
        finally:
            dongle.close()
        if battery is None and mode is None:
            print(
                "Донгл открыт, но наушники не ответили (выключены, спят "
                "или вне радиуса).",
                file=sys.stderr,
            )
        else:
            if battery is None:
                print("Батарею прочитать не удалось.", file=sys.stderr)
            else:
                print(f"Батарея: {battery.percent}% ({_battery_label(battery)})")
                exit_code = 0
            if mode is None:
                print("Режим звука прочитать не удалось.", file=sys.stderr)
            else:
                print(f"Режим звука: {AUDIO_MODE_NAMES[int(mode)]}")
                exit_code = 0

    dock = find_dock()
    if dock is not None:
        try:
            info = DockService(dock).read_device_info()
        finally:
            dock.close()
        if info is None:
            print(
                "Док открыт, но информацию устройства прочитать не удалось.",
                file=sys.stderr,
            )
        else:
            name = info.get("name", "имя неизвестно")
            version = info.get("version", "версия неизвестна")
            print(f"Док: {name}, прошивка: {version}")
            exit_code = 0
    return exit_code


def _cmd_mode(args: argparse.Namespace) -> int:
    dongle = find_dongle()
    if dongle is None:
        _print_no_device("Донгл наушников")
        return 1
    try:
        actual = HeadsetService(dongle).set_audio_mode(_CLI_AUDIO_MODES[args.value])
    finally:
        dongle.close()
    if actual is None:
        print("Команда отправлена, но наушники не подтвердили режим.", file=sys.stderr)
        return 1
    print(f"Режим звука: {AUDIO_MODE_NAMES[int(actual)]}")
    return 0


def _cmd_shutdown(args: argparse.Namespace) -> int:
    dongle = find_dongle()
    if dongle is None:
        _print_no_device("Донгл наушников")
        return 1
    try:
        service = HeadsetService(dongle)
        accepted = service.set_auto_shutdown(args.minutes)
        current = service.get_auto_shutdown()
    finally:
        dongle.close()
    if not accepted:
        print("Устройство не подтвердило установку.", file=sys.stderr)
        return 1
    if current is None:
        print("Установлено, но подтвердить чтением не удалось.", file=sys.stderr)
        return 1
    print(f"Автоотключение: {current} мин")
    return 0


def _cmd_prompt(args: argparse.Namespace) -> int:
    dongle = find_dongle()
    if dongle is None:
        _print_no_device("Донгл наушников")
        return 1
    try:
        service = HeadsetService(dongle)
        accepted = service.set_prompt_volume(args.level)
        current = service.get_prompt_volume()
    finally:
        dongle.close()
    if not accepted:
        print("Устройство не подтвердило установку.", file=sys.stderr)
        return 1
    if current is None:
        print("Установлено, но подтвердить чтением не удалось.", file=sys.stderr)
        return 1
    print(f"Громкость подсказок: {current}")
    return 0


def _print_light_info(info: DockLighting) -> None:
    print(f"Подсветка: {'вкл' if info.enabled else 'выкл'}")
    print(f"Режим: {info.mode} — {LIGHT_MODE_NAMES.get(info.mode, 'неизвестно')}")
    print(f"Эффектов в устройстве: {info.mode_count}")
    print(f"Скорость: {info.speed}, яркость: {info.brightness}")
    print(f"Светомузыка: {'вкл' if info.light_sync else 'выкл'}")
    color = ", ".join(str(item) for item in info.base_color)
    print(f"Базовый цвет: RGB({color})")
    direction = "по часовой" if info.direction == 0 else "против часовой"
    print(f"Направление Flowing Wave: {direction}")
    if info.sleep_enabled is not None:
        sleep = "вкл" if info.sleep_enabled else "выкл"
        minutes = f", {info.sleep_time} мин" if info.sleep_time is not None else ""
        print(f"Сон дока: {sleep}{minutes}")
    if info.version is not None:
        print(f"Версия (глобальная): {info.version}")
    print("Эффекты:")
    for mode_id, name in LIGHT_MODE_NAMES.items():
        print(f"  {int(mode_id)} — {name}")


def _cmd_light(args: argparse.Namespace) -> int:
    dock = find_dock()
    if dock is None:
        _print_no_device("Док (RGB-база)")
        return 1
    try:
        service = DockService(dock)
        match args.light_command:
            case "info":
                info = service.read_global_info()
                if info is None:
                    print(
                        "Док открыт, но данные подсветки прочитать не удалось.",
                        file=sys.stderr,
                    )
                    return 1
                _print_light_info(info)
                return 0
            case "on":
                service.set_light_switch(True)
                print("Подсветка включена")
                return 0
            case "off":
                service.set_light_switch(False)
                print("Подсветка выключена")
                return 0
            case "mode":
                service.set_light_mode(args.id)
                print(
                    f"Режим подсветки: {args.id} — "
                    f"{LIGHT_MODE_NAMES.get(args.id, 'неизвестно')}"
                )
                return 0
            case "color":
                service.set_base_color((args.r, args.g, args.b))
                print(f"Базовый цвет: RGB({args.r}, {args.g}, {args.b})")
                return 0
            case "brightness":
                service.set_light_brightness(args.mode, args.value)
                print(f"Яркость режима {args.mode}: {args.value}")
                return 0
            case "speed":
                service.set_light_speed(args.value)
                print(f"Скорость эффекта: {args.value}")
                return 0
            case "direction":
                # Атомарный read-modify-write внутри сервиса (одна блокировка):
                # параллельная команда не вклинится между чтением и записью.
                direction = 0 if args.value == "cw" else 1
                if not service.set_direction(direction):
                    print("Не удалось прочитать состояние дока.", file=sys.stderr)
                    return 1
                label = "по часовой" if direction == 0 else "против часовой"
                print(f"Направление Flowing Wave: {label}")
                return 0
            case _:
                return 1
    finally:
        dock.close()


def _cmd_eq(args: argparse.Namespace) -> int:
    dsp = PipeWireDSP()
    try:
        match args.eq_command:
            case "on":
                gains = dsp.state.get("gains")
                name = dsp.enable_eq(
                    gains if isinstance(gains, list) else PRESETS["Flat"]
                )
                print(f"EQ включён: {name} (default переключён)")
                return 0
            case "off":
                physical = dsp.disable_eq()
                print(f"EQ выключен, default sink: {physical}")
                return 0
            case "preset":
                name = args.name
                if name not in PRESETS:
                    print(
                        f"Неизвестный пресет: {name}. Доступны: {', '.join(PRESETS)}",
                        file=sys.stderr,
                    )
                    return 1
                gains = PRESETS[name]
                if dsp.eq_loaded():
                    dsp.set_eq_gains(gains)
                    print(f"Пресет применён: {name}")
                else:
                    dsp.enable_eq(gains)
                    print(f"EQ включён с пресетом: {name}")
                return 0
            case "set":
                band = args.band
                if not 1 <= band <= len(EQ_BANDS):
                    print(
                        f"Полоса 1..{len(EQ_BANDS)}; {band} вне диапазона",
                        file=sys.stderr,
                    )
                    return 1
                current = dsp.state.get("gains")
                gains = [
                    float(g)
                    for g in (current if isinstance(current, list) else PRESETS["Flat"])
                ]
                gains[band - 1] = args.gain
                if dsp.eq_loaded():
                    dsp.set_eq_gains(gains)
                    print(
                        f"Полоса {band} ({EQ_BANDS[band - 1][1]}): {args.gain:+.1f} дБ"
                    )
                else:
                    dsp.enable_eq(gains)
                    print(
                        f"EQ включён; полоса {band} ({EQ_BANDS[band - 1][1]}): "
                        f"{args.gain:+.1f} дБ"
                    )
                return 0
            case "status":
                _print_eq_status(dsp)
                return 0
            case _:
                return 1
    except AudioError as exc:
        print(f"Ошибка: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"Подсказка: {exc.hint}", file=sys.stderr)
        return 1


def _print_eq_status(dsp: PipeWireDSP) -> None:
    sink = dsp.find_sink()
    print(f"MCHOSE sink: {sink or 'не найден'}")
    loaded = dsp.eq_loaded()
    print(f"EQ: {'вкл' if loaded else 'выкл'}")
    if loaded:
        gains = dsp.state.get("gains")
        if isinstance(gains, list) and len(gains) == len(EQ_BANDS):
            for (freq, label), gain in zip(EQ_BANDS, gains):
                print(f"  {label}: {float(gain):+.1f} дБ")
    default = dsp._default_of("sink")
    print(f"Default sink: {default or 'не определён'}")


def _cmd_mic(args: argparse.Namespace) -> int:
    dsp = PipeWireDSP()
    try:
        match args.mic_command:
            case "volume":
                percent = dsp.mic_volume(args.percent)
                print(f"Громкость микрофона: {percent}%")
                return 0
            case "mute":
                print(f"Микрофон заглушен: {dsp.mic_mute()}")
                return 0
            case "unmute":
                print(f"Микрофон активен: {dsp.mic_unmute()}")
                return 0
            case "nr":
                if args.nr_state == "on":
                    name = dsp.enable_nr()
                    print(f"Шумоподавление включено: {name}")
                else:
                    physical = dsp.disable_nr()
                    print(f"Шумоподавление выключено, source: {physical}")
                return 0
            case "monitor":
                if args.monitor_state == "on":
                    route = dsp.monitoring_enable()
                    print(f"Мониторинг включён: {route}")
                else:
                    print(dsp.monitoring_disable())
                return 0
            case "status":
                _print_mic_status(dsp)
                return 0
            case _:
                return 1
    except AudioError as exc:
        print(f"Ошибка: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"Подсказка: {exc.hint}", file=sys.stderr)
        return 1


def _print_mic_status(dsp: PipeWireDSP) -> None:
    source = dsp.find_source()
    print(f"MCHOSE source: {source or 'не найден (микрофон не активен)'}")
    print(
        f"Шумоподавление: {'вкл' if dsp.nr_loaded() else 'выкл'} (RNNoise {'есть' if dsp.nr_supported() else 'нет'})"
    )
    print(f"Мониторинг: {'вкл' if dsp.monitoring_active() else 'выкл'}")
    if source is not None:
        node_id = dsp._node_id(source)
        if node_id is not None:
            result = dsp._run(["wpctl", "get-volume", str(node_id)])
            print(f"Громкость: {result.stdout.strip()}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mchose-hub",
        description="Управление MCHOSE V9 Turbo+ (донгл 2.4G и RGB-док).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="устройства, батарея, режим звука")
    status.set_defaults(func=_cmd_status)

    mode = sub.add_parser("mode", help="режим звука наушников")
    mode.add_argument("value", choices=sorted(_CLI_AUDIO_MODES))
    mode.set_defaults(func=_cmd_mode)

    shutdown = sub.add_parser("shutdown", help="автоотключение наушников")
    shutdown.add_argument("minutes", type=int, choices=[15, 30, 45])
    shutdown.set_defaults(func=_cmd_shutdown)

    prompt = sub.add_parser("prompt", help="громкость голосовых подсказок")
    prompt.add_argument("level", type=_volume)
    prompt.set_defaults(func=_cmd_prompt)

    light = sub.add_parser("light", help="подсветка RGB-дока")
    light_sub = light.add_subparsers(dest="light_command", required=True)
    light_sub.add_parser("info", help="состояние подсветки и таблица эффектов")
    light_sub.add_parser("on", help="включить подсветку")
    light_sub.add_parser("off", help="выключить подсветку")
    light_mode = light_sub.add_parser("mode", help="режим подсветки (0-5)")
    light_mode.add_argument("id", type=int)
    light_color = light_sub.add_parser("color", help="базовый цвет R G B")
    for axis in ("r", "g", "b"):
        light_color.add_argument(axis, type=_byte)
    light_brightness = light_sub.add_parser(
        "brightness", help="яркость: режим и уровень 0–4 (>=5 устройство режет до 4)"
    )
    light_brightness.add_argument("mode", type=int)
    light_brightness.add_argument("value", type=_byte)
    light_speed = light_sub.add_parser("speed", help="скорость эффекта")
    light_speed.add_argument("value", type=_byte)
    light_direction = light_sub.add_parser("direction", help="направление Flowing Wave")
    light_direction.add_argument("value", choices=["cw", "ccw"])
    light.set_defaults(func=_cmd_light)

    eq = sub.add_parser("eq", help="эквалайзер (PipeWire filter-chain)")
    eq_sub = eq.add_subparsers(dest="eq_command", required=True)
    eq_sub.add_parser("on", help="включить EQ (виртуальный sink)")
    eq_sub.add_parser("off", help="выключить EQ, вернуть default sink")
    eq_preset = eq_sub.add_parser("preset", help="применить пресет")
    eq_preset.add_argument("name", choices=sorted(PRESETS))
    eq_set = eq_sub.add_parser("set", help="гейн полосы 1-10 (дБ)")
    eq_set.add_argument("band", type=int, help="номер полосы 1-10 (31 Hz..16 kHz)")
    eq_set.add_argument("gain", type=float, help=f"гейн {GAIN_MIN}..{GAIN_MAX} дБ")
    eq_sub.add_parser("status", help="состояние EQ")
    eq.set_defaults(func=_cmd_eq)

    mic = sub.add_parser("mic", help="микрофон наушников (PipeWire)")
    mic_sub = mic.add_subparsers(dest="mic_command", required=True)
    mic_volume = mic_sub.add_parser(
        "volume", help="громкость микрофона 0-100 (без арг. — текущая)"
    )
    mic_volume.add_argument("percent", nargs="?", type=int)
    mic_sub.add_parser("mute", help="заглушить микрофон")
    mic_sub.add_parser("unmute", help="включить микрофон")
    mic_nr = mic_sub.add_parser("nr", help="шумоподавление RNNoise")
    mic_nr.add_argument("nr_state", choices=["on", "off"])
    mic_monitor = mic_sub.add_parser("monitor", help="мониторинг микрофона в наушники")
    mic_monitor.add_argument("monitor_state", choices=["on", "off"])
    mic_sub.add_parser("status", help="состояние микрофона")
    mic.set_defaults(func=_cmd_mic)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Точка входа CLI; транспортные ошибки превращает в сообщения."""
    args = _build_parser().parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.func
    try:
        return handler(args)
    except TransportError as exc:
        print(f"Ошибка: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"Подсказка: {exc.hint}", file=sys.stderr)
        return 1
    except AudioError as exc:
        print(f"Ошибка: {exc.message}", file=sys.stderr)
        if exc.hint:
            print(f"Подсказка: {exc.hint}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — CLI не должен показывать traceback
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
