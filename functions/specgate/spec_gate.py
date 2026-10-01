#!/usr/bin/env python
"""spec_gate — слой 0 «спека-гейт» схемы делегации v3.

Эталон: `SCHEME.md` §4 (блок 0), §5.1, §6, §8 (инварианты 1-2) и
`TODO.md` Фаза 2-ter, слой 1. Здесь реализован только слой 0. Слои 2-7
(baseline, state machine, механика, рецензент, статусы) — отдельные модули.

Что делает
----------
Две ноги, как в эталоне.

**0a. Механика — всегда, для каждого критерия.** Сопоставляет критерию
метод проверки из `test | command | invariant | snapshot | manual` и
возвращает потолок статуса:

    test | command | invariant | snapshot  -> ceiling VERIFIED
    manual                               -> ceiling JUDGED  (никогда VERIFIED)
    нет метода                           -> UNVERIFIABLE + способ суждения

**0b. LLM — только по триггерам риска.** Изменение существующего кода,
>3 критериев, изменение API/протокола, state/persistence,
concurrency/async, destructive operation. NIM `openai/gpt-oss-20b`.
Лимит 2 цикла reject, потом эскалация человеку.

Границы, которые модуль держит механически
-----------------------------------------
1. **Гейт не может выдать `VERIFIED`.** Он выдаёт потолок и назначает
   метод, но не факт. Факт появляется только в слое 4 (механика), и то
   после сравнения хэша baseline-тестов. Это инвариант №1 из §8 плюс
   §10: «Механика не даёт права выдать verified». Инвариант проверяется
   функцией `_ceiling()`, а тест `test_manual_never_reaches_verified`
   ловит попытку ослабить его.
2. **`manual` → `JUDGED`, навсегда.** Причина не формальная: иначе
   «метод существует ⇒ критерий верифицирован» становится возможной
   логикой, и граница verified перестаёт означать что-либо (§5.1).
3. **Объявленный метод без проверяемого `check` — это `UNVERIFIABLE`,
   а не верифицируемый критерий.** Это тот же обход с другой стороны:
   `method: test` без имени теста ничего не проверяет, но выглядит как
   «метод есть». Объявление метода и возможность его выполнить — разные
   вещи, и гейт считает их разными.
4. **`UNVERIFIABLE` ≠ стоп.** Работа продолжается, на выходе `judged`.
   Эскалация (`>2 UNVERIFIABLE` либо любой критичный) — это запрос
   человеку ДО начала работы, а не запрет (§5.1).
5. **Спека, которую нечем проверить на выходе, обязана называть
   `judged_ceiling`.** Критерии перечисляются поимённо (§6).

Что модуль НЕ делает
--------------------
Ничего, что стоит денег без разрешения владельца: закон $0. LLM-нога
идёт на NIM free и отказывается модели, которая не проходит проверку
бесплатности (`_free_tier_ok`). Ключ берётся из `$NVIDIA_API_KEY` и
никогда не печатается.

Известные ловушки, закрытые здесь (замерено 2026-09-30, SCHEME.md §3)
--------------------------------------------------------------------------
* **gpt-oss съедает `max_tokens` на reasoning.** При малом бюджете
  возвращает пустой `content` при HTTP 200. Проверяем не код ответа, а
  непустой `content` и валидный JSON; `max_tokens` >= 700, `temperature: 0`.
* **NIM отдаёт 503, а не 429**, при переполнении. Обрабатываются оба.
* **Без VPN NIM отдаёт 451**, OpenRouter/Groq — 403. 451/403 не имеют
  отношения к качеству спеки, поэтому это `INFRA`, а не `SPEC_FAILURE`:
  иначе сетевая проблема съест цикл rework спеки.

Интерфейс
---------
CLI:  ``py spec_gate.py spec.json`` | ``--json`` | ``--no-llm`` | ``--stdin``
Библиотека: ``spec_gate.run(spec) -> dict``

Читает:  stdin или путь к JSON-спеки.
Пишет:   stdout (отчёт или JSON), `$HERMES_HOME/cache/spec-gate.json`
         (только счётчик циклов reject, fail-open).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_MODEL = "openai/gpt-oss-20b"
MAX_TOKENS = 700          # см. ловушку gpt-oss в docstring
TIMEOUT = 60
MAX_REJECT_CYCLES = 2     # «лимит 2 цикла reject» (TODO.md, слой 1 п.5)

# Порог эскалации. Строгое «больше 2», то есть 3 UNVERIFIABLE уже
# эскалация. Число не магическое: это защита от накопления
# субъективности, а счёт критериев не равен их весу (§5.1).
UNVERIFIABLE_ESCALATE_AT = 3

AUTO_METHODS = ("test", "command", "invariant", "snapshot")
MANUAL_METHOD = "manual"
KNOWN_METHODS = AUTO_METHODS + (MANUAL_METHOD,)

METHOD_ALIASES = {
    "unit test": "test", "unit-test": "test", "tests": "test",
    "pytest": "test", "assertion": "invariant", "assert": "invariant",
    "property test": "invariant",
    "cli": "command", "shell": "command", "script": "command",
    "golden": "snapshot", "golden file": "snapshot", "reference": "snapshot",
    "checksum": "snapshot", "file hash": "snapshot",
    "human": "manual", "review": "manual", "human judgement": "manual",
    "human judgment": "manual", "субъективно": "manual",
}

CEILING_VERIFIED = "VERIFIED"
CEILING_JUDGED = "JUDGED"

STATUS_AUTO = "auto"
STATUS_MANUAL = "manual"
STATUS_UNVERIFIABLE = "unverifiable"

OUTCOME_PROCEED = "PROCEED"
OUTCOME_ESCALATE = "ESCALATE"
OUTCOME_REWORK_SPEC = "REWORK_SPEC"
OUTCOME_INFRA = "INFRA_UNAVAILABLE"

REASON_NO_METHOD = "no_method"
REASON_MANUAL = "manual_by_nature"
REASON_MANUAL_NO_JUDGE = "manual_without_judging_method"
REASON_NO_CHECK = "method_without_check"
REASON_UNKNOWN_METHOD = "unknown_method"
REASON_EMPTY = "empty_criterion"

RISK_EXISTING_CODE = "existing_code"
RISK_MANY_CRITERIA = "many_criteria"
RISK_API = "api_or_protocol"
RISK_STATE = "state_or_persistence"
RISK_CONCURRENCY = "concurrency_or_async"
RISK_DESTRUCTIVE = "destructive"

# Триггеры риска — ловушка против «LLM проверяет спеку всегда». Проверка
# каждой спеки на NIM стоит времени и квоты, а большинство задач
# (новый файл, текст, переименование) ловятся механикой. Порог из
# эталонной схемы: LLM-нога включается только по этим шести.
RISK_PATTERNS = (
    (RISK_EXISTING_CODE, re.compile(
        r"\b(existing|legacy|current|modify|change|refactor|rewrite|"
        r"modify|patch|migrate|backward|compat)\w*\b", re.I)),
    (RISK_API, re.compile(
        r"\b(api|endpoint|protocol|schema|request|response|contract|"
        r"payload|signature|interface|cli|flag|argument|versioning)\b", re.I)),
    (RISK_STATE, re.compile(
        r"\b(state|persist\w*|storag\w*|database|db|cache|checkpoint|"
        r"migration|write.*disk|save|restore|session)\b", re.I)),
    (RISK_CONCURRENCY, re.compile(
        r"\b(async|await|concurren\w*|thread|lock|mutex|parallel|"
        r"race|deadlock|timeout|background|queue)\b", re.I)),
    (RISK_DESTRUCTIVE, re.compile(
        r"\b(delete|remove|drop|destroy|truncate|wipe|reset|overwrite|"
        r"migrate.*down|rollback|irreversible)\w*\b", re.I)),
)

# Критерий без текста — сам по себе эскалация: нечего проверять и
# нечего отдавать воркеру.
EMPTY_CRITERION_REASON = "empty_criterion"


# --------------------------------------------------------------------------
# нормализация спеки
# --------------------------------------------------------------------------

def _str(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def spec_id(spec: dict) -> str:
    """Стабильный короткий id спеки. Один и тот же файл на повторных
    прогонах обязан давать один и тот же id, иначе счётчик циклов reject
    обнуляется и лимит 2 становится бесконечным."""
    canonical = json.dumps(spec, sort_keys=True, ensure_ascii=False,
                           default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def normalize_method(raw) -> str:
    """Метод → канонический вид, либо '' если неизвестен.

    Неизвестный метод НЕ превращается в manual и не отбрасывается: он
    становится UNVERIFIABLE с причиной unknown_method. Иначе опечатка
    (`tets`) тихо выдавала бы за верифицируемый критерий или за
    осознанное решение человека — оба варианта врут."""
    text = _str(raw).lower()
    if not text:
        return ""
    if text in KNOWN_METHODS:
        return text
    return METHOD_ALIASES.get(text, "")


def normalize_criterion(raw, index: int) -> dict:
    """Приводит критерий к виду, с которым работает механика.

    Принимает строку (тогда метода нет — критерий UNVERIFIABLE) и
    словарь. Метод вытаскивается из `method` / `verify_by` /
    `check_method`, чтобы воркер-спека могла называть поле как угодно:
    отсутствие метода — это UNVERIFIABLE, а не ошибка разбора."""
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return {"id": "C%d" % (index + 1), "text": "", "method": "",
                "check": "", "judge_by": "", "critical": False,
                "status": STATUS_UNVERIFIABLE, "reason": EMPTY_CRITERION_REASON,
                "ceiling": CEILING_JUDGED}

    text = _str(raw.get("text") or raw.get("criterion")
                or raw.get("requirement") or raw.get("description"))
    declared = (raw.get("method") or raw.get("verify_by")
                or raw.get("check_method") or raw.get("verification"))
    method = normalize_method(declared)
    check = _str(raw.get("check") or raw.get("command")
                 or raw.get("test") or raw.get("probe"))
    judge_by = _str(raw.get("judge_by") or raw.get("judgement")
                    or raw.get("judgment") or raw.get("how_to_judge"))

    critical = raw.get("critical", raw.get("risk", False))
    if isinstance(critical, str):
        critical = critical.strip().lower() in ("1", "true", "yes", "critical")

    return {
        "id": _str(raw.get("id")) or "C%d" % (index + 1),
        "text": text,
        "declared_method": _str(declared),
        "method": method,
        "check": check,
        "judge_by": judge_by,
        "critical": bool(critical),
        "status": STATUS_AUTO,
        "reason": "",
        "ceiling": CEILING_VERIFIED,
    }


def normalize_spec(raw) -> dict:
    if isinstance(raw, str):
        raw = {"goal": raw}
    if not isinstance(raw, dict):
        raw = {}
    criteria_raw = (raw.get("criteria") or raw.get("acceptance")
                    or raw.get("acceptance_criteria") or [])
    if isinstance(criteria_raw, dict):
        criteria_raw = [criteria_raw]
    if isinstance(criteria_raw, str):
        criteria_raw = [criteria_raw]
    return {
        "id": _str(raw.get("id")) or spec_id(raw),
        "goal": _str(raw.get("goal") or raw.get("task") or raw.get("title")),
        "criteria": [normalize_criterion(c, i)
                     for i, c in enumerate(criteria_raw)],
        "flags": raw.get("flags") if isinstance(raw.get("flags"), dict) else {},
        "context": raw.get("context") if isinstance(raw.get("context"), str) else "",
    }


# --------------------------------------------------------------------------
# 0a. механика — всегда
# --------------------------------------------------------------------------

def _ceiling(method: str) -> str:
    """Потолок статуса по методу. Инвариант №1 живёт здесь.

    `manual` физически не может дать VERIFIED: единственный путь к
    CEILING_VERIFIED — один из AUTO_METHODS. Проверяется тестом
    test_manual_never_reaches_verified; если кто-то расширит этот
    список, тест упадёт."""
    return CEILING_JUDGED if method in ("", MANUAL_METHOD) else CEILING_VERIFIED


def classify(criterion: dict) -> dict:
    """Один критерий → status + reason + ceiling. Никаких сетевых вызовов."""
    method = criterion.get("method") or ""

    if not _str(criterion.get("text")):
        # Пустой критерий автоматически критичный: нечего передать
        # воркеру и нечего судить человеку. Обычный UNVERIFIABLE — это
        # «спроси владельца, сходится ли», здесь вопрос бессмысленен,
        # поэтому критерий не должен тихо уехать в работу. Автоматически
        # критичный пустой критерий — единственное место, где гейт сам
        # повышает критичность.
        criterion["status"] = STATUS_UNVERIFIABLE
        criterion["reason"] = REASON_EMPTY
        criterion["ceiling"] = CEILING_JUDGED
        criterion["critical"] = True
        return criterion

    declared = _str(criterion.get("declared_method"))
    if not method and declared:
        criterion["status"] = STATUS_UNVERIFIABLE
        criterion["reason"] = REASON_UNKNOWN_METHOD
        criterion["ceiling"] = CEILING_JUDGED
        return criterion

    if not method:
        criterion["status"] = STATUS_UNVERIFIABLE
        criterion["reason"] = REASON_NO_METHOD
        criterion["ceiling"] = CEILING_JUDGED
        return criterion

    if method == MANUAL_METHOD:
        criterion["status"] = STATUS_UNVERIFIABLE
        criterion["reason"] = (REASON_MANUAL if _str(criterion.get("judge_by"))
                               else REASON_MANUAL_NO_JUDGE)
        criterion["ceiling"] = _ceiling(MANUAL_METHOD)
        return criterion

    if not _str(criterion.get("check")):
        criterion["status"] = STATUS_UNVERIFIABLE
        criterion["reason"] = REASON_NO_CHECK
        criterion["ceiling"] = CEILING_JUDGED
        return criterion

    criterion["status"] = STATUS_AUTO
    criterion["reason"] = ""
    criterion["ceiling"] = _ceiling(method)
    return criterion


def mechanical(spec: dict) -> dict:
    """Механическая часть гейта. Работает всегда, без сети.

    Возвращает разбор по критериям, счётчики и факт эскалации по
    инварианту №2. Эскалация — запрос человеку ДО работы, не запрет."""
    criteria = [classify(c) for c in spec.get("criteria") or []]
    unverifiable = [c for c in criteria if c["status"] == STATUS_UNVERIFIABLE]
    manual = [c for c in criteria if c.get("method") == MANUAL_METHOD]

    escalate_reasons = []
    if len(unverifiable) >= UNVERIFIABLE_ESCALATE_AT:
        escalate_reasons.append(
            "unverifiable_count: %d UNVERIFIABLE >= %d — эскалация человеку "
            "ДО начала работы" % (len(unverifiable), UNVERIFIABLE_ESCALATE_AT))
    for c in unverifiable:
        if c.get("critical"):
            escalate_reasons.append(
                "critical_unverifiable: %s — критичный UNVERIFIABLE "
                "эскалирует независимо от счётчика" % c["id"])

    return {
        "criteria": criteria,
        "counts": {
            "total": len(criteria),
            "auto": len([c for c in criteria if c["status"] == STATUS_AUTO]),
            "manual": len(manual),
            "unverifiable": len(unverifiable),
            "critical_unverifiable": len([c for c in unverifiable
                                          if c.get("critical")]),
        },
        "unverifiable_ids": [c["id"] for c in unverifiable],
        "judged_ids": [c["id"] for c in criteria
                       if c["ceiling"] == CEILING_JUDGED],
        "escalate": bool(escalate_reasons),
        "escalate_reasons": escalate_reasons,
    }


# --------------------------------------------------------------------------
# 0b. триггеры риска
# --------------------------------------------------------------------------

def detect_risk(spec: dict, mech: dict) -> dict:
    """Шесть триггеров риска из эталона. Ни одного вызова, если они
    не сработали — тогда LLM-нога не нужна вовсе."""
    fired = {}
    counts = mech["counts"]

    if counts["total"] > 3:
        fired[RISK_MANY_CRITERIA] = "%d критериев (>3)" % counts["total"]

    blob = " ".join([spec.get("goal", ""), spec.get("context", "")]
                    + [c.get("text", "") for c in mech["criteria"]])
    for name, pattern in RISK_PATTERNS:
        match = pattern.search(blob)
        if match:
            fired[name] = match.group(0)

    for name, value in (spec.get("flags") or {}).items():
        if value and name not in fired:
            fired[name] = "flag:%s" % name

    return {"fired": fired, "count": len(fired)}


# --------------------------------------------------------------------------
# 0b. LLM-нога (NIM free, только по триггерам)
# --------------------------------------------------------------------------

def _free_tier_ok(model: str) -> bool:
    """Закон $0 проверяется до вызова, а не после.

    Отказоустойчивость тут обратная привычной: сомнительную модель мы
    НЕ пробуем «и посмотрим». Один платный вызов нарушает закон
    проекта, и заметить его по счёту можно будет не сразу."""
    m = _str(model).lower()
    if ":free" in m:
        return True
    return m.startswith(("nvidia/", "groq/", "openai/gpt-oss-"))


def api_key() -> str:
    for name in ("NVIDIA_API_KEY", "NVIDIA_NIM_KEY"):
        value = os.environ.get(name)
        if value:
            return value.strip()
    return ""


def home() -> str:
    return os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes"))


def state_path() -> str:
    path = os.path.join(home(), "cache", "spec-gate.json")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError:
        pass
    return path


def load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    """Fail-open: гейт не имеет права упасть из-за кэша счётчика. Если
    запись не удалась, цикл reject просто не запомнится — это хуже, чем
    не блокировать работу, и лучше, чем исключение на ровном месте."""
    try:
        with open(state_path(), "w", encoding="utf-8") as f:
            json.dump(state, f)
    except OSError:
        pass


REVIEW_PROMPT = """\
You review an implementation spec BEFORE any code is written. Your job is to
find specs that cannot be implemented without guessing.

Criteria (verbatim):
{criteria}

Report, as JSON only:
- "verdict": "accept" if a competent engineer could implement every criterion
  without inventing a requirement. "reject" otherwise.
- "ambiguous": list of criterion ids whose wording allows two readings that
  lead to different code.
- "guessed": list of criterion ids requiring a requirement that is NOT in the
  spec (missing behaviour, missing error case, missing boundary).
- "reason": one short sentence.

A criterion that cannot be machine-checked is NOT a reason to reject: it is
judged, not verified. Reject only for genuine ambiguity or missing
requirements."""


def _review_payload(spec: dict, mech: dict) -> dict:
    lines = []
    for c in mech["criteria"]:
        lines.append("- [%s] %s%s" % (
            c["id"], c["text"] or "(empty)",
            " [critical]" if c.get("critical") else ""))
    return {
        "model": NIM_MODEL,
        "messages": [
            {"role": "system",
             "content": "You are a spec reviewer. Output JSON only."},
            {"role": "user",
             "content": REVIEW_PROMPT.format(criteria="\n".join(lines))},
        ],
        "temperature": 0,
        "max_tokens": MAX_TOKENS,
        "response_format": {"type": "json_object"},
    }


def call_nim(payload: dict, key: str, timeout: int = TIMEOUT) -> dict:
    """Один вызов NIM. Возвращает сырой dict HTTP-ответа.

    Ключ уходит в заголовке и не возвращается наружу."""
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        NIM_URL, data=body, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def _http_error_kind(exc) -> str:
    """Разделяем «спека плохая» и «сеть/ключи сломаны».

    Иначе 451 из-за VPN будет выглядеть как недостаток спеки и съест
    цикл rework спеки, которого не существует (§6: INFRA не rework)."""
    code = getattr(exc, "code", None)
    if code == 401:
        return "no_key"
    if code in (403, 451):
        return "vpn_or_egress"
    if code == 429:
        return "rate_limited"
    if code == 503:
        return "overloaded"      # NIM при переполнении отдаёт 503
    if code is not None and 500 <= int(code) < 600:
        return "provider_error"
    return "unreachable"


def review_with_llm(spec: dict, mech: dict, model: str = NIM_MODEL) -> dict:
    """LLM-проверка спеки. Никогда не поднимает исключение.

    Проверяется не HTTP-код, а непустой `content` и валидный JSON:
    gpt-oss при малом `max_tokens` отдаёт пустую строку при 200."""
    if not _free_tier_ok(model):
        return {"state": "blocked_paid_model",
                "reason": "model %r is not a free tier; the $0 law is not "
                          "negotiable and this gate never auto-pays" % model}
    key = api_key()
    if not key:
        return {"state": "no_key",
                "reason": "NVIDIA_API_KEY is not set in the environment"}

    try:
        raw = call_nim(_review_payload(spec, mech), key)
    except urllib.error.HTTPError as exc:
        return {"state": "infra", "kind": _http_error_kind(exc),
                "code": getattr(exc, "code", None),
                "reason": "NIM HTTP %s" % getattr(exc, "code", "?")}
    except (urllib.error.URLError, OSError, json.JSONDecodeError,
            TimeoutError) as exc:
        return {"state": "infra", "kind": "unreachable",
                "reason": "%s: %s" % (type(exc).__name__, exc)}

    try:
        content = (raw.get("choices") or [{}])[0].get("message", {}).get("content")
    except (AttributeError, IndexError, TypeError):
        content = None
    if not _str(content):
        return {"state": "empty_content",
                "reason": "HTTP 200 with empty content — the gpt-oss "
                          "max_tokens trap; a missing verdict is not a "
                          "pass and not a fail"}

    text = _str(content)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {"state": "bad_json", "reason": "no JSON object in content",
                "content": text[:200]}
    try:
        data = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        return {"state": "bad_json", "reason": str(exc), "content": text[:200]}

    verdict = _str(data.get("verdict")).lower()
    if verdict not in ("accept", "reject"):
        return {"state": "bad_json",
                "reason": "verdict %r is neither accept nor reject" % verdict,
                "content": text[:200]}
    return {
        "state": "ok",
        "verdict": verdict,
        "ambiguous": data.get("ambiguous") or [],
        "guessed": data.get("guessed") or [],
        "reason": _str(data.get("reason")),
        "model": model,
    }


# --------------------------------------------------------------------------
# счётчик циклов reject
# --------------------------------------------------------------------------

def _cycles(state: dict, key: str) -> dict:
    entry = state.get(key)
    if not isinstance(entry, dict):
        entry = {}
    entry.setdefault("rejects", 0)
    entry.setdefault("first_seen", time.time())
    return entry


# --------------------------------------------------------------------------
# сборка решения
# --------------------------------------------------------------------------

def run(spec_raw, use_llm: bool = True, state: dict | None = None) -> dict:
    """Полный прогон гейта. Возвращает dict — тот же, что уходит в stdout.

    Порядок именно такой: механика → триггеры → LLM только если
    сработали → решение. Эскалация по инварианту №2 не зависит от
    того, ответила ли сеть: если критериев-без-метода много, человек
    нужен всё равно."""
    spec = normalize_spec(spec_raw)
    mech = mechanical(spec)
    risk = detect_risk(spec, mech)

    key = spec["id"]
    own_state = state is None
    if state is None:
        state = load_state()
    entry = _cycles(state, key)

    review = {"state": "skipped_no_risk", "reason":
              "no risk trigger fired; the mechanical pass is the gate"}
    outcome = OUTCOME_PROCEED
    reasons = list(mech["escalate_reasons"])

    if mech["escalate"]:
        outcome = OUTCOME_ESCALATE

    if use_llm and risk["count"] and outcome != OUTCOME_ESCALATE:
        review = review_with_llm(spec, mech)
        if review["state"] == "ok" and review["verdict"] == "reject":
            entry["rejects"] += 1
            reasons.append("SPEC_FAILURE: рецензент отклонил спеку (%s)" %
                           (review.get("reason") or "без причины"))
            if entry["rejects"] > MAX_REJECT_CYCLES:
                outcome = OUTCOME_ESCALATE
                reasons.append(
                    "reject_cycles_exhausted: %d циклов > лимита %d — "
                    "эскалация человеку" % (entry["rejects"],
                                            MAX_REJECT_CYCLES))
            else:
                outcome = OUTCOME_REWORK_SPEC
        elif review["state"] in ("infra", "no_key", "empty_content",
                                 "bad_json", "blocked_paid_model"):
            reasons.append(
                "INFRA: LLM-нога не дала вердикта (%s/%s) — это не дефект "
                "спеки и не повод переписывать её"
                % (review["state"], review.get("kind", review.get("reason", ""))[:60]))
            outcome = (OUTCOME_ESCALATE if outcome == OUTCOME_ESCALATE
                       else OUTCOME_PROCEED)

    entry["last_seen"] = time.time()
    entry["last_outcome"] = outcome
    state[key] = entry
    if own_state:
        save_state(state)

    judged = mech["judged_ids"]
    return {
        "spec_id": key,
        "goal": spec["goal"],
        "outcome": outcome,
        "proceed": outcome == OUTCOME_PROCEED,
        "worker_may_start": outcome in (OUTCOME_PROCEED,),
        "reasons": reasons,
        "counts": mech["counts"],
        "risk": risk,
        "review": review,
        "criteria": mech["criteria"],
        "unverifiable_ids": mech["unverifiable_ids"],
        "judged_ids": judged,
        "judged_ceiling": "judged" if judged else "verified_candidate",
        "exit_ceiling": CEILING_JUDGED if judged else CEILING_VERIFIED,
        "rejects": entry["rejects"],
        "reject_cycles_used": "%d/%d" % (entry["rejects"], MAX_REJECT_CYCLES),
        "gate_cannot_verify": (
            "этот гейт назначает метод и потолок статуса; VERIFIED "
            "появляется только в слое 4 после сверки хэша baseline-тестов"),
    }


# --------------------------------------------------------------------------
# вывод
# --------------------------------------------------------------------------

def render(result: dict) -> str:
    counts = result["counts"]
    lines = []
    lines.append("SPEC GATE  %s" % result["outcome"])
    if result["goal"]:
        lines.append("goal: %s" % result["goal"])
    lines.append("criteria: %d total | %d auto | %d manual | %d UNVERIFIABLE"
                 % (counts["total"], counts["auto"], counts["manual"],
                    counts["unverifiable"]))
    lines.append("risk triggers: %d%s" % (
        result["risk"]["count"],
        (" -> " + ", ".join(sorted(result["risk"]["fired"])))
        if result["risk"]["count"] else " -> none, LLM leg skipped"))

    for c in result["criteria"]:
        mark = {"auto": "AUTO", "manual": "MANUAL",
                "unverifiable": "UNVERIF"}[c["status"]]
        lines.append("  [%s] %-6s ceiling=%-8s %s" % (
            c["id"], mark, c["ceiling"],
            (c["text"] or "(empty)")[:90]))
        if c["reason"]:
            detail = c["reason"]
            if c["reason"] in (REASON_MANUAL, REASON_MANUAL_NO_JUDGE) \
                    and c.get("judge_by"):
                detail += " (judge_by: %s)" % c["judge_by"]
            if c["reason"] == REASON_MANUAL_NO_JUDGE:
                detail += " — способ суждения не задан"
            lines.append("         reason: %s" % detail)

    for reason in result["reasons"]:
        lines.append("! %s" % reason)

    if result["judged_ids"]:
        lines.append("judged criteria (на выходе НЕ verified): %s"
                     % ", ".join(result["judged_ids"]))
    if result["worker_may_start"]:
        lines.append("=> гейт пройден: воркер может стартовать. "
                     "VERIFIED появится только после слоя 4.")
    else:
        lines.append("=> воркер НЕ стартует. %s" % result["outcome"])
    return "\n".join(lines)


def load_source(args) -> dict:
    if getattr(args, "stdin", False):
        raw = sys.stdin.read()
    else:
        path = getattr(args, "spec", None)
        if not path:
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = f.read()
        except OSError as exc:
            raise SystemExit("cannot read spec %r: %s" % (path, exc))
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise SystemExit("spec is not valid JSON: %s" % exc)


def main(argv=None) -> int:
    import argparse

    # The report is Russian, the console on this machine is cp866/cp1251
    # while every reader of this output (Hermes, a log, `py ... > file`)
    # expects UTF-8. Left alone, Python encodes to cp1251 and the reader
    # gets mozibake - a gate nobody can read is a gate nobody obeys.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass

    parser = argparse.ArgumentParser(
        description="spec gate: criterion -> verification method, always.")
    parser.add_argument("spec", nargs="?", help="path to a JSON spec")
    parser.add_argument("--json", action="store_true",
                        help="machine-readable result on stdout")
    parser.add_argument("--stdin", action="store_true",
                        help="read the spec from stdin")
    parser.add_argument("--no-llm", action="store_true",
                        help="mechanical pass only, never touch the network")
    args = parser.parse_args(argv)

    if not args.spec and not args.stdin:
        parser.error("give a spec path or --stdin")

    result = run(load_source(args), use_llm=not args.no_llm)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json
          else render(result))
    return 0 if result["outcome"] in (OUTCOME_PROCEED,
                                      OUTCOME_REWORK_SPEC) else 1


if __name__ == "__main__":
    raise SystemExit(main())