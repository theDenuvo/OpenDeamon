#!/bin/sh
# Супервизор круглосуточного эндпоинта кодинга.
#
# systemd и tmux на сервере недоступны без sudo и linger - это проверено:
# `systemctl` отдаёт "Failed to connect to user scope bus", `tmux ls` -
# "error connecting to /tmp/tmux-1000/default". Значит единственный способ
# держать процесс - свой супервизор, и он отвечает за три вещи, которые
# обычно даёт init-система:
#
#   1. ПОДЪЁМ ПОСЛЕ ПАДЕНИЯ. Цикл: поднять, опросить /health, при отказе
#      снять процесс и поднять снова. Проверка живости - именно /health,
#      а не «процесс существует»: процесс может висеть и не отвечать.
#   2. ЗАПРЕТ ВТОРОГО ЭКЗЕМПЛЯРА. PID-файл плюс `kill -0`. Два
#      экземпляра означали бы две очереди к одному репозиторию и две
#      сессии на одну задачу - ровно то, ради чего нужна идемпотентность.
#      Если живой экземпляр есть, второй СУПЕРВИЗОР не запускается.
#   3. ЧЕСТНЫЙ ЛОГ. Всё в stderr супервизора и в $LOG. Молчаливый
#      перезапуск - это сервис, который не работает и об этом не знает.
#
# Переменные окружения (значения по умолчанию - в коде, секретов здесь нет):
#   OC_SERVICE_HOST, OC_SERVICE_PORT  - где слушать
#   OC_STATE_DIR                      - состояние (jobs.json, pid)
#   OC_LOG                            - куда писать
#   OC_RESTART_DELAY                  - пауза перед рестартом, секунды
#   OC_HEALTH_TIMEOUT                 - сколько ждать ответа /health
#   OC_MAX_RESTARTS                   - предохранитель: столько рестартов
#                                       подряд и супервизор сдаётся громко,
#                                       вместо того чтобы крутить пустой цикл

set -eu

HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=$(dirname "$HERE")

HOST=${OC_SERVICE_HOST:-127.0.0.1}
PORT=${OC_SERVICE_PORT:-8791}
STATE_DIR=${OC_STATE_DIR:-$HOME/.local/state/opendeamon-oc}
LOG=${OC_LOG:-$STATE_DIR/supervisor.log}
PIDFILE=$STATE_DIR/service.pid
LOCKFILE=$STATE_DIR/supervisor.lock
RESTART_DELAY=${OC_RESTART_DELAY:-5}
HEALTH_TIMEOUT=${OC_HEALTH_TIMEOUT:-5}
MAX_RESTARTS=${OC_MAX_RESTARTS:-10}

PYTHON=${OC_PYTHON:-/home/server/venv/bin/python}
[ -x "$PYTHON" ] || PYTHON=python3

mkdir -p "$STATE_DIR"

say() {
    # Всё, что здесь пишется, - это оператору, а не в лог задачи.
    printf '%s supervisor: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >&2
    printf '%s supervisor: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$LOG" 2>/dev/null || true
}

# --- запрет второго экземпляра -------------------------------------------
# flock не всегда есть, поэтому основной механизм - живой PID, а flock -
# # подстраховка от гонки двух запусков в одну секунду.
if [ -f "$PIDFILE" ]; then
    old=$(cat "$PIDFILE" 2>/dev/null || echo "")
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
        say "refusing to start: service pid $old is already alive"
        exit 3
    fi
    say "stale pidfile ($old is gone), continuing"
    rm -f "$PIDFILE"
fi

if command -v flock >/dev/null 2>&1; then
    # shellcheck disable=SC2069
    exec 9>"$LOCKFILE"
    if ! flock -n 9; then
        say "refusing to start: another supervisor holds $LOCKFILE"
        exit 3
    fi
fi

CHILD=""

cleanup() {
    trap - TERM INT EXIT
    if [ -n "$CHILD" ] && kill -0 "$CHILD" 2>/dev/null; then
        say "stopping service pid $CHILD"
        kill -TERM "$CHILD" 2>/dev/null || true
        wait "$CHILD" 2>/dev/null || true
    fi
    rm -f "$PIDFILE"
    say "supervisor stopped"
}
trap cleanup TERM INT EXIT

health() {
    curl -fsS -m "$HEALTH_TIMEOUT" "http://$HOST:$PORT/health" 2>/dev/null
}

say "starting: host=$HOST port=$PORT python=$PYTHON state=$STATE_DIR"

restarts=0
while :; do
    "$PYTHON" "$HERE/service.py" --host "$HOST" --port "$PORT" &
    CHILD=$!
    echo "$CHILD" > "$PIDFILE"
    say "service up, pid $CHILD"

    # Ждём первый успешный /health, а не просто факт запуска: сервис без
    # учётных данных падает сразу, и это должно быть видно в логе супервизора,
    # а не молчаливый цикл рестартов.
    ok=0
    i=0
    while [ "$i" -lt 30 ]; do
        if ! kill -0 "$CHILD" 2>/dev/null; then
            break
        fi
        if health >/dev/null 2>&1; then
            ok=1
            break
        fi
        i=$((i + 1))
        sleep 1
    done

    if [ "$ok" -eq 1 ]; then
        restarts=0
        while :; do
            sleep 5
            if ! kill -0 "$CHILD" 2>/dev/null; then
                say "service process $CHILD exited"
                break
            fi
            if health >/dev/null 2>&1; then
                continue
            fi
            # Процесс жив, но не отвечает: это тоже падение.
            say "health check failed while pid $CHILD is alive; restarting"
            kill -TERM "$CHILD" 2>/dev/null || true
            wait "$CHILD" 2>/dev/null || true
            break
        done
    else
        say "service never became healthy; see its output above"
        wait "$CHILD" 2>/dev/null || true
    fi

    rm -f "$PIDFILE"
    CHILD=""
    restarts=$((restarts + 1))
    if [ "$restarts" -ge "$MAX_RESTARTS" ]; then
        say "GIVING UP after $restarts consecutive failed starts. This is a "\
"configuration problem, not a crash; the supervisor will not loop on it."
        exit 4
    fi
    say "restarting in ${RESTART_DELAY}s ($restarts/$MAX_RESTARTS)"
    sleep "$RESTART_DELAY"
done