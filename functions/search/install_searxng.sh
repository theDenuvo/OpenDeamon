#!/usr/bin/env bash
# Установка SearXNG НА ДИСКЕ, переживающая перезагрузку.
#
# ЗАЧЕМ ЭТОТ СКРИПТ, А НЕ «УСТАНОВИЛ РУКАМИ ОДИН РАЗ».
#
# Живая половина приёмки A6 - четыре группы в functions/search/test_search.py -
# проверяет настоящий эндпоинт: /search отдаёт непустой results, интерфейс
# воспроизводит ответ этого эндпоинта, а слушатель сидит на петле. Пока
# SearXNG нет, эти группы объявляются пропуском, и это честно, но тогда
# «проверено» и «проверяемо завтра» - разные вещи.
#
# Так случилось: первый SearXNG стоял в /tmp/opencode/sx-venv, а /tmp здесь
# tmpfs на 2 ГБ. После перезагрузки tmpfs обнулился, и живая половина приёмки
# стала непроверяемой без причины, которую видно.
#
# Поэтому установка обязана быть: (1) воспроизводимой - скрипт в репозитории,
#     а не память оператора; (2) на диске, а не в tmpfs; (3) с ЗАКРЕПЛЁННОЙ
#     версией, потому что «latest» через полгода - это другой продукт в
#     строке, которой раньше не было.
#
# КУДА СТАВИТСЯ И ПОЧЕМУ НЕ В РЕПОЗИТОРИЙ
#
#   ${HERMES_SEARXNG_PREFIX:-/home/server/projects/opendeamon-runtime/searxng}
#
# Это каталог рядом с репозиторием, но ВНЕ его: исходники SearXNG плюс venv -
# сотни мегабайт, и тащить их в git незачем. Каталог на /home/server/projects,
# то есть на /dev/sda2, а не на tmpfs, - поэтому переживает перезагрузку.
# Скрипт отказывается ставить в /tmp и проверяет, что префикс не на tmpfs.
#
# СЕКРЕТ
#
# Никаких ключей в репозитории. Ключ лежит в $SECRET_ENV (по умолчанию
# /home/server/projects/opendeamon-runtime/searxng.env), права 600, вне git.
# Скрипт НЕ генерирует и НЕ подставляет секрет: он проверяет, что файл есть и
# что ключ в нём уже есть, и отказывается работать иначе. Сгенерировать ключ -
# одноразовая операция владельца, и молчаливая подстановка заглушки означала
# бы, что сервис поднялся с секретом, который кто-то ещё знает.
#
# ИСПОЛЬЗОВАНИЕ
#
#   bash functions/search/install_searxng.sh              # поставить/проверить
#   bash functions/search/install_searxng.sh --systemd    # + включить автозапуск
#
# --systemd требует root (нужен для /etc/systemd/system и systemctl enable).
# Всё остальное работает от обычного пользователя.
set -euo pipefail

PREFIX="${HERMES_SEARXNG_PREFIX:-/home/server/projects/opendeamon-runtime/searxng}"
SECRET_ENV="${HERMES_SEARXNG_SECRET_ENV:-/home/server/projects/opendeamon-runtime/searxng.env}"
REPO="${HERMES_REPO:-/home/server/projects/opendeamon}"
SETTINGS="$REPO/searxng/searxng/settings.yml"
PYTHON_BASE="${HERMES_PYTHON_BASE:-/home/server/venv/bin/python}"
UPSTREAM="https://github.com/searxng/searxng.git"

# ЗАКРЕПЛЁННЫЙ КОМИТ. Не тег и не ветка: коммит - единственное имя, которое
# нельзя переназначить. Проверено 2026-10-05: это masterupstream, версия
# 2026.10.4, python_requires >= 3.10.
SEARXNG_PIN="d48c4b555421e824342c51d68482dd0898e54d0f"

WITH_SYSTEMD=0
[ "${1:-}" = "--systemd" ] && WITH_SYSTEMD=1

say() { printf '%s\n' "$*" >&2; }
die() { printf 'installer: %s\n' "$*" >&2; exit 1; }

# ── проверки, которые обязаны сработать ДО установки ──────────────────────

[ -x "$PYTHON_BASE" ] || die "нет интерпретатора $PYTHON_BASE (переопредели HERMES_PYTHON_BASE)"

case "$PREFIX" in
  /tmp|/tmp/*)
    die "PREFIX=$PREFIX попадает в tmpfs, а установка обязана пережить перезагрузку.
      Скрипт отказывается ставить в /tmp именно из-за того, из-за чего эта
      установка и нужна. Возьми каталог на диске."
    ;;
esac

# Проверка «это диск, а не память»: mount point даёт тип файловой системы.
probe_dir="$(dirname "$PREFIX")"
mkdir -p "$probe_dir" 2>/dev/null || true
fstype="$(stat -f -c %T "$probe_dir" 2>/dev/null || echo unknown)"
case "$fstype" in
  tmpfs) die "$probe_dir смонтирован как tmpfs - установка там не переживёт перезагрузку" ;;
esac

[ -f "$SETTINGS" ] || die "нет $SETTINGS - настройки лежат в репозитории, без них нечего запускать"

if [ ! -f "$SECRET_ENV" ]; then
  say "секрета нет: $SECRET_ENV"
  say "сгенерируйте ОДИН раз (вне репозитория) и повторите:"
  say "  install -d -m 700 $(dirname "$SECRET_ENV")"
  say "  umask 077; printf 'SEARXNG_SECRET=%s\n' \"\$(openssl rand -hex 32)\" > $SECRET_ENV"
  exit 1
fi
grep -q '^SEARXNG_SECRET=..' "$SECRET_ENV" \
  || die "$SECRET_ENV не содержит непустой SEARXNG_SECRET"

chmod 600 "$SECRET_ENV" 2>/dev/null || true

# ── установка ─────────────────────────────────────────────────────────────

# Идемпотентность: «уже стоит закреплённый коммит» - это не только git.
#
# Первая версия спрашивала `git -C src rev-parse HEAD`, и это ломалось при
# запуске от root: git отказывается работать в чужом репозитории
# («dubious ownership»), проверка возвращала неудачей, и скрипт ПЕРЕУСТАНАВЛИВАЛ
# venv уже от root. То есть запуск от root не просто падал, а оставлял после
# себя venv, принадлежащий root, и следующий запуск от server его не мог
# починить.
#
# Поэтому носителем версии служит штамп INSTALL_PIN, а git - лишь
# дополнительная проверка, и она молча пропускается, если git не готов.
# И второе: если venv нет, а передан --systemd, скрипт требует сначала
# поставить его владельцем, а не ставит от root.
have_install() {
  [ -x "$PREFIX/venv/bin/python" ] || return 1
  [ -d "$PREFIX/src" ] || return 1
  [ -f "$PREFIX/INSTALL_PIN" ] || return 1
  [ "$(cat "$PREFIX/INSTALL_PIN")" = "$SEARXNG_PIN" ] || return 1
  head="$(git -C "$PREFIX/src" rev-parse HEAD 2>/dev/null || true)"
  if [ -n "$head" ] && [ "$head" != "$SEARXNG_PIN" ]; then
    say "внимание: $PREFIX/src на $head, а закреплён $SEARXNG_PIN - переустанавливаю"
    return 1
  fi
  return 0
}

if have_install; then
  say "уже установлен закреплённый коммит - установка не требуется"
else
  if [ "$(id -u)" = 0 ] && [ ! -x "$PREFIX/venv/bin/python" ]; then
    die "venv нет, а скрипт запущен от root.
      Ставить надо владельцем, иначе venv станет root-owned и следующий
      запуск от server его не починит. Сначала (от server):
        bash functions/search/install_searxng.sh
      Флаг --systemd можно передать отдельно, уже после установки."
  fi
  say "ставлю SearXNG $SEARXNG_PIN в $PREFIX"
  mkdir -p "$PREFIX"
  python3 -m venv "$PREFIX/venv" || die "не создался venv"

  # Исходники берутся точным коммитом. `clone --branch` не годится: тега у
  # этого проекта нет, а ветка master уезжает.
  if [ -d "$PREFIX/src/.git" ]; then
    git -C "$PREFIX/src" fetch --depth 1 origin "$SEARXNG_PIN"
    git -C "$PREFIX/src" checkout --force "$SEARXNG_PIN"
  else
    mkdir -p "$PREFIX/src"
    git -C "$PREFIX/src" init -q
    git -C "$PREFIX/src" remote add origin "$UPSTREAM" 2>/dev/null || true
    git -C "$PREFIX/src" fetch -q --depth 1 origin "$SEARXNG_PIN"
    git -C "$PREFIX/src" checkout -q "$SEARXNG_PIN"
  fi

  # Требования SearXNG уже закреплены точно (requirements.txt с пиннами), а
  # сам пакет ставится в редактируемом виде: webapp берёт статику из дерева
  # исходников, и обычная установка в site-packages её не даёт.
  #
  # --no-build-isolation обязателен: setup.py импортирует searx, то есть msgspec,
  # которого в изолированном окружении сборки нет. Ошибка выглядит невнятно
  # («Getting requirements to build editable: failed»), и на неё уже ушла
  # одна попытка.
  "$PREFIX/venv/bin/python" -m pip install --quiet --upgrade pip setuptools wheel
  "$PREFIX/venv/bin/python" -m pip install --quiet -r "$PREFIX/src/requirements.txt"
  "$PREFIX/venv/bin/python" -m pip install --quiet --no-build-isolation -e "$PREFIX/src"
  printf '%s\n' "$SEARXNG_PIN" > "$PREFIX/INSTALL_PIN"
  say "установлено: $("$PREFIX/venv/bin/python" -c 'import searx;print(searx.__file__)')"
fi

# ── лаунчер ───────────────────────────────────────────────────────────────

LAUNCHER="$PREFIX/searxng-run.sh"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
# Лаунчер SearXNG. Создаётся установщиком, в репозиторий не попадает.
set -euo pipefail
set -a; . "$SECRET_ENV"; set +a
export SEARXNG_SECRET
export SEARXNG_SETTINGS_PATH="$SETTINGS"
export SEARXNG_DISABLE_ETC_SETTINGS=1
cd "$PREFIX/src"
exec "$PREFIX/venv/bin/python" -m searx.webapp
EOF
chmod 755 "$LAUNCHER"

# ── systemd, по требованию ────────────────────────────────────────────────

if [ "$WITH_SYSTEMD" = 1 ]; then
  UNIT_SRC="$REPO/functions/search/searxng.service"
  [ -f "$UNIT_SRC" ] || die "нет $UNIT_SRC"
  [ "$(id -u)" = 0 ] || die "--systemd требует root: нужны /etc/systemd/system и systemctl enable"
  install -m 644 "$UNIT_SRC" /etc/systemd/system/searxng.service
  systemctl daemon-reload
  systemctl enable --now searxng.service
  say "юнит включён; состояние: $(systemctl is-active searxng.service)"
else
  say "юнит не трогал (передайте --systemd от root, чтобы он поднимался после перезагрузки)"
fi

say "готов запуск: $LAUNCHER"
