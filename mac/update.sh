#!/bin/bash
# Qamyal для macOS: обновление изнутри программы (кнопка «Обновить» на странице «О приложении»).
# Качает новый образ через curl — так у него нет пометки «скачано из интернета», и macOS не спрашивает
# разрешения заново; ставит Qamyal.app на место старого и запускает. При первом запуске новая версия
# сама обновит ~/Qamyal (setup.sh). Запуск: update.sh <адрес образа> <путь к Qamyal.app>
set -uo pipefail
URL="$1"
APP="$2"
LOG="$HOME/Qamyal/update.log"
exec >>"$LOG" 2>&1
echo "=== $(date) обновление: $URL"
TMP="$(mktemp -d)"
fail() { echo "ОШИБКА: $1"; rm -rf "$TMP"; exit 1; }

curl -fsSL --retry 3 --max-time 600 -o "$TMP/qamyal.dmg" "$URL" || fail "образ не скачался"
MNT="$(hdiutil attach -nobrowse -readonly -noautoopen "$TMP/qamyal.dmg" | awk -F'\t' '/\/Volumes\// {print $NF}' | tail -1)"
[[ -d "$MNT/Qamyal.app" ]] || fail "в образе нет Qamyal.app"
rm -rf "$APP.new"
ditto "$MNT/Qamyal.app" "$APP.new" || fail "не получилось скопировать"
hdiutil detach "$MNT" -quiet || true
xattr -cr "$APP.new" 2>/dev/null || true

# закрываем окно Qamyal — Python замечает это и выходит сам; ждём до 30 секунд
pkill -TERM -f "$APP/Contents/MacOS/Qamyal" 2>/dev/null || true
for _ in $(seq 1 30); do pgrep -f "$APP/Contents/MacOS/Qamyal" >/dev/null || break; sleep 1; done
for _ in $(seq 1 15); do pgrep -f "$HOME/Qamyal/app/app.py" >/dev/null || break; sleep 1; done

rm -rf "$APP.old"
mv "$APP" "$APP.old" && mv "$APP.new" "$APP" || { mv "$APP.old" "$APP" 2>/dev/null; fail "не получилось заменить программу"; }
rm -rf "$APP.old" "$TMP"
echo "готово, запускаю"
open "$APP"
