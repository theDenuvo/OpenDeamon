"""MoA $0 interlock: prove no PAID model is reachable through MoA.

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

Run: py A:/OpenDeamon/functions/quota-watch/test_moa_free_rail.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, r"C:\Users\cheli\AppData\Local\hermes\hermes-agent")

import yaml  # noqa: E402
from hermes_cli.moa_config import normalize_moa_config  # noqa: E402

CONFIG = Path(r"A:\OpenDeamon\hermes-home\config.yaml")


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


def check() -> list[str]:
    if not CONFIG.is_file():
        return [f"config not found: {CONFIG}"]
    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    fails: list[str] = []

    moa_raw = raw.get("moa")
    if not isinstance(moa_raw, dict):
        return ["config has no `moa:` block - Hermes would fall back to "
                "config_defaults, whose default preset is PAID "
                "(openai-codex/gpt-5.5, deepseek-v4-pro, claude-opus-4.8)"]

    cfg = normalize_moa_config(moa_raw)
    for role, provider, model in slots(cfg):
        fails += check_free(f"{role} (provider={provider!r})", model)

    # The interlock itself: the default preset must be disabled so /moa cannot
    # fan out (and cannot reach the reference models at all).
    preset = (cfg.get("presets") or {}).get("default") or {}
    if preset.get("enabled", True) is not False:
        fails.append(
            "moa.presets.default.enabled is not False - /moa would fan out to "
            "every reference model (~3 calls per turn). Set enabled: false; "
            "per agent/moa_loop.py a disabled preset runs the aggregator alone.")

    return fails


def upstream_default_status() -> tuple[bool, list[str]]:
    """Is Hermes' own default MoA preset still PAID?

    Not a pass/fail check - it is the premise behind keeping our block. If
    upstream ever ships a free default, the block could be dropped; until
    then, deleting ours would expose paid models. Returns (is_paid, models).
    """
    try:
        from hermes_cli.config_defaults import DEFAULT_CONFIG  # type: ignore
        presets = ((DEFAULT_CONFIG or {}).get("moa") or {}).get("presets") or {}
        default = presets.get("default") or {}
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"cannot read config_defaults ({type(exc).__name__}) - unable to "
            f"verify why the interlock exists") from None
    agg = (default.get("aggregator") or {}).get("model", "")
    refs = [str((s or {}).get("model", "")) for s in (default.get("reference_models") or [])]
    paid = [m for m in [agg, *refs] if m and not m.endswith(":free")]
    return bool(paid), paid


def main() -> int:
    fails = check()
    try:
        upstream_paid, upstream_models = upstream_default_status()
    except RuntimeError as exc:
        fails.append(str(exc))
        upstream_paid, upstream_models = False, []

    if fails:
        print("FAIL - a PAID model is reachable, or the interlock is off:")
        for f in fails:
            print("   -", f)
        return 1

    raw = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
    cfg = normalize_moa_config(raw.get("moa") or {})
    preset = (cfg.get("presets") or {}).get("default") or {}
    agg = preset.get("aggregator") or {}
    print("pass  no paid model reachable through MoA")
    print(f"      preset default enabled = {preset.get('enabled')}"
          "  -> /moa runs the aggregator alone (1 call, not 3)")
    print(f"      aggregator = {agg.get('provider','')}/{agg.get('model','')}")
    if upstream_paid:
        print(f"      upstream default preset is PAID ({', '.join(upstream_models)}),"
              " so keeping our block is required")
    else:
        print("      upstream default preset is now free - our block could be"
              " dropped if MoA is wanted enabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

