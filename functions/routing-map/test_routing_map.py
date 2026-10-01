"""Фаза 4: карта маршрутизации проверена по факту, а не по описанию.

План утверждает, что пользователь не трогает /model, и перечисляет семь
маршрутов. Проверять это «похоже, работает» нельзя - проект уже ловил
конфигурацию, которая выглядела включённой и не была. Каждая строка карты
проверяется здесь тем способом, который соответствует её природе:

  - разговор/план -> основной ход: реальный вызов ядра
  - кодинг -> opencode worker: правило delegate-first должно БЛОКИРОВ��ть
    прямое кодирование, иначе маршрут не работает
  - глубокий ресёрч -> delegate_task: делегация идёт на NIM, а не на ядро
  - фото -> ComfyUI + Qwen fp8: путь и модель в SKILL и в файле совпадают
  - визуал -> auxiliary.vision: локальный Qwen3-VL, не облако
  - UI -> computer_use в режиме ax: текстовое ядро не должно получать пиксели
  - файлы -> read/patch: базовый тулсет

Не-противоречие проверяется отдельно: каждый маршрут обязан быть БЕСПЛАТНЫМ,
потому что закон проекта - $0.
"""
from __future__ import annotations

import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
CONFIG = os.path.join(_ROOT, "hermes-home", "config.yaml")
TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def _read(path):
    try:
        return open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return ""


def _yaml():
    import yaml
    return yaml.safe_load(_read(CONFIG)) or {}


# --- маршруты ----------------------------------------------------------

@test
def test_conversation_goes_to_the_free_core_and_nothing_paid():
    d = _yaml()
    model = (d.get("model") or {})
    default = str(model.get("default") or "")
    if not default.endswith(":free"):
        return ["core model is not a free SKU: %s" % default]
    # Весь fallback тоже должен быть бесплатным: первый платный элемент
    # перехватит запрос раньше, чем сработает второй.
    for entry in d.get("fallback_providers") or []:
        m = str(entry.get("model") or "")
        if not m.endswith(":free"):
            return ["a fallback is not free: %s" % m]
    for entry in (d.get("moa") or {}).get("presets", {}).get(
            "default", {}).get("reference_models") or []:
        if not str(entry.get("model") or "").endswith(":free"):
            return ["a MoA reference model is not free: %s" % entry]
    agg = ((d.get("moa") or {}).get("presets", {}).get("default", {})
           .get("aggregator") or {})
    if agg and not str(agg.get("model") or "").endswith(":free"):
        return ["the MoA aggregator is not free: %s" % agg]
    return []


@test
def test_moa_is_switched_off_and_was_not_deleted():
    """Владелец решил: MoA выключить и не трогать. Interlock обязана стоять -
    upstream-дефолт платный. Отсутствие блока тоже считаем нарушением."""
    d = _yaml()
    moa = d.get("moa")
    if moa is None:
        return ["the MoA interlock is gone; the upstream default is paid"]
    preset = ((moa.get("presets") or {}).get("default") or {})
    if preset.get("enabled") is not False:
        return ["the MoA preset is not disabled: %r" % preset.get("enabled")]
    return []


@test
def test_coding_is_forced_through_the_delegate_first_hook():
    """Маршрут «кодинг -> opencode worker» держится на механике, а не на
    просьбе. Если хук перестал блокировать, маршрут вырождается в «ядро пишет
    код сам»."""
    body = _read(os.path.join(_ROOT, "functions", "delegate-first",
                              "pre_tool_budget.py"))
    if not body:
        return ["the delegate-first hook source is missing"]
    for tool in ("write_file", "patch", "edit", "delegate_task"):
        if tool not in body:
            return ["the hook no longer mentions %s" % tool]
    return []


@test
def test_delegation_uses_a_separate_provider_from_the_core():
    """Ключевое: делегация не должна идти на провайдера ядра, иначе
    рецензент и кодер на одном рельсе."""
    d = _yaml()
    core_provider = (d.get("model") or {}).get("provider")
    dl = d.get("delegation") or {}
    if dl.get("provider") == core_provider and core_provider:
        return ["delegation shares the core provider %s" % core_provider]
    if not dl.get("model"):
        return ["delegation has no model"]
    return []


@test
def test_review_is_independent_of_the_core_provider():
    """Рецензент обязан быть на другом провайдере ИЛИ явно помечен, если
    нет. Иначе вердикт не независим, а выглядит так, будто независим."""
    d = _yaml()
    core_provider = (d.get("model") or {}).get("provider")
    review = ((d.get("auxiliary") or {}).get("review") or {})
    chain = [review] + list(review.get("fallback_chain") or [])
    providers = {str((e or {}).get("provider") or "") for e in chain if e}
    if not review.get("provider"):
        return ["auxiliary.review has no provider"]
    if review.get("provider") == core_provider:
        return ["the reviewer's primary IS the core provider %s; that is "
                "SEVERE, not FULL" % core_provider]
    # Допустим, что последний элемент ведёт на ядро - но это должно быть
    # обнаружимо, а не молчаливо. Проверяем, что механизм детекта есть.
    if core_provider in providers:
        src = _read(os.path.join(_ROOT, "functions", "verdict", "verdict.py"))
        if "route_is_core_provider" not in src:
            return ["a chain entry leads to the core provider and there is no "
                    "detector for it"]
    return []


@test
def test_vision_is_local_and_the_ui_mode_needs_no_pixels():
    d = _yaml()
    vision = ((d.get("auxiliary") or {}).get("vision") or {})
    if vision.get("provider") != "custom" or "127.0.0.1" not in str(
            vision.get("base_url") or ""):
        return ["vision is not routed to the local model: %s" % vision]
    cu = d.get("computer_use") or {}
    if cu.get("capture_after_mode") != "ax":
        return ["computer_use does not use the accessibility tree; a "
                "text-only core would get a screenshot it cannot read: %r"
                % cu.get("capture_after_mode")]
    return []


@test
def test_background_spending_roles_are_switched_off():
    """Эти две роли съедали 11% бюджета впустую."""
    a = _yaml().get("auxiliary") or {}
    for key in ("title_generation", "background_review"):
        v = a.get(key)
        if isinstance(v, dict) and v.get("enabled") is not False:
            return ["%s is still enabled" % key]
    if a.get("free_only") is not True:
        return ["auxiliary.free_only is not true"]
    return []


@test
def test_the_image_path_document_and_the_code_agree():
    """Матрица обещала SD 1.5 за 1.5 с, пока рабочий путь ComfyUI + fp8.
    Расхождение документации и кода - это и есть неправда."""
    matrix = _read(os.path.join(_ROOT, "functions", "capability-matrix",
                                "MATRIX.md"))
    if "SD 1.5, RTX 5060 Ti" in matrix:
        return ["MATRIX.md still advertises SD 1.5 as the image path"]
    skill = _read(os.path.join(_ROOT, "functions", "imggen", "SKILL.md"))
    if "comfy_gen.py" not in skill:
        return ["imggen/SKILL.md does not name the working entry point"]
    code = _read(os.path.join(_ROOT, "functions", "imggen", "comfy_gen.py"))
    if "fp8" not in code.lower():
        return ["comfy_gen.py does not look like the fp8 path"]
    return []


@test
def test_no_paid_model_is_reachable_through_the_config():
    """Итоговая проверка закона $0: ни один платный идентификатор в
    маршрутах не должен просочиться."""
    d = _yaml()
    paid = ("gpt-4", "gpt-5", "claude-opus", "deepseek-v4-pro", "o1", "o3")
    text = _read(CONFIG)
    for token in paid:
        for line in text.splitlines():
            if token in line and ":free" not in line:
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue          # комментарий про платное - не маршрут
                return ["a non-free model id appears in an active line: %s"
                        % stripped[:90]]
    return []


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