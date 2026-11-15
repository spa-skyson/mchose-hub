#!/usr/bin/env bash
# Установка окружения mchose-hub: udev-правила + .desktop (идемпотентно).
# Опционально --entrypoint: скрипт запуска в ~/.local/bin/mchose-hub.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
RULES_SRC="$SCRIPT_DIR/99-mchose-hub.rules"
DESKTOP_SRC="$SCRIPT_DIR/mchose-hub.desktop"
RULES_DST="/etc/udev/rules.d/99-mchose-hub.rules"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP_DST="$DESKTOP_DIR/mchose-hub.desktop"
BIN_DIR="${HOME}/.local/bin"
BIN_DST="$BIN_DIR/mchose-hub"

if [[ ! -f "$RULES_SRC" ]]; then
  echo "Ошибка: не найден $RULES_SRC" >&2
  exit 1
fi
if [[ ! -f "$DESKTOP_SRC" ]]; then
  echo "Ошибка: не найден $DESKTOP_SRC" >&2
  exit 1
fi

# --- udev-правила (нужен root; через sudo, если скрипт запущен пользователем) ---
SUDO=""
if [[ "$(id -u)" -ne 0 ]]; then
  SUDO="sudo"
fi

echo "==> Копирование udev-правил в $RULES_DST"
$SUDO install -m 0644 "$RULES_SRC" "$RULES_DST"

echo "==> Перезагрузка правил udev"
if command -v udevadm >/dev/null 2>&1; then
  $SUDO udevadm control --reload
  $SUDO udevadm trigger
else
  echo "Предупреждение: udevadm не найден, правила применятся после перезагрузки." >&2
fi

# --- .desktop с подстановкой пути проекта вместо %PYTHONPATH% ---
echo "==> Установка .desktop в $DESKTOP_DST"
mkdir -p "$DESKTOP_DIR"
sed "s|%PYTHONPATH%|$PROJECT_DIR|g" "$DESKTOP_SRC" > "$DESKTOP_DST"
chmod 0644 "$DESKTOP_DST"

# --- опциональный entrypoint ---
if [[ "${1:-}" == "--entrypoint" ]]; then
  echo "==> Создание скрипта запуска $BIN_DST"
  mkdir -p "$BIN_DIR"
  cat > "$BIN_DST" <<EOF
#!/usr/bin/env bash
# Диспетчер: первый аргумент — CLI-подкоманда (status/mode/shutdown/prompt/
# light/eq/mic) → mchose_hub.cli; иначе (включая запуск без аргументов) — GUI.
CLI_CMDS="status mode shutdown prompt light eq mic"
if [[ \$# -gt 0 && " \$CLI_CMDS " == *" \$1 "* ]]; then
  exec env PYTHONPATH="$PROJECT_DIR\${PYTHONPATH:+:\$PYTHONPATH}" python3 -m mchose_hub.cli "\$@"
fi
exec env PYTHONPATH="$PROJECT_DIR\${PYTHONPATH:+:\$PYTHONPATH}" python3 -m mchose_hub.gui "\$@"
EOF
  chmod 0755 "$BIN_DST"
fi

echo "==> Готово. Запуск: python3 -m mchose_hub.gui (из $PROJECT_DIR) или через меню приложений."
