r"""MoA $0 interlock: prove no PAID model is reachable through MoA.

Why this exists
---------------
Two real holes were found on 2026-09-30, both by resolving the config the
way Hermes does and reading the result rather than trusting the YAML:

1. `_normalize_preset` builds the aggregator as
       _clean_slot(raw["aggregator"]) or deepcopy(DEFAULT_MOA_AGGREGATOR)
   A slot without `provider` cleans to None, so DEFAULT_MOA_AGGREGATOR wins
   - and that default is anthropic/claude-opus-4.8, a PAID model. The preset
   carried `aggregator.model` with no `provider`, so /moa was billing a paid
   aggregator on a project whose entire law is $0.
2. Deleting the `moa:` block does not disable MoA: config_defaults ships a
   default preset whose references are openai-codex/gpt-5.5 and
   openrouter/deepseek-v4-pro, aggregator claude-opus-4.8. Removing our block
   would have exposed exactly that.

This test fails loudly if either shape ever comes back, and if a user preset
ever names a model that is not `:free`.

ПЕРЕНОСИМОСТЬ. Набор проверял две вещи, и одна из них требовала чужой
машины: `normalize_moa_config` и `DEFAULT_CONFIG` живут в исходниках
установленного Hermes (`%LOCALAPPDATA%\hermes\hermes-agent`), а путь к ним
был вписан в `sys.path` константой с буквой диска. На любой машине без
Hermes набор падал на ИМПОРТЕ - до первой проверки.

Хуже: обе дыры выше проверяются по ФОРМЕ конфига, а не по коду Hermes.
Слот без `provider` и модель без `:free` видны в самом YAML. Поэтому сейчас:

  - пять групп, и три из них читают ТОЛЬКО репозиторий, поэтому выполняются
    на любой машине и ловят обе дыры;
  - две группы требуют исходников Hermes и объявляют SKIP с причиной, если
    их на этой машине нет.

Раньше здесь был один безусловный `import` и один безусловный путь к
`A:\OpenDeamon\hermes-home\config.yaml`. Оба заменены на вычисление от
`__file__` с переопределением через `$HERMES_CONFIG`.

Run: python functions/quota-watch/test_moa_free_rail.py
"""
from __future__ import annotations

import os
import sys
from collections import namedtuple
from pathlib import Path

import yaml  # noqa: E402

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent


def find_config() -> Path:
    """Конфиг, который проверяем.

    Порядок: явное переопределение, потом репозиторий, потом исходная
    Windows-развёртка. Последнее - для машины разработчика, где проект
    лежит на `A:`; на любой другой машине первых двух достаточно.
    """
    override = os.environ.get("HERMES_CONFIG")
    if override:
        return Path(override)
    inside = _ROOT / "hermes-home" / "config.yaml"
    if inside.is_file():
        return inside
    return Path(r"A:\OpenDeamon\hermes-home\config.yaml")


CONFIG = find_config()

# Третий исход: «проверка не выполнялась». Не путать с успехом.
Skip = namedtuple("Skip", "reason")

TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def hermes_module(name):
    """`hermes_cli.<name>` или None, если Hermes на этой машине не стоит.

    Путь к исходникам больше не вписан константой: сначала переменная
    окружения (её ставит bootstrap), потом типичные места установки.
    """
    candidates = []
    src = os.environ.get("HERMES_SRC")
    if src:
        candidates.append(src)
    candidates.append(os.path.expandvars(
        r"%LOCALAPPDATA%\hermes\hermes-agent"))
    for root in candidates:
        if root and os.path.isdir(root) and root not in sys.path:
            sys.path.insert(0, root)
    try:
        return __import__("hermes_cli.%s" % name, fromlist=["x"])
    except Exception:  # noqa: BLE001 - отсутствие Hermes это норма, не сбой
        return None


def raw_moa() -> dict:
    if not CONFIG.is_file():
        return {}
    return yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}


def slots(cfg: dict) -> list[tuple[str, str, str]]:
    """(role, provider, model) for every slot in every preset, plus the
    flattened view MoA actually dispatches on."""
    out: list[tuple[str, str, str]] = []
    for pname, preset in (cfg.get("presets") or {}).items():
        for i, s in enumerate(preset.get("reference_models") or []):
            out.append((f"{pname}.reference[{i}]", str(s.get("provider", "")),
                        str(s.get("model", ""))))
        agg = preset.get("aggregator") or {}
        out.append((f"{pname}.aggregator", str(agg.get("provider", "")),
                    str(agg.get("model", ""))))
    # MoA also exposes a flattened view derived from the default preset.
    if "reference_models" in cfg:
        for i, s in enumerate(cfg.get("reference_models") or []):
            out.append((f"flat.reference[{i}]", str(s.get("provider", "")),
                        str(s.get("model", ""))))
    return out


def check_free(label: str, model: str) -> list[str]:
    if not model:
        return [f"{label}: model is empty (a slot with no provider/model cleans "
                f"to None and falls back to the PAID default)"]
    if not model.endswith(":free"):
        return [f"{label}: PAID model reachable: {model}"]
    return []


# --- группы --------------------------------------------------------------

@test
def test_config_is_reachable():
    if not CONFIG.is_file():
        return ["config not found: %s (looked from %s; set $HERMES_CONFIG to "
                "point at it)" % (CONFIG, _ROOT)]
    return []


@test
def test_the_moa_block_is_present():
    """Дыра №2: удаление блока `moa:` НЕ выключает MoA - оно возвращает
    платный пресет из `config_defaults`."""
    moa = raw_moa().get("moa")
    if not isinstance(moa, dict):
        return ["config has no `moa:` block - Hermes would fall back to "
                "config_defaults, whose default preset is PAID "
                "(openai-codex/gpt-5.5, deepseek-v4-pro, claude-opus-4.8)"]
    return []


@test
def test_no_paid_slot_in_our_own_block():
    """Дыра №1 и закон $0: ни один слот нашего блока не должен называть
    платную модель. Читается из репозитория, поэтому работает везде."""
    moa = raw_moa().get("moa")
    if not isinstance(moa, dict):
        return []                      # предыдущая группа это поймает
    fails = []
    for role, provider, model in slots(moa):
        fails += check_free(f"{role} (provider={provider!r})", model)
    return fails


@test
def test_every_slot_names_its_provider():
    """Дыра №1 - самая дорогая, потому что она молчаливая.

    `_normalize_preset` берёт `_clean_slot(...) or DEFAULT_MOA_AGGREGATOR`.
    Слот без `provider` чистится в None, и выигрывает DEFAULT - а это
    anthropic/claude-opus-4.8, ПЛАТНЫЙ. YAML при этом выглядит совершенно
    невинно, поэтому отсутствие `provider` проверяется явно и отдельно от
    значения модели.
    """
    moa = raw_moa().get("moa")
    if not isinstance(moa, dict):
        return []
    fails = []
    for role, provider, model in slots(moa):
        if not provider:
            fails.append("%s has no `provider` - the slot cleans to None and "
                         "MoA falls back to the PAID default aggregator "
                         "(model=%r)" % (role, model))
    return fails


@test
def test_the_preset_is_disabled_so_moa_cannot_fan_out():
    """`enabled: false` - это то, что реально выключает разветвление:
    по agent/moa_loop.py отключённый пресет идёт напрямую в агрегатор,
    то есть 1 вызов вместо 3."""
    moa = raw_moa().get("moa")
    if not isinstance(moa, dict):
        return []
    preset = (moa.get("presets") or {}).get("default") or {}
    if not preset:
        return ["moa.presets.default is missing - /moa would use the "
                "upstream PAID default preset"]
    if preset.get("enabled", True) is not False:
        return ["moa.presets.default.enabled is not False - /moa would fan "
                "out to every reference model (~3 calls per turn). Set "
                "enabled: false; per agent/moa_loop.py a disabled preset runs "
                "the aggregator alone."]
    return []


@test
def test_normalized_config_still_resolves_to_free_slots():
    """Та же проверка, но на РЕЗОЛЬВНУТОМ конфиге - тем самым кодом,
    которым пользуется Hermes. Требует установленного Hermes; без него
    проверка не выполняется и говорит об этом, а не рапортует успех."""
    moa_mod = hermes_module("moa_config")
    if moa_mod is None:
        return Skip("hermes_cli.moa_config is not installed on this host, so "
                    "the resolved config cannot be checked here")
    moa = raw_moa().get("moa")
    if not isinstance(moa, dict):
        return []                      # предыдущая группа это поймает
    cfg = moa_mod.normalize_moa_config(moa)
    fails = []
    for role, provider, model in slots(cfg):
        fails += check_free(f"resolved {role} (provider={provider!r})", model)
    preset = (cfg.get("presets") or {}).get("default") or {}
    if preset.get("enabled", True) is not False:
        fails.append("after normalization the default preset is not disabled")
    agg = preset.get("aggregator") or {}
    print("      aggregator = %s/%s" % (agg.get("provider", ""),
                                        agg.get("model", "")))
    return fails


@test
def test_upstream_default_preset_is_still_paid():
    """Это не проверка паса/фейла, а ПОСЫЛКА, на которой держится наш блок.

    Если бы upstream когда-нибудь отдал бесплатный дефолт, блок можно было
    бы удалить. Пока нет - удаление открыло бы платные модели. Требует
    исходников Hermes (`config_defaults`), поэтому без них это SKIP, а не
    «проверено, что upstream платный».
    """
    mod = hermes_module("config_defaults")
    if mod is None:
        return Skip("hermes_cli.config_defaults is not installed on this "
                    "host; whether upstream's default is still PAID cannot be "
                    "read here")
    default = (((getattr(mod, "DEFAULT_CONFIG", None) or {}).get("moa")
                or {}).get("presets") or {}).get("default") or {}
    agg = (default.get("aggregator") or {}).get("model", "")
    refs = [str((s or {}).get("model", ""))
            for s in (default.get("reference_models") or [])]
    paid = [m for m in [agg, *refs] if m and not m.endswith(":free")]
    if paid:
        print("      upstream default preset is PAID (%s), so keeping our "
              "block is required" % ", ".join(paid))
        return []
    return ["upstream's default MoA preset now looks free (%r); our `moa:` "
            "block may be dead weight - re-check before removing it" % (paid,)]


def main() -> int:
    failed = skipped_n = 0
    for label, fn in TESTS:
        try:
            outcome = fn()
        except Exception as exc:  # noqa: BLE001
            outcome = ["%s: %s" % (type(exc).__name__, exc)]
        if isinstance(outcome, Skip):
            skipped_n += 1
            print("SKIP  %s" % label)
            print("        - %s" % outcome.reason)
            continue
        fails = list(outcome or [])
        if fails:
            failed += 1
            print("FAIL  %s" % label)
            for line in fails:
                print("        - %s" % line)
        else:
            print("pass  %s" % label)
    print()
    ran = len(TESTS) - skipped_n
    print("groups: %d total, %d passed, %d failed, %d skipped"
          % (len(TESTS), ran - failed, failed, skipped_n))
    if failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())