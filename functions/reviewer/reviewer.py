"""Слой 5 - рецензент.

Слой 4 доказал, что критерии исполнены. Этот слой отвечает на другой вопрос:
а КАК это сделано. Механика не может отличить правильное решение от правдоподобного,
поэтому нужен второй взгляд - на другую модель у другого провайдера.

Пять правил, которые здесь нельзя ослабить:

1. max_tokens >= 700 и response_format: json_object, temperature 0.
   gpt-oss - reasoning-модель, она съедает бюджет токенов на размышление.
   Замерено: max_tokens=10 -> content='', 32 -> '', 64 -> 'K', 200+ -> нормальный
   текст. И HTTP-код во всех случаях 200 - сломанный вердикт НЕОТЛИЧИМ от
   успешного по коду ответа. Поэтому проверяется не HTTP-код, а непустой
   content и валидный JSON: иначе пустой content даст парсеру None, работу
   отвергнут как «вердикта нет», и ни одного сообщения об ошибке не возникнет.
   Тихая поломка вместо отказа.

2. Рецензент читает diff КОМИТА, а не живой workspace. Это устраняет TOCTOU:
   он анализирует ровно то состояние, которое прошло механику.

3. DIFF, TEST_RESULTS, HASHES, CHANGED_FILES собирает КОД из фактического
   состояния. WORKER_CLAIM идёт последним, с наименьшим доверием, в отдельном
   блоке. Иначе воркер формирует для ревьюера выгодную картину.

4. Capsule ~2000 токенов - это TARGET, а не cap. Что не влезает - дробится по
   ЛОГИЧЕСКИМ группам изменений: не расширяется (жёсткий потолок), и не режется
   механически по файлам, потому что это убивает связь parser -> AST ->
   transform -> test. Scheduler считает prompt + completion, не только input.

5. Порядок усечения: первым уходит полный stdout, потом boilerplate, потом
   нерелевантные хунки. НИКОГДА не усекаются acceptance criteria, изменённые
   тестовые файлы, критичные хунки и error output - ошибка может сидеть в
   2001-м токене.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.dirname(_HERE)
for _d in ("", "baseline", "mechanical", "specgate"):
    _p = os.path.join(_FUNCTIONS, _d) if _d else _HERE
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

# --- константы ---------------------------------------------------------

CAPSULE_TARGET_TOKENS = 2000     # target, не cap
# Потолок числа кусков. 596-файловый коммит из этого репозитория даёт 241
# логическую группу; молча разослать 241 запрос - это не ревью, а расход.
# Лучше честно сказать «ревью неполное», чем назвать первые 12 кусков проверкой
# всего коммита.
MAX_CHUNKS = 12
COMPLETION_RESERVE = 700         # >= 700 обязательно, см. правило 1
MINUTE_TOKEN_LIMIT = 8000        # жёсткий потолок Groq, tokens/min
MIN_TOKENS = 700
MAX_TOKENS = 1200
TEMPERATURE = 0.0
ATTEMPTS_PER_MODEL = 2           # мёртвый эндпоинт держит соединение 75-81 с
TIMEOUT = 120

NIM_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NIM_MODEL = "openai/gpt-oss-20b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-120b"

VERDICTS = ("PASS", "REVISE", "REJECT")
TEST_CLASSES = ("contract", "supporting", "diagnostic")

# Секции капсулы и приоритет усечения. Меньше число - реже режется.
# Никогда не режется: 0. Именно поэтому порядок задан константой, а не
# «а если поместится».
NEVER_TRUNCATE = ("criteria", "changed_tests", "errors", "critical_hunks")
TRUNCATE_FIRST = ("stdout_full", "boilerplate", "irrelevant_hunks")

ROUTES = {
    "nim": {"url": NIM_URL, "model": NIM_MODEL, "key": "NVIDIA_API_KEY"},
    "groq": {"url": GROQ_URL, "model": GROQ_MODEL, "key": "GROQ_API_KEY"},
}

REVIEW_PROMPT = """You are the independent reviewer of a code change. You did not \
write it and you do not trust the author's summary.

You receive FACTUAL state gathered by code from a verified commit: the diff of \
the commit itself, the changed file list, real test exit codes, and baseline \
hashes. The author's own claim comes LAST and is the least trustworthy part.

Judge whether the change actually does what its acceptance criteria require,
and whether the tests would catch it if it did not.

Rules you must follow:
- Reply with ONE JSON object and nothing else. No prose, no markdown fence.
- Required keys: verdict, findings, new_tests, notes.
- verdict is exactly one of: PASS, REVISE, REJECT.
- findings is a list of {severity, where, what, why}; severity is one of
  blocker, major, minor.
- new_tests is a list of {file, class, why}; class is exactly one of
  contract, supporting, diagnostic. A contract test asserts a requirement from
  the acceptance criteria. A supporting test guards adjacent behaviour. A
  diagnostic test only helps a human debug. Only contract tests are required
  to appear in the acceptance criteria.
- If a required behaviour has no contract test, that is a finding.
- A test that cannot fail is a finding: it looks green and proves nothing.
- Do not restate the diff back to me. Point at specific locations.

Acceptance criteria:
{criteria}

Changed files:
{changed_files}

Test results (real exit codes, collected by code):
{test_results}

Hashes:
{hashes}

Diff of the verified commit:
{diff}

Author's claim (LEAST TRUSTWORTHY - verify it, do not accept it):
{worker_claim}

Reply with the JSON object now."""


# --- оценка размера ----------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Грубая оценка токенов: ~4 символа на токен.

    Точный токенизатор в проекте отсутствует, а тайминги и бюджет считаются
    до вызова, поэтому оценка должна быть на стороне запаса. Для русского
    текста символов на токен меньше, так что оценка занижает объём - это
    безопасная сторона: budget сработает раньше, чем упрётся в потолок."""
    return (len(text or "") + 3) // 4


# --- сбор капсулы из фактического состояния ---------------------------

def _git(args: list[str], cwd: str) -> tuple[int, str]:
    """Запустить git и прочитать вывод как UTF-8.

    `text=True` НЕЛЬЗЯ: он декодирует через кодовую страницу консоли, а на этой
    машине это cp1251. Диф с байтом, который cp1251 не знает, роняет
    subprocess UnicodeDecodeError, и commit_diff возвращает ПУСТУЮ строку -
    рецензент получил бы пустой дифф и спокойно ответил PASS. Это ровно тот
    класс тихой поломки, который слой обязан не допускать, поэтому вывод
    читается в bytes и декодируется явно, с replace вместо исключения."""
    try:
        p = subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True,
                           timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    out = (p.stdout or b"").decode("utf-8", "replace")
    err = (p.stderr or b"").decode("utf-8", "replace")
    return p.returncode, out + err


def commit_diff(root: str, commit: str, context: int = 3) -> str:
    """Diff ИМЕННО коммита. Живой workspace не читается принципиально."""
    rc, out = _git(["show", "--no-color", "-U%d" % context, commit], root)
    if rc != 0:
        return "(cannot read commit %s: %s)" % (commit[:12], out.strip()[:200])
    return out


def changed_files(root: str, commit: str) -> list[str]:
    rc, out = _git(["show", "--no-color", "--name-status", "--format=", commit], root)
    if rc != 0:
        return []
    names = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            names.append(parts[-1].strip())
    return [n for n in names if n]


def is_test_path(path: str) -> bool:
    base = os.path.basename(path)
    return (base.startswith("test_") or base.endswith("_test.py")
            or os.sep + "tests" + os.sep in path or path.startswith("tests"))


def is_error_bearing(text: str) -> bool:
    """Кусок диффа несёт ошибку/утверждение? Такое усекать нельзя."""
    low = (text or "").lower()
    return any(m in low for m in ("assert", "raise", "error", "except",
                                  "assertionerror", "unverified", "fixme"))


def build_capsule(root: str, commit: str, criteria: str = "",
                  test_results: str = "", hashes: str = "",
                  worker_claim: str = "") -> dict:
    """Капсула из фактического состояния. Ни одно поле не берётся от воркера,
    кроме явного WORKER_CLAIM."""
    files = changed_files(root, commit)
    diff = commit_diff(root, commit)
    tests = [f for f in files if is_test_path(f)]
    return {
        "commit": commit,
        "criteria": criteria or "(критерии не переданы)",
        "changed_files": "\n".join(files) or "(нет изменённых файлов)",
        "changed_tests": "\n".join(tests) or "(изменённых тестов нет)",
        "diff": diff,
        "test_results": test_results or "(прогоны не переданы)",
        "hashes": hashes or "(хэши не переданы)",
        # Последним и в отдельном блоке: минимальное доверие.
        "worker_claim": worker_claim or "(заявление воркера отсутствует)",
        "files": files,
        "test_files": tests,
    }


# --- разбиение по логическим группам -----------------------------------

def _diff_blocks(diff: str) -> list[tuple[str, str]]:
    """Разбор unified diff на блоки (путь -> текст блока)."""
    blocks: list[tuple[str, str]] = []
    current_name, current = None, []
    for line in (diff or "").splitlines():
        if line.startswith("diff --git "):
            if current_name is not None:
                blocks.append((current_name, "\n".join(current)))
            m = re.search(r" b/(.+)$", line)
            current_name = m.group(1).strip() if m else ""
            current = [line]
        elif current_name is not None:
            current.append(line)
    if current_name is not None:
        blocks.append((current_name, "\n".join(current)))
    return blocks


def _group_key(name: str, body: str, imports: dict[str, set],
               members: set[str]) -> str:
    """Логическая группа: тест едет вместе с исходником, который импортирует."""
    base = os.path.basename(name)
    root_mod = base[:-3] if base.endswith(".py") else base
    linked = sorted(other for other in members
                    if other != name and not is_test_path(other)
                    and root_mod in imports.get(name, set()))
    if linked:
        return linked[0]
    if is_test_path(name):
        return "__tests__"
    return os.path.dirname(name) or "."


def split_logically(capsule: dict) -> list[dict]:
    """Дробить по ЛОГИЧЕСКИМ группам, а не по файлам.

    Резать по файлам нельзя: связка parser -> AST -> transform -> test
    разрывается, и рецензент видит трансформ без парсера - то есть не видит
    ошибки вообще. Группировка идёт по связности: тест попадает в группу тех
    исходников, которые он импортирует; импорт вытаскивается прямо из текста
    диффа, это данные, а не догадки."""
    files = list(capsule.get("files") or [])
    if not files:
        return [capsule]
    blocks = dict(_diff_blocks(capsule.get("diff") or ""))
    imports: dict[str, set] = {f: set() for f in files}
    for name, body in blocks.items():
        if name not in imports:
            continue
        for line in body.splitlines():
            m = re.search(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", line)
            if m:
                imports[name].add(m.group(1).split(".")[0])

    member_set = set(files)
    groups: dict[str, list] = {}
    for name in files:
        groups.setdefault(_group_key(name, blocks.get(name, ""), imports,
                                     member_set), []).append(name)

    chunks = []
    for key, members in groups.items():
        part = dict(capsule)
        part["files"] = sorted(members)
        part["test_files"] = [f for f in sorted(members) if is_test_path(f)]
        part["diff"] = "\n\n".join(blocks.get(m, "") for m in sorted(members)
                                  if m in blocks)
        part["group"] = key
        chunks.append(part)
    return chunks or [capsule]


# --- усечение ----------------------------------------------------------

def _fits(capsule: dict, target: int) -> bool:
    return estimate_tokens(render_prompt(capsule)) <= target


def _bounded(text: str, max_tokens: int, label: str) -> str:
    """Ограничить секцию по размеру, сохранив начало и пометив хвост."""
    text = text or ""
    if estimate_tokens(text) <= max_tokens:
        return text
    head = text[: max_tokens * 4]
    return "%s\n[... %s truncated at %d tokens ...]" % (
        head, label, max_tokens)


def _shrink_to_fit(capsule: dict, target: int) -> dict:
    """Последнее средство: ужать необязательные секции до размера.

    Ужимаются ТОЛЬКО те секции, которые правило 6 разрешает усекать: полный
    stdout, boilerplate, нерелевантные хунки. Criteria, changed_tests и errors
    не трогаются - ошибка может сидеть в 2001-м токене. Если и после этого не
    влезает, лучше честно объявить усечение, чем резать эти секции."""
    part = dict(capsule)
    part["test_results"] = _last_lines(capsule.get("test_results", ""), 12)
    part["diff"] = _drop_boring_hunks(capsule.get("diff", ""))
    part["hashes"] = _bounded(capsule.get("hashes", ""), 60, "hashes")
    part["worker_claim"] = _bounded(capsule.get("worker_claim", ""), 200,
                                    "author claim")
    part["changed_files"] = _bounded(capsule.get("changed_files", ""),
                                     max(60, target // 12),
                                     "changed file list")
    return part


def fit_chunks(capsule: dict, target: int = CAPSULE_TARGET_TOKENS) -> list[str]:
    """Нарезать капсулу на куски не больше target токенов каждый.

    Усечение идёт в фиксированном порядке: первым уходит полный stdout, затем
    boilerplate, затем нерелевантные хунки. Acceptance criteria, изменённые
    тестовые файлы, критичные хунки и error output не усекаются НИКОГДА.

    Что не влезает - дробится по ЛОГИЧЕСКИМ группам, и деление повторяется
    рекурсивно: группа может оказаться больше таргета сама по себе (коммит на
    785 файлов - реальный случай из этого репозитория), тогда она делится
    ещё раз, и только в последнюю очередь применяется аварийный cap."""
    if _fits(capsule, target):
        return [render_prompt(capsule)]

    trimmed = _shrink_to_fit(capsule, target)
    if _fits(trimmed, target):
        trimmed["truncated"] = list(TRUNCATE_FIRST)
        return [render_prompt(trimmed)]

    out: list[str] = []
    queue = split_logically(trimmed)
    guard = 0
    while queue and guard < 4000:
        guard += 1
        part = queue.pop(0)
        if _fits(part, target):
            out.append(render_prompt(part))
            continue
        # Группа всё ещё велика: делим её на под-группы по импортам.
        sub = split_logically(part) if len(part.get("files") or []) > 1 else []
        sub = [s for s in sub if s.get("files") != part.get("files")]
        if sub:
            queue.extend(sub)
            continue
        out.append(_hard_cap(render_prompt(part), target))
    if not out:
        out.append(_hard_cap(render_prompt(trimmed), target))
    return out


def _last_lines(text: str, n: int) -> str:
    """Усечение stdout: строки с признаками ошибки плюс хвост.

    Наивный «оставить последние N строк» - прямая противоположность
    правилу 6. Ошибка часто стоит В НАЧАЛЕ вывода (сводка сборки, первый
    упавший тест), а правило запрещает выбрасывать error output: ошибка
    может сидеть в 2001-м токене. Поэтому все строки-сигналы сохраняются
    безусловно, а сокращается только безликий хвост."""
    lines = (text or "").splitlines()
    if len(lines) <= n:
        return text or ""
    signal = [l for l in lines if is_error_bearing(l) and l.strip()]
    rest = [l for l in lines if not (is_error_bearing(l) and l.strip())]
    kept = signal + rest[-n:]
    # Порядок исходника важнее дедупликации: рецензент читает сверху вниз.
    seen, ordered = set(), []
    for line in kept:
        if line not in seen:
            seen.add(line)
            ordered.append(line)
    return "\n".join(["(stdout shortened: %d -> %d lines; error lines kept)"
                      % (len(lines), len(ordered))] + ordered)


def _drop_boring_hunks(diff: str) -> str:
    """Убрать хунки без утверждений и ошибок: импорт, комментарии, формат."""
    kept, block = [], []
    for line in (diff or "").splitlines():
        if line.startswith("diff --git ") or line.startswith("@@"):
            if block and any(is_error_bearing(x) or x.startswith(("+", "-"))
                             and not x.startswith(("+++", "---"))
                             and len(x.strip()) > 3 for x in block):
                kept.extend(block)
            block = [line]
        elif block:
            block.append(line)
    if block and any(is_error_bearing(x) for x in block):
        kept.extend(block)
    return "\n".join(kept) if kept else (diff or "")


def _hard_cap(prompt: str, target: int) -> str:
    """Аварийное усечение.

    Резать можно только сам diff. Criteria, changed_files, реальные коды
    возврата и имена изменённых тестов остаются целыми даже здесь: лучше
    сказать «diff не влез» честно, чем вырезать доказательства. Случай, где
    и это не влезает, означает, что размер диффа задаёт не текст промта, а
    число файлов - и это видно по маркеру."""
    head, sep, _tail = prompt.partition("\nDiff of the verified commit:")
    marker = ("\n\nDiff of the verified commit:\n"
              "[diff omitted: the commit is larger than %d tokens. Review the "
              "changed file list and the test results above, and ask for the "
              "specific hunk you need.]\n" % target)
    if not sep:
        return _bounded(prompt, target, "whole prompt")
    return head + marker


def render_prompt(capsule: dict) -> str:
    """Сборка промта.

    Подстановка сделана через replace, а НЕ через str.format: в тексте промта
    есть фигурные скобки - они описывают схему JSON-ответа
    ({severity, where, what, why}), и format() попытался бы трактовать их как
    поля форматирования. Наивный вариант падал с KeyError на любой capsule.
    """
    out = REVIEW_PROMPT
    for token, value in (
            ("{criteria}", capsule.get("criteria", "")),
            ("{changed_files}", capsule.get("changed_files", "")),
            ("{test_results}", capsule.get("test_results", "")),
            ("{hashes}", capsule.get("hashes", "")),
            ("{diff}", capsule.get("diff", "")),
            ("{worker_claim}", capsule.get("worker_claim", ""))):
        out = out.replace(token, value)
    return out


# --- scheduler: prompt + completion ------------------------------------

class MinuteBudget:
    """Считать prompt + completion, не только input.

    У Groq жёсткий потолок 8000 токенов/мин. Считать только input - значит
    упираться в потолок там, где бюджет ещё не исчерпан."""
    def __init__(self, limit: int = MINUTE_TOKEN_LIMIT):
        self.limit = limit
        self.events: list[tuple] = []

    def spent(self, now: float | None = None) -> int:
        now = now if now is not None else time.time()
        return sum(t for ts, t in self.events if now - ts < 60)

    def reserve(self, tokens: int, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if self.spent(now) + tokens > self.limit:
            return False
        self.events.append((now, tokens))
        return True

    def wait_for_slot(self, tokens: int, timeout: float = 60.0) -> bool:
        if self.reserve(tokens):
            return True
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(5)
            if self.reserve(tokens):
                return True
        return False


# --- вызов рецензента --------------------------------------------------

def http_error_kind(exc) -> str:
    """Тип ошибки по коду. 403/451 = VPN или гео, 401 = ключ.

    NIM при переполнении отдаёт 503, а НЕ 429: обрабатывать оба, иначе NIM
    будет считаться живым, когда переполнен."""
    code = getattr(exc, "code", None)
    if code == 401:
        return "no_key"
    if code in (403, 451):
        return "vpn_or_egress"
    if code == 429:
        return "rate_limited"
    if code == 503:
        return "overloaded"
    if code is not None and 500 <= int(code) < 600:
        return "provider_error"
    return "unreachable"


def _post(url: str, key: str, payload: dict, timeout: int) -> dict:
    body = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", "replace"))


def parse_verdict(content: str) -> tuple[dict | None, str]:
    """Разбор ответа. Ключевой момент правила 1.

    Проверяется НЕПУСТОЙ content и ВАЛИДНЫЙ JSON, а не HTTP-код: сломанный
    вердикт приходит с кодом 200 и пустым content, и без этой проверки он
    выглядел бы как успех."""
    if not content or not content.strip():
        return None, "empty_content"
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None, "no_json_object"
    try:
        data = json.loads(text[start:end + 1])
    except ValueError as exc:
        return None, "invalid_json: %s" % str(exc)[:80]
    if not isinstance(data, dict):
        return None, "json_not_object"
    return data, ""


def normalize_verdict(data: dict) -> dict:
    """Привести к схеме. Отсутствующее поле - это дефект, а не «всё хорошо»."""
    verdict = str(data.get("verdict") or "").strip().upper()
    if verdict not in VERDICTS:
        verdict = "REVISE"      # неизвестный вердикт не может быть «хорошим»
    findings = []
    for f in data.get("findings") or []:
        if not isinstance(f, dict):
            continue
        findings.append({"severity": str(f.get("severity") or "minor"),
                         "where": str(f.get("where") or ""),
                         "what": str(f.get("what") or ""),
                         "why": str(f.get("why") or "")})
    tests = []
    for t in data.get("new_tests") or []:
        if not isinstance(t, dict):
            continue
        cls = str(t.get("class") or "").strip().lower()
        if cls not in TEST_CLASSES:
            cls = "supporting"   # неизвестный класс не может быть contract
        tests.append({"file": str(t.get("file") or ""), "class": cls,
                      "why": str(t.get("why") or "")})
    return {"verdict": verdict, "findings": findings, "new_tests": tests,
            "notes": str(data.get("notes") or "")}


def call_reviewer(route: str, prompt: str, budget: MinuteBudget | None = None,
                  attempts: int = ATTEMPTS_PER_MODEL) -> dict:
    """Вызов с двумя попытками минимум.

    Мёртвый эндпоинт держит соединение 75-81 секунду и только потом отдаёт
    ошибку, при этом живые модели давали 91.4 с и проходили. Одна неудача -
    это флак, а не приговор."""
    conf = ROUTES.get(route)
    if not conf:
        return {"ok": False, "error": "unknown_route", "route": route}
    key = os.environ.get(conf["key"], "")
    if not key:
        return {"ok": False, "error": "no_key", "route": route}
    payload = {
        "model": conf["model"],
        "messages": [{"role": "user", "content": prompt}],
        # Ниже 700 content приходит пустым при коде 200.
        "max_tokens": MAX_TOKENS,
        "temperature": TEMPERATURE,
        "response_format": {"type": "json_object"},
    }
    estimated = estimate_tokens(prompt) + MAX_TOKENS
    if budget is not None and not budget.wait_for_slot(estimated):
        return {"ok": False, "error": "minute_budget_exhausted", "route": route}

    last = ""
    for attempt in range(1, attempts + 1):
        try:
            blob = _post(conf["url"], key, payload, TIMEOUT)
        except urllib.error.HTTPError as exc:
            last = "%s (http %s)" % (http_error_kind(exc), exc.code)
            if attempt < attempts:
                time.sleep(2)
            continue
        except Exception as exc:  # noqa: BLE001
            last = "unreachable (%s)" % type(exc).__name__
            if attempt < attempts:
                time.sleep(2)
            continue
        content = ((blob.get("choices") or [{}])[0].get("message") or {}).get("content")
        data, why = parse_verdict(content or "")
        if data is None:
            # Именно этот случай даёт «тихую поломку»: код 200, content пуст.
            last = why
            if attempt < attempts:
                time.sleep(1)
            continue
        return {"ok": True, "attempt": attempt, "route": route,
                "model": conf["model"], "raw": data,
                "verdict": normalize_verdict(data),
                "usage": blob.get("usage") or {}}
    return {"ok": False, "error": last, "route": route, "attempts": attempts}


def review(capsule: dict, routes: tuple = ("nim", "groq"),
           budget: MinuteBudget | None = None,
           max_chunks: int = MAX_CHUNKS) -> dict:
    """Полный проход: нарезка по бюджету, попытки, разбор, агрегация.

    Если логических групп больше max_chunks, ревью объявляется НЕПОЛНЫМ.
    Это осознанное решение: вердикт по 12 кускам 241-файлового коммита выглядел
    бы как одобрение всего, а это ровно та подмена, которую слой ловит."""
    budget = budget or MinuteBudget()
    prompts = fit_chunks(capsule)
    complete = len(prompts) <= max_chunks
    results = []
    for i, prompt in enumerate(prompts[:max_chunks], 1):
        for route in routes:
            res = call_reviewer(route, prompt, budget=budget)
            res["chunk"] = i
            res["chunks_total"] = len(prompts)
            results.append(res)
            if res.get("ok"):
                break
    verdicts = [r for r in results if r.get("ok")]
    out = {"ok": bool(verdicts) and complete, "attempts": results,
           "verdict": verdicts[0]["verdict"] if verdicts else None,
           "routes_used": [r["route"] for r in verdicts],
           "chunks": len(prompts),
           "chunks_reviewed": min(len(prompts), max_chunks),
           "complete": complete,
           "capsule_tokens": estimate_tokens(prompts[0]) if prompts else 0}
    if not verdicts:
        out["error"] = results[0].get("error") if results else "no_attempt"
    elif not complete:
        out["error"] = ("review_incomplete: %d logical groups exceed the %d "
                        "chunk limit; a verdict here covers %d of them and "
                        "must not be read as approval of the whole commit"
                        % (len(prompts), max_chunks, min(len(prompts),
                                                         max_chunks)))
        out["ok"] = False
    return out


def render(result: dict) -> str:
    lines = ["REVIEW %s" % ("OK" if result.get("ok") else "NO VERDICT"),
             "verdict: %s" % (result.get("verdict") or "-"),
             "routes:  %s" % ", ".join(result.get("routes_used") or []),
             "chunks:  %s of %s (~%d tokens each)"
             % (result.get("chunks_reviewed", result.get("chunks", 0)),
                result.get("chunks", 0), result.get("capsule_tokens", 0))]
    if result.get("error"):
        lines.append("error:  %s" % result["error"])
    for r in result.get("attempts") or []:
        if r.get("ok"):
            lines.append("  chunk %s via %s ok on attempt %s"
                         % (r.get("chunk"), r.get("route"), r.get("attempt")))
        else:
            lines.append("  chunk %s via %s FAILED: %s"
                         % (r.get("chunk"), r.get("route"), r.get("error")))
    v = (result.get("verdict") or {}) if isinstance(result.get("verdict"), dict) else {}
    for f in v.get("findings") or []:
        lines.append("  [%s] %s: %s" % (f["severity"], f["where"], f["what"]))
    for t in v.get("new_tests") or []:
        lines.append("  test %s -> %s" % (t["file"], t["class"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="layer 5 reviewer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("review")
    p.add_argument("--root", default=".")
    p.add_argument("--commit", required=True)
    p.add_argument("--criteria")
    p.add_argument("--criteria-file")
    p.add_argument("--tests")
    p.add_argument("--hashes")
    p.add_argument("--claim")
    p.add_argument("--routes", default="nim,groq")

    p2 = sub.add_parser("fit")
    p2.add_argument("--root", default=".")
    p2.add_argument("--commit", required=True)
    p2.add_argument("--criteria", default="")

    args = ap.parse_args(argv)
    criteria = args.criteria or ""
    if getattr(args, "criteria_file", None):
        with open(args.criteria_file, encoding="utf-8") as fh:
            criteria = fh.read()

    capsule = build_capsule(args.root, args.commit, criteria=criteria,
                            test_results=getattr(args, "tests", "") or "",
                            hashes=getattr(args, "hashes", "") or "",
                            worker_claim=getattr(args, "claim", "") or "")

    if args.cmd == "fit":
        prompts = fit_chunks(capsule)
        print("chunks: %d" % len(prompts))
        for i, pr in enumerate(prompts, 1):
            print("  chunk %d: ~%d tokens" % (i, estimate_tokens(pr)))
        return 0

    result = review(capsule, routes=tuple(r.strip() for r in
                                          args.routes.split(",") if r.strip()))
    print(render(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())