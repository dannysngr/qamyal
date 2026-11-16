#!/bin/bash
# Qamyal для macOS: обновление изнутри программы (кнопка «Обновить» на странице «О приложении»).
# Качает новый образ через curl — так у него нет пометки «скачано из интернета», и macOS не спрашивает
# разрешения заново; ставит Qamyal.app на место старого и запускает. При первом запуске новая версия
# сама обновит ~/Qamyal (setup.sh).
# Запуск: update.sh <адрес образа> <путь к Qamyal.app> [файл хода] [PID окна] [PID программы]
# Файл хода (JSON: step = download | install | restart | done | error) читает окно — показывает, что происходит.
set -uo pipefail
URL="$1"
APP="$2"
STATUS="${3:-}"
APP_PID="${4:-}"
PY_PID="${5:-}"
LOG="$HOME/Qamyal/update.log"
exec >>"$LOG" 2>&1
echo "=== $(date) обновление: $URL (окно $APP_PID, программа $PY_PID)"
TMP="$(mktemp -d)"

say() {                                  # шаг и, для ошибки, её текст (без кавычек)
    echo "шаг: $1 ${2:-}"
    [[ -n "$STATUS" ]] || return 0
    printf '{"step": "%s", "error": "%s", "at": %s}\n' "$1" "${2:-}" "$(date +%s)" >"$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
}
REOPEN=""
fail() {                                 # окно уже закрыли — открываем прежнюю версию, чтобы не остаться без программы
    echo "ОШИБКА: $1"; say error "$1"; rm -rf "$TMP"
    [[ -n "$REOPEN" ]] && open "$APP"
    exit 1
}
alive() { [[ -n "$1" ]] && kill -0 "$1" 2>/dev/null; }
wait_gone() {                            # ждём, пока процесс $1 завершится, до $2 секунд
    for _ in $(seq 1 "$2"); do alive "$1" || return 0; sleep 1; done
    ! alive "$1"
}

say download
curl -fsSL --retry 3 --connect-timeout 20 --max-time 600 -o "$TMP/qamyal.dmg" "$URL" \
    || fail "Новая версия не скачалась — проверьте интернет"

say install
MNT="$(hdiutil attach -nobrowse -readonly -noautoopen "$TMP/qamyal.dmg" | awk -F'\t' '/\/Volumes\// {print $NF}' | tail -1)"
[[ -d "$MNT/Qamyal.app" ]] || fail "В скачанном образе нет Qamyal.app"
rm -rf "$APP.new"
ditto "$MNT/Qamyal.app" "$APP.new" || { hdiutil detach "$MNT" -quiet; fail "Не получилось скопировать новую версию"; }
hdiutil detach "$MNT" -quiet || true
xattr -cr "$APP.new" 2>/dev/null || true

say restart
REOPEN=1
# закрываем окно: по номеру процесса (путь к программе мог поменяться), иначе — по пути; не закрылось — принудительно.
# Python, увидев, что окна нет, выходит сам (запись в это время не идёт: обновление запускается только без неё)
if alive "$APP_PID"; then
    kill -TERM "$APP_PID" 2>/dev/null
    wait_gone "$APP_PID" 15 || { echo "окно не закрылось — закрываю принудительно"; kill -KILL "$APP_PID" 2>/dev/null; }
fi
pkill -TERM -f "$APP/Contents/MacOS/Qamyal" 2>/dev/null || true
for _ in $(seq 1 15); do pgrep -f "$APP/Contents/MacOS/Qamyal" >/dev/null || break; sleep 1; done
pkill -KILL -f "$APP/Contents/MacOS/Qamyal" 2>/dev/null || true
if alive "$PY_PID"; then
    wait_gone "$PY_PID" 20 || { kill -TERM "$PY_PID" 2>/dev/null; wait_gone "$PY_PID" 10 || kill -KILL "$PY_PID" 2>/dev/null; }
fi

rm -rf "$APP.old"
mv "$APP" "$APP.old" && mv "$APP.new" "$APP" || { mv "$APP.old" "$APP" 2>/dev/null; fail "Не получилось заменить программу"; }
rm -rf "$APP.old" "$TMP"
echo "готово, запускаю"
say done
open "$APP" || open -a Qamyal
