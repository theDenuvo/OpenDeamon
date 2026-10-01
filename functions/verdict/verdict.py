"""Слой 6 - независимость рецензента и итоговый вердикт.

Слой 4 доказал, что критерии исполнены. Слой 5 получил мнение второй модели.
Слой 6 отвечает на вопрос, которое оба предыдущих не могут: а кому мы вообще
доверяем, и что из этого следует для статуса задачи.

Правила, которые здесь нельзя ослабить:

1. FULL = другой провайдер И другая модель.
   PARTIAL = другая модель, тот же провайдер.
   SEVERE = тот же провайдер И та же модель.

   Считать по ФАКТИЧЕСКИМ провайдерам, а не по заявленным. Рецензент,
   ушедший в фолбэк, меняет провайдера молча: сейчас в конфиге последний
   элемент review.fallback_chain - это openrouter, то есть провайдер ЯДРА.
   Такая подстановка даёт PARTIAL вместо FULL, и без флага это выглядело бы
   как полная независимость.

2. SEVERE => task_status != verified, но РЕЗУЛЬТАТ ВЫДАЁТСЯ с пометкой в
   первой строке вердикта. Блокировка превращала «проверенный результат +
   то же мнение» в «ничего» ровно тогда, когда провайдеры лежат. Отнимать
   результат у пользователя из-за недоступности рельса - это наказание за
   чужую поломку.

3. Комбинацию система обязана обнаружить сама: OpenRouter упал (ядро ушло на
   NIM) и Groq упал (ревьюер ушел на NIM) => Core = NIM и Reviewer = NIM =>
   SEVERE. Объявлять независимость в этой ситуации - ложь.

4. Каждая новая мутация проходит механику заново. После rework обязателен
   полный цикл: worker -> mechanical -> reviewer. Иначе получается
   worker -> verify -> reviewer -> FAIL -> worker -> reviewer, где вторая
   мутация не встретила барьера.

5. Типология фейлов и НАЗНАЧЕННОЕ действие. TEST_TAMPERING и WORKER_CRASH
   никогда не дают воркеру ещё заход: повтор воспроизводит то же самое.
   INFRA_FAILURE сменяет маршрут, а не код, иначе сетевая помеха съедает цикл
   rework.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.dirname(_HERE)
for _d in ("", "mechanical", "reviewer"):
    _p = os.path.join(_FUNCTIONS, _d) if _d else _HERE
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

FULL = "FULL"
PARTIAL = "PARTIAL"
SEVERE = "SEVERE"
UNKNOWN = "UNKNOWN"

SEVERITY_ORDER = ("blocker", "major", "minor")


# --- независимость -----------------------------------------------------

def _norm(value: str) -> str:
    """Нормализовать провайдера/модель.

    Не-строка приводится к ПУСТОМУ значению, а не к str(value): значение 5 -
    это мусор на входе, и `str(5)` сравнилось бы с реальным именем как
    осмысленное. Пустая строка даёт честный UNKNOWN вместо исключения или
    выдуманного совпадения."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.strip().lower().split())


def independence(core: dict, reviewer: dict) -> dict:
    """Классифицировать независимость по ФАКТИЧЕСКИМ провайдерам и моделям.

    `core` и `reviewer` - словари с ключами `provider`, `model` и, по желанию,
    `route` (каким именно элементом цепочки отвечен рецензент). Ничего не
    предполагается: если провайдер неизвестен, честный ответ UNKNOWN, а не
    FULL - оборотная сторона «удобного» допущения."""
    cp, cm = _norm(core.get("provider")), _norm(core.get("model"))
    rp, rm = _norm(reviewer.get("provider")), _norm(reviewer.get("model"))

    if not cp or not rp:
        return {"level": UNKNOWN, "core_provider": core.get("provider", ""),
                "reviewer_provider": reviewer.get("provider", ""),
                "reason": "provider of one side is unknown; assuming "
                          "independence would be a guess",
                "core_model": core.get("model", ""),
                "reviewer_model": reviewer.get("model", "")}
    if not cm or not rm:
        return {"level": UNKNOWN, "core_provider": cp,
                "reviewer_provider": rp, "core_model": cm, "reviewer_model": rm,
                "reason": "model of one side is unknown"}

    same_provider = cp == rp
    same_model = cm == rm

    if same_provider and same_model:
        level = SEVERE
        reason = ("same provider AND same model: the reviewer can only repeat "
                  "the core, it cannot disagree")
    elif same_provider:
        level = PARTIAL
        reason = ("same provider %s, different model: a shared provider means "
                  "a shared outage and correlated blind spots" % cp)
    else:
        level = FULL
        reason = ("different provider (%s vs %s) AND different model: "
                  "independent" % (cp, rp))
    return {"level": level, "core_provider": cp, "reviewer_provider": rp,
            "core_model": cm, "reviewer_model": rm, "reason": reason,
            "same_provider": same_provider, "same_model": same_model}


def route_is_core_provider(reviewer_cfg: dict, core_provider: str) -> list[str]:
    """Элементы review.fallback_chain, которые ведут на провайдера ядра.

    Именно они превращают независимость в PARTIAL, причём молча: подстановка
    происходит в рантайме, и без явной проверки это читается как FULL."""
    bad = []
    cp = _norm(core_provider)
    if not cp:
        return bad
    for i, entry in enumerate(reviewer_cfg.get("fallback_chain") or []):
        if not isinstance(entry, dict):
            continue
        if _norm(entry.get("provider")) == cp:
            bad.append("fallback_chain[%d](%s) is the CORE provider %r"
                       % (i, entry.get("provider"), core_provider))
    return bad


def collapse_scenario(core_fallback_to: str | None,
                      reviewer_fallback_to: str | None) -> dict:
    """Обнаружить SEVERE, возникший из-за падения провайдеров.

    Провайдеры лежат одновременно - и ядро, и рецензент уходят на один и тот
    же третий рельс. Тогда Core и Reviewer совпадают, и система обязана это
    сказать, а не заявить независимость."""
    if not core_fallback_to or not reviewer_fallback_to:
        return {"collapsed": False, "reason": "both sides stayed on their "
                                              "configured rails"}
    if _norm(core_fallback_to) == _norm(reviewer_fallback_to):
        return {"collapsed": True, "shared_provider": core_fallback_to,
                "level": SEVERE,
                "reason": "core and reviewer both fell back to %s; the review "
                          "is no longer independent" % core_fallback_to}
    return {"collapsed": False, "core_on": core_fallback_to,
            "reviewer_on": reviewer_fallback_to}


# --- вердикт -----------------------------------------------------------

def blocking_findings(findings) -> list[dict]:
    out = [f for f in (findings or [])
           if isinstance(f, dict)
           and str(f.get("severity", "")).strip().lower() in ("blocker", "major")]
    return out


def verdict_of(review: dict) -> str:
    v = (review.get("verdict") or {})
    verdict = str(v.get("verdict") or "").strip().upper()
    if verdict not in ("PASS", "REVISE", "REJECT"):
        return "REVISE"
    if verdict == "PASS" and blocking_findings(v.get("findings")):
        # PASS с blocker-ами - это противоречие, а не успех.
        return "REVISE"
    return verdict


def contract_tests_missing(review: dict, criteria: str | None) -> list[dict]:
    """Новые тесты, которые рецензент назвал contract, но которых нет в
    критериях.

    Только contract-тест обязан быть в критериях. Механика отличить не может -
    это вопрос намерения, а не структуры, - поэтому проверка здесь по тому, что
    сам рецензент классифицировал."""
    if not criteria:
        return []
    text = _norm(criteria)
    missing = []
    for t in (review.get("verdict") or {}).get("new_tests") or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("class", "")).strip().lower() != "contract":
            continue
        name = os.path.basename(str(t.get("file") or ""))
        stem = name[:-3] if name.endswith(".py") else name
        if stem and stem not in text:
            missing.append(t)
    return missing


def decide(mechanical: dict, review: dict, independence_info: dict,
           core_provider: str = "", reviewer_cfg: dict | None = None,
           collapse: dict | None = None, criteria: str | None = None) -> dict:
    """Итоговый вердикт задачи.

    `mechanical` - результат Слоя 4, `review` - результат Слоя 5. Ни один из
    них не может выставить task_status в verified: механика не судит о
    качестве, рецензент - о достаточности доказательств."""
    mech_ok = bool((mechanical or {}).get("passed"))
    mech_status = (mechanical or {}).get("status_after", "judged")
    review_ok = bool((review or {}).get("ok"))
    verdict = verdict_of(review)
    level = (independence_info or {}).get("level", UNKNOWN)
    reasons = list((mechanical or {}).get("reasons") or [])
    reasons += list((review or {}).get("reasons") or [])
    if (review or {}).get("error"):
        reasons.append(str(review["error"])[:200])

    core_on = ""
    core_model = ""
    if mechanical:
        core_on = core_provider or _norm(core_provider)

    if not mech_ok:
        kind = (mechanical or {}).get("kind") or "MECHANICAL_FAILURE"
        action = (mechanical or {}).get("action") or "REWORK_CODE"
        status = "judged"
        headline = "%s: %s" % (kind, action)
    elif not review_ok:
        # Рецензент не ответил. Это НЕ «всё хорошо» и не повод отдать
        # результат как проверенный.
        kind = "REVIEW_UNAVAILABLE"
        action = "CHANGE_ROUTE"
        status = "judged"
        headline = "%s: %s" % (kind, action)
        reasons.append("the reviewer produced no usable verdict")
    elif verdict in ("REVISE", "REJECT"):
        kind = "REVIEW_FAILURE"
        action = "REWORK_CODE"
        status = "judged"
        headline = "%s: %s" % (kind, action)
    else:
        kind = ""
        action = ""
        headline = "PASS"
        status = "judged" if mech_status != "verified" else "verified"

    # Независимость влияет на статус, но НЕ отменяет результат.
    notes = []
    if level == SEVERE:
        status = "judged"
        notes.append("SEVERE: reviewer shares provider and model with the "
                     "core; the task cannot be 'verified'")
    elif level == PARTIAL:
        notes.append("PARTIAL: reviewer shares the core's provider; the "
                     "verdict is weaker than independent")
    elif level == UNKNOWN:
        status = "judged"
        notes.append("UNKNOWN independence: a provider is unknown, and "
                     "assuming independence would be a guess")

    silent = route_is_core_provider(reviewer_cfg or {}, core_provider)
    for item in silent:
        notes.append("SILENT PARTIAL RISK: %s" % item)

    missing = contract_tests_missing(review, criteria)
    if missing:
        status = "judged"
        notes.append("%d contract test(s) classified by the reviewer are not "
                     "named in the acceptance criteria: %s"
                     % (len(missing),
                        ", ".join(os.path.basename(str(m.get("file")))
                                  for m in missing)))

    if collapse and collapse.get("collapsed"):
        status = "judged"
        notes.append("COLLAPSE: %s" % collapse.get("reason"))

    return {
        "task_status": status,
        "independence": level,
        "review_verdict": verdict,
        "kind": kind,
        "action": action,
        "headline": headline,
        "notes": notes,
        "reasons": reasons,
        "contract_tests_missing": missing,
        "collapsible": bool(collapse and collapse.get("collapsed")),
    }


def first_line(result: dict) -> str:
    """Первая строка вердикта - то, что человек видит первым.

    Пометка о независимости обязана быть ЗДЕСЬ, а не в хвосте лога: при SEVERE
    результат всё равно выдаётся, но читатель обязан увидеть ограничение
    сразу."""
    tag = result.get("independence", UNKNOWN)
    line = "[%s] %s | status=%s" % (tag, result.get("headline", ""),
                                    result.get("task_status", ""))
    for n in result.get("notes") or []:
        line += " | " + n.split(":")[0]
    return line


def render(result: dict) -> str:
    lines = [first_line(result)]
    lines.append("review: %s   action: %s"
                 % (result.get("review_verdict", "-"), result.get("action") or "-"))
    for n in result.get("notes") or []:
        lines.append("  ! %s" % n)
    for r in result.get("reasons") or []:
        lines.append("  - %s" % r)
    for m in result.get("contract_tests_missing") or []:
        lines.append("  ? contract test not in criteria: %s" % m.get("file"))
    return "\n".join(lines)


def _load_review_cfg(path: str) -> dict | None:
    """Прочитать секцию auxiliary.review из конфига Hermes.

    Конфиг - YAML, а не JSON, поэтому сначала пробуем YAML (у него есть
    под-дерево `auxiliary.review`), и только потом JSON для тестовых фикстур."""
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        print("cannot read %s: %s" % (path, exc), file=sys.stderr)
        return None
    try:
        import yaml  # noqa: PLC0415
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            section = (data.get("auxiliary") or {}).get("review")
            return section if isinstance(section, dict) else {}
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        print("cannot parse %s as YAML: %s" % (path, exc), file=sys.stderr)
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="layer 6 verdict and independence")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("independence")
    p.add_argument("--core-provider", required=True)
    p.add_argument("--core-model", required=True)
    p.add_argument("--review-provider", required=True)
    p.add_argument("--review-model", required=True)

    p2 = sub.add_parser("routes")
    p2.add_argument("--config", required=True)
    p2.add_argument("--core-provider", required=True)

    p3 = sub.add_parser("decide")
    p3.add_argument("--mechanical", required=True)
    p3.add_argument("--review", required=True)
    p3.add_argument("--core-provider", required=True)
    p3.add_argument("--core-model", required=True)
    p3.add_argument("--review-provider", default="")
    p3.add_argument("--review-model", default="")
    p3.add_argument("--review-route", default="")
    p3.add_argument("--config")
    p3.add_argument("--criteria-file")
    p3.add_argument("--core-fallback-to", default="")
    p3.add_argument("--reviewer-fallback-to", default="")

    args = ap.parse_args(argv)

    if args.cmd == "independence":
        info = independence({"provider": args.core_provider,
                             "model": args.core_model},
                            {"provider": args.review_provider,
                             "model": args.review_model})
        print("%s  %s" % (info["level"], info["reason"]))
        return 0 if info["level"] == FULL else 1

    if args.cmd == "routes":
        review_cfg = _load_review_cfg(args.config)
        if review_cfg is None:
            return 2
        bad = route_is_core_provider(review_cfg, args.core_provider)
        for item in bad:
            print("SILENT PARTIAL RISK: %s" % item)
        if not bad:
            print("no chain entry lands on the core provider")
        return 1 if bad else 0

    with open(args.mechanical, encoding="utf-8") as fh:
        mech = json.load(fh)
    with open(args.review, encoding="utf-8") as fh:
        rev = json.load(fh)
    reviewer_cfg = None
    if args.config:
        reviewer_cfg = _load_review_cfg(args.config)
        if reviewer_cfg is None:
            return 2
    criteria = None
    if args.criteria_file:
        with open(args.criteria_file, encoding="utf-8") as fh:
            criteria = fh.read()
    info = independence({"provider": args.core_provider, "model": args.core_model},
                        {"provider": args.review_provider,
                         "model": args.review_model})
    collapse = collapse_scenario(args.core_fallback_to or None,
                                 args.reviewer_fallback_to or None)
    result = decide(mech, rev, info, core_provider=args.core_provider,
                    reviewer_cfg=reviewer_cfg, collapse=collapse,
                    criteria=criteria)
    print(render(result))
    return 0 if result["task_status"] == "verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())