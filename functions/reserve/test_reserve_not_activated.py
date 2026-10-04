"""Фаза 9: аварийный резерв документирован и НЕ активирован.

TODO Фаза 9 просит описать платный GPT как аварийный аэродром и при этом
не включать его. Половина этой просьбы невыполнима без проверки: «не
включён» — это не обещание в документе, а отсутствие маршрута в конфиге.
Поэтому набор делает ровно две вещи.

1. **Ни одного платного маршрута.** Ядро, `fallback_providers`, каждая
   цепочка `fallback_chain` и пресет MoA остаются на `:free` или локальном
   провайдере, `bootstrap.ps1` не грузит платный ключ, и в конфиге нет
   инлайнового `api_key` и внешнего `api.openai.com`.

2. **Процедура существует.** Шесть пунктов (а)–(е) и явная строка «не
   активировано» лежат в `DEPLOYMENT_REPORT.md` §26-bis, и на них есть
   ссылка из `ARCHITECTURE.md`. Иначе через месяц «резерв» останется в
   голове автора, и первое же настоящее отключение `:free` закончится
   импровизацией при полной остановке ядра.

Проверка намеренно читает ТОЛЬКО репозиторий: ни сети, ни ключей, ни
дисков. Её можно гонять на любой машине — портативность здесь не
вежливость, а условие, что набор вообще запускается на CI.

Запуск: `python functions/reserve/test_reserve_not_activated.py`
"""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
CONFIG = os.path.join(_ROOT, "hermes-home", "config.yaml")
BOOTSTRAP = os.path.join(_ROOT, "bootstrap.ps1")
REPORT = os.path.join(_ROOT, "DEPLOYMENT_REPORT.md")
ARCH = os.path.join(_ROOT, "ARCHITECTURE.md")
RESERVE_SECTION = "26-bis"

# Модели, у которых нет бесплатного рельса. Список шире, чем у
# routing-map, потому что здесь важен сам факт: любая платная строка в
# активной (не комментарий) строке конфига означает, что резерв поднят.
#
# Обратите внимание, чего в списке НЕТ: `openai/gpt-oss-20b` и
# `openai/gpt-oss-120b`. Это не платный OpenAI. Это open-weight модели,
# которые NIM отдаёт по `NVIDIA_API_KEY`, а Groq — по `GROQ_API_KEY`, обе
# навсегда бесплатны (SCHEME §26, комментарий в config.yaml). Префикс
# `openai/` здесь — только имя OpenAI-совместимого пространства имён.
# Проверка, которая «просто ищет gpt-», покраснела бы на действующем и
# правильном конфиге — то есть отучилась бы проверять.
PAID_TOKENS = ("gpt-4", "gpt-5", "claude-", "claude-opus", "opus-4",
               "sonnet", "deepseek-v4-pro", "o1-", "o3-", "o4-")

# Не-`:free` рельсы, которые всё равно бесплатны, потому что ключ бесплатный.
FREE_PROVIDERS = ("nvidia", "pollinations", "ollama", "llamacpp")
FREE_CUSTOM_HOSTS = ("api.groq.com", "127.0.0.1", "localhost")

# Куда физически нельзя смотреть: платный резерв не должен осесть ни в
# одном из этих мест.
FORBIDDEN_IN_CONFIG = ("api.openai.com", "OPENAI_API_KEY")

# `key_env` — разрешённый способ сослаться на секрет (Groq-рельс так и
# сделан). `api_key:` инлайном — нет: значение попадёт в git-трекаемый
# файл. Локальный `api_key: "no-key"` у vision — не секрет, это протокол
# Ollama, поэтому исключение точное и узкое.
ALLOWED_KEY_ENV = ("GROQ_API_KEY", "NVIDIA_API_KEY", "OPENROUTER_API_KEY")

TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def active_lines(text):
    """Строки, которые реально читает Hermes.

    Комментарий про платное — не маршрут, и наоборот: именно поэтому
    moa-блок может объяснять, от чего он спасает. Всё остальное —
    активная конфигурация.
    """
    return [(i, l) for i, l in enumerate(text.splitlines(), 1)
            if l.strip() and not l.strip().startswith("#")]


# --- (1) резерв не активирован -------------------------------------------

@test
def test_no_paid_model_id_on_any_active_config_line():
    fails = []
    for num, line in active_lines(read(CONFIG)):
        if ":free" in line:
            continue
        for token in PAID_TOKENS:
            if token in line:
                fails.append("line %d exposes a paid id (%s): %s"
                             % (num, token, line.strip()[:80]))
    return fails


@test
def test_core_and_every_fallback_stay_free():
    """Точечная проверка дороже grep: она читает структуру, а не текст.

    Ключ `fallback_chain` живёт на восьми уровнях конфига, и резерв,
    добавленный во внутренний список, не был бы виден ни одной
    построчной проверке.
    """
    import yaml
    data = yaml.safe_load(read(CONFIG)) or {}
    fails = []

    core = (data.get("model") or {})
    default = str(core.get("default") or "")
    if not default.endswith(":free"):
        fails.append("core model is not a free SKU: %r" % default)

    for i, entry in enumerate(data.get("fallback_providers") or []):
        model = str((entry or {}).get("model") or "")
        if not model.endswith(":free"):
            fails.append("fallback_providers[%d] is not free: %r" % (i, model))

    aux = data.get("auxiliary") or {}
    for lane in ("review", "vision"):
        for i, entry in enumerate(((aux.get(lane) or {})
                                   .get("fallback_chain") or [])):
            entry = entry or {}
            model = str(entry.get("model") or "")
            if model.endswith(":free"):
                continue
            if str(entry.get("provider") or "") in FREE_PROVIDERS:
                continue
            base_url = str(entry.get("base_url") or "")
            if (entry.get("provider") == "custom"
                    and any(h in base_url for h in FREE_CUSTOM_HOSTS)):
                continue
            fails.append("auxiliary.%s.fallback_chain[%d] is neither :free nor "
                         "a free-key rail: %r (provider=%r, base_url=%r)"
                         % (lane, i, model, entry.get("provider"), base_url))

    for name, slot in ((data.get("moa") or {}).get("presets") or {}).items():
        preset = slot or {}
        for ref in preset.get("reference_models") or []:
            model = str((ref or {}).get("model") or "")
            if not model.endswith(":free"):
                fails.append("moa preset %s reference is not free: %r"
                             % (name, model))
        agg = (preset.get("aggregator") or {}).get("model")
        if agg and not str(agg).endswith(":free"):
            fails.append("moa preset %s aggregator is not free: %r"
                         % (name, agg))
    return fails


@test
def test_no_paid_endpoint_or_key_in_the_config():
    """`OPENAI_API_KEYS` мёртв (401), а новый платный ключ обязан прийти
    через `key_env`. Инлайновый `api_key` — это секрет в git."""
    fails = []
    for num, line in active_lines(read(CONFIG)):
        for token in FORBIDDEN_IN_CONFIG:
            if token in line:
                fails.append("line %d mentions %s: %s"
                             % (num, token, line.strip()[:80]))
        if "api_key:" in line:
            value = line.split("api_key:", 1)[1].strip().strip('"\'')
            if value and value != "no-key":
                fails.append("line %d carries an inline api_key: %s"
                             % (num, line.strip()[:80]))
        if "key_env:" in line:
            name = line.split("key_env:", 1)[1].strip()
            if name not in ALLOWED_KEY_ENV:
                fails.append("line %d wires a new key_env: %s" % (num, name))
    return fails


@test
def test_bootstrap_still_loads_only_the_three_free_keys():
    """`bootstrap.ps1:80` — список, через который секрет попадает в
    процесс. Пока он там ровно из двух имён, платный ключ физически не
    может стать маршрутом."""
    text = read(BOOTSTRAP)
    fails = []
    allow = [l for l in text.splitlines() if "API_KEY" in l
             and "-in" in l and "SetEnvironmentVariable" not in l]
    if not allow:
        fails.append("the key allow-list in bootstrap.ps1 is gone")
        return fails
    for line in allow:
        for name in ("OPENAI_API_KEY", "OPENAI_API_KEYS", "ANTHROPIC_API_KEY"):
            if name in line:
                fails.append("bootstrap.ps1 would load a paid key: %s"
                             % line.strip()[:80])
    loaded = [l for l in text.splitlines() if l.strip().startswith("$env:")]
    for line in loaded:
        if "OPENAI" in line:
            fails.append("bootstrap.ps1 exports a paid key: %s"
                         % line.strip()[:80])
    return fails


# --- (2) процедура существует --------------------------------------------

@test
def test_the_runbook_covers_all_six_points():
    fails = []
    text = read(REPORT)
    if RESERVE_SECTION not in text:
        fails.append("DEPLOYMENT_REPORT.md has no §%s" % RESERVE_SECTION)
        return fails
    section = text.split(RESERVE_SECTION, 1)[1]
    section = section.split("\n## ", 1)[0]
    for label in ("(а)", "(б)", "(в)", "(г)", "(д)", "(е)"):
        if label not in section:
            fails.append("§%s does not cover point %s" % (RESERVE_SECTION, label))
    if "не активировано" not in section:
        fails.append("§%s does not say 'не активировано'" % RESERVE_SECTION)
    # Шестой пункт — про откат, и он обязан быть не только «как вернуть»,
    # но и «чем доказать, что вернулось».
    for needle in ("hermes doctor", "verdict.py routes", "$0.00"):
        if needle not in section:
            fails.append("§%s cannot prove the rollback: %r missing"
                         % (RESERVE_SECTION, needle))
    return fails


@test
def test_the_runbook_says_why_the_trigger_is_not_a_429():
    """Самая частая подмена: списали суточную квоту и решили, что каталог
    умер. Диагноз обязан различать их прямо в тексте."""
    fails = []
    section = read(REPORT).split(RESERVE_SECTION, 1)[-1].split("\n## ", 1)[0]
    for needle in ("VPN", "401", "403"):
        if needle not in section:
            fails.append("the trigger table does not mention %s" % needle)
    if "bootstrap.ps1" not in section:
        fails.append("the procedure does not say where the key is loaded from")
    return fails


@test
def test_architecture_points_at_the_runbook():
    text = read(ARCH)
    fails = []
    if RESERVE_SECTION not in text:
        fails.append("ARCHITECTURE.md has no pointer to §%s" % RESERVE_SECTION)
    if "не активировано" not in text:
        fails.append("ARCHITECTURE.md does not carry the 'не активировано' "
                     "marker; the pointer must not read as an active route")
    return fails


def main() -> int:
    failed = 0
    for label, fn in TESTS:
        try:
            fails = list(fn() or [])
        except Exception as exc:  # noqa: BLE001
            fails = ["%s: %s" % (type(exc).__name__, exc)]
        if fails:
            failed += 1
            print("FAIL  %s" % label)
            for line in fails:
                print("        - %s" % line)
        else:
            print("pass  %s" % label)
    print()
    if failed:
        print("%d/%d groups failed" % (failed, len(TESTS)))
        return 1
    print("all pass (%d groups)" % len(TESTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())