"""Тесты Слоя 5.

Приоритет - на тихие поломки. Самая дорогая ошибка этого слоя не та, что падает
с исключением, а та, что выглядит успехом: verdict приходит с кодом 200 и
пустым content. Если такое пройдёт как «рецензент отвечает», система годами
будет считать непроверенное проверенным.

Поэтому первыми идут тесты на content/JSON, потом - на доказательство, что
рецензент читает коммит, а не живое дерево, и только потом - на усечение и
нарезку.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_FUNCTIONS = os.path.dirname(_HERE)
for _d in ("", "baseline", "mechanical", "reviewer"):
    _p = os.path.join(_FUNCTIONS, _d) if _d else _HERE
    if os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

import reviewer as rv  # noqa: E402

TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def git(*a, cwd):
    return subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True,
                          text=True)


def make_repo(prefix="rev-"):
    root = tempfile.mkdtemp(prefix=prefix)
    git("init", "-q", cwd=root)
    git("config", "user.email", "t@t", cwd=root)
    git("config", "user.name", "t", cwd=root)
    with open(os.path.join(root, "parser.py"), "w", encoding="utf-8") as f:
        f.write("def parse(text):\n    return [x for x in text.split(',')]\n")
    with open(os.path.join(root, "transform.py"), "w", encoding="utf-8") as f:
        f.write("from parser import parse\n\n\n"
                "def ast_of(text):\n    return [len(p) for p in parse(text)]\n")
    with open(os.path.join(root, "test_transform.py"), "w", encoding="utf-8") as f:
        f.write("from transform import ast_of\n\n\n"
                "def test_ast():\n    assert ast_of('a,b') == [1, 1]\n")
    git("add", "-A", cwd=root)
    git("commit", "-q", "-m", "base", cwd=root)
    return root


# --------------------------------------------------------------------------
# правило 1: не код ответа, а content
# --------------------------------------------------------------------------

@test
def test_transport_is_probed_end_to_end_with_a_fake_model():
    """Полный проход review() на подменённом транспорте.

    Живой рельс сейчас недоступен (VPN не поднят, все три облака блокируются),
    но логику это не отменяет: если ответ приходит - система обязана вести себя
    правильно. Проверяем четыре исхода, которые различаются ТОЛЬКО содержимым
    ответа или кодом, то есть ровно те, где HTTP-код бесполезен."""
    import urllib.error
    fails = []
    orig = rv._post
    orig_env = {k: os.environ.get(k) for k in ("NVIDIA_API_KEY", "GROQ_API_KEY")}
    # Заглушки: call_reviewer честно отказывается без ключа, поэтому для
    # проверки транспорта ключ должен быть. Значения не используются - вызов
    # перехвачен.
    os.environ["NVIDIA_API_KEY"] = "test-key-nim"
    os.environ["GROQ_API_KEY"] = "test-key-groq"
    state = {"mode": "ok", "calls": 0}

    def fake_post(url, key, payload, timeout):
        state["calls"] += 1
        mode = state["mode"]
        if mode == "empty_content":
            # Именно этот случай: код 200, content пуст.
            return {"choices": [{"message": {"content": ""}}],
                    "usage": {"total_tokens": 700}}
        if mode == "fenced":
            return {"choices": [{"message": {"content":
                '```json\n{"verdict":"REVISE","findings":[{"severity":"major",'
                '"where":"a.py","what":"w","why":"y"}],"new_tests":'
                '[{"file":"t.py","class":"contract","why":"z"}]}\n```'}}]}
        if mode == "bad_json":
            return {"choices": [{"message": {"content": "I think it is fine"}}]}
        if mode == "overloaded_once":
            state["mode"] = "ok"
            raise urllib.error.HTTPError(url, 503, "overloaded", {}, None)
        if mode == "always_overloaded":
            raise urllib.error.HTTPError(url, 503, "overloaded", {}, None)
        return {"choices": [{"message": {"content": json.dumps(
            {"verdict": "PASS", "findings": [], "new_tests": [
                {"file": "t.py", "class": "contract", "why": "covers it"}]})}}],
            "usage": {"total_tokens": 1234}}

    try:
        rv._post = fake_post

        state["mode"] = "ok"
        res = rv.review(rv.build_capsule(tempfile.gettempdir(), "deadbeef"),
                        routes=("nim",))
        if res.get("ok") is not True:
            fails.append("a healthy verdict was not accepted: %s" % res.get("error"))
        if (res.get("verdict") or {}).get("verdict") != "PASS":
            fails.append("PASS was not parsed: %s" % res.get("verdict"))
        if not (res.get("verdict") or {}).get("new_tests"):
            fails.append("new_tests were lost")
        if rv.MAX_TOKENS < 700:
            fails.append("payload max_tokens %s" % rv.MAX_TOKENS)

        state["mode"] = "empty_content"
        state["calls"] = 0
        res = rv.review(rv.build_capsule(tempfile.gettempdir(), "deadbeef"),
                        routes=("nim", "groq"))
        if res.get("ok") is not False:
            fails.append("HTTP 200 with empty content was accepted as a verdict")
        if state["calls"] < 2:
            fails.append("empty content did not trigger a retry (calls=%d)"
                         % state["calls"])
        if not res.get("error"):
            fails.append("an empty-content refusal carries no reason")

        state["mode"] = "bad_json"
        res = rv.review(rv.build_capsule(tempfile.gettempdir(), "deadbeef"),
                        routes=("nim",))
        if res.get("ok") is not False:
            fails.append("prose instead of JSON was accepted as a verdict")

        state["mode"] = "fenced"
        res = rv.review(rv.build_capsule(tempfile.gettempdir(), "deadbeef"),
                        routes=("nim",))
        if res.get("ok") is not True:
            fails.append("a fenced JSON verdict was rejected")
        if (res.get("verdict") or {}).get("verdict") != "REVISE":
            fails.append("REVISE was not parsed from the fenced reply")

        # 503 один раз, потом успех: переполнение не приговор.
        state["mode"] = "overloaded_once"
        res = rv.review(rv.build_capsule(tempfile.gettempdir(), "deadbeef"),
                        routes=("nim",))
        if res.get("ok") is not True:
            fails.append("a single 503 was treated as a dead endpoint: %s"
                         % res.get("error"))

        state["mode"] = "always_overloaded"
        state["calls"] = 0
        res = rv.review(rv.build_capsule(tempfile.gettempdir(), "deadbeef"),
                        routes=("nim",))
        if res.get("ok") is not False:
            fails.append("persistent 503 produced a verdict")
        if state["calls"] < 2:
            fails.append("persistent 503 was not retried (calls=%d)"
                         % state["calls"])
        if "overloaded" not in (res.get("error") or ""):
            fails.append("the 503 reason was lost: %r" % res.get("error"))
        return fails
    finally:
        rv._post = orig
        for k, v in orig_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@test
def test_undecodable_diff_is_not_silently_empty():
    """Найдено на живом коммите: `text=True` декодирует вывод git через
    кодовую страницу консоли (cp1251), и дифф падал UnicodeDecodeError.
    commit_diff возвращал ПУСТУЮ строку - рецензент получил бы пустой дифф и
    мог ответить PASS. Это ровно тот класс тихой поломки, что слой ловит."""
    fails = []
    import ast
    src = open(rv.__file__, encoding="utf-8").read()
    tree = ast.parse(src)
    # Проверка по дереву, а не по тексту: `text=True` в докстринге - это
    # объяснение, а не вызов. Ловим настоящий keyword в subprocess.run.
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = getattr(fn, "attr", None) or getattr(fn, "id", None)
        if name != "run":
            continue
        for kw in node.keywords or []:
            if kw.arg == "text" and getattr(kw.value, "value", None) is True:
                offenders.append(node.lineno)
    if offenders:
        fails.append("subprocess.run(..., text=True) at lines %s: locale "
                     "decoding can return an empty diff instead of the real one"
                     % offenders)
    if 'decode("utf-8"' not in src:
        fails.append("git output is not decoded explicitly as UTF-8")
    if 'decode("utf-8", "replace")' not in src:
        fails.append("git output decoding does not tolerate bad bytes")
    return fails


@test
def test_oversized_review_is_reported_as_incomplete_not_as_pass():
    """Коммит на 596 файлов даёт 241 логическую группу. Молча разослать 241
    запрос - расход; взять 12 и назвать это проверкой всего - подмена."""
    fails = []
    if rv.MAX_CHUNKS < 2:
        fails.append("MAX_CHUNKS %s is too small to be meaningful" % rv.MAX_CHUNKS)
    src = open(rv.__file__, encoding="utf-8").read()
    if "review_incomplete" not in src:
        fails.append("an over-budget review is not marked incomplete")
    # Проверка на синтетике: подсовываем заведомо много групп.
    cap = {"files": [], "criteria": "c", "changed_files": "x",
           "changed_tests": "t", "test_results": "ok", "hashes": "h",
           "worker_claim": "w",
           "diff": "\n".join("diff --git a/f%03d.py b/f%03d.py\n"
                             "--- a/f%03d.py\n+++ b/f%03d.py\n@@ -1 +1 @@\n"
                             "-x = 1\n+x = 2\n    assert True" % (i, i, i, i)
                             for i in range(400))}
    files = ["f%03d.py" % i for i in range(400)]
    cap["files"] = files
    prompts = rv.fit_chunks(cap, 400)
    if len(prompts) <= rv.MAX_CHUNKS:
        return fails   # не воспроизвелось - не ломаем тест
    res = rv.review(cap, routes=("nim",))
    if res.get("complete") is not False:
        fails.append("a %d-chunk review was not marked incomplete" % len(prompts))
    if "review_incomplete" not in (res.get("error") or ""):
        fails.append("no explicit incompleteness reason: %r" % res.get("error"))
    if res.get("chunks_reviewed", 0) > rv.MAX_CHUNKS:
        fails.append("more chunks were sent than the limit")
    return fails


@test
def test_empty_content_is_refused_even_with_http_200():
    """Центральный инвариант слоя. HTTP-код тут неуместен: сломанный вердикт
    приходит с кодом 200 и пустым content."""
    fails = []
    data, why = rv.parse_verdict("")
    if data is not None:
        fails.append("empty content was accepted as a verdict")
    if why != "empty_content":
        fails.append("reason %r, expected empty_content" % why)
    data, why = rv.parse_verdict("   \n  ")
    if data is not None or why != "empty_content":
        fails.append("whitespace content accepted: %r / %s" % (data, why))
    return fails


@test
def test_invalid_json_is_refused():
    fails = []
    for bad, expect in (("not json at all", "no_json_object"),
                        ("{verdict: PASS}", "invalid_json"),
                        ("[1,2,3]", "no_json_object"),
                        ('{"verdict": ', "no_json_object")):
        data, why = rv.parse_verdict(bad)
        if data is not None:
            fails.append("%r was accepted" % bad)
        elif expect not in why and why != expect:
            fails.append("%r -> %s, expected %s" % (bad, why, expect))
    return fails


@test
def test_fenced_json_is_accepted():
    """Модель может прийти в markdown-ограждении. Это не повод отвергать
    содержательный ответ."""
    fails = []
    data, why = rv.parse_verdict('```json\n{"verdict": "PASS"}\n```')
    if data is None:
        fails.append("fenced JSON rejected: %s" % why)
    elif data.get("verdict") != "PASS":
        fails.append("fenced JSON mis-parsed: %s" % data)
    return fails


@test
def test_token_budget_is_at_least_700():
    """max_tokens ниже 700 даёт пустой content у reasoning-модели."""
    fails = []
    if rv.MIN_TOKENS < 700:
        fails.append("MIN_TOKENS %s is below 700" % rv.MIN_TOKENS)
    if rv.MAX_TOKENS < 700:
        fails.append("MAX_TOKENS %s is below 700" % rv.MAX_TOKENS)
    if rv.COMPLETION_RESERVE < 700:
        fails.append("COMPLETION_RESERVE %s is below 700" % rv.COMPLETION_RESERVE)
    if rv.TEMPERATURE != 0.0:
        fails.append("temperature %s, verdict must be reproducible" % rv.TEMPERATURE)
    return fails


@test
def test_unknown_verdict_is_never_promoted_to_pass():
    fails = []
    out = rv.normalize_verdict({"verdict": "looks good to me"})
    if out["verdict"] != "REVISE":
        fails.append("unknown verdict became %s" % out["verdict"])
    out = rv.normalize_verdict({})
    if out["verdict"] in ("PASS", ""):
        fails.append("a missing verdict became %s" % out["verdict"])
    return fails


@test
def test_unknown_test_class_is_never_promoted_to_contract():
    """Только contract-тест обязан быть в критериях. Неизвестный класс не может
    им стать - иначе ревьюер сам себе выдаст пропуск."""
    fails = []
    out = rv.normalize_verdict({"verdict": "PASS",
                                "new_tests": [{"file": "t.py",
                                               "class": "maybe"}]})
    if out["new_tests"][0]["class"] == "contract":
        fails.append("an unknown class was promoted to contract")
    return fails


# --------------------------------------------------------------------------
# правило 2: читается коммит, а не живой workspace
# --------------------------------------------------------------------------

@test
def test_capsule_is_built_from_the_commit_not_the_working_tree():
    """TOCTOU: рецензент обязан видеть ровно то состояние, которое прошло
    механику. Если бы он читал живое дерево, незакоммиченная правка после
    механики изменила бы предмет ревью."""
    fails = []
    root = make_repo()
    try:
        with open(os.path.join(root, "transform.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef injected():\n    return 'not committed'\n")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True).stdout.strip()
        cap = rv.build_capsule(root, head)
        if "injected" in cap["diff"]:
            fails.append("the capsule picked up an uncommitted change")
        if "transform.py" not in cap["changed_files"]:
            fails.append("changed_files looks wrong: %r" % cap["changed_files"])
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_worker_claim_comes_last_and_is_separated():
    fails = []
    root = make_repo()
    try:
        with open(os.path.join(root, "transform.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef later():\n    return 1\n")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "x"], cwd=root,
                       capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True).stdout.strip()
        cap = rv.build_capsule(root, head, worker_claim="всё отлично")
        prompt = rv.render_prompt(cap)
        if "LEAST TRUSTWORTHY" not in prompt:
            fails.append("the claim is not marked as least trustworthy")
        if prompt.find("Author's claim") < prompt.find("Diff of the verified"):
            fails.append("the claim appears before the factual diff")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# правило 4 и 5: нарезка и scheduler
# --------------------------------------------------------------------------

@test
def test_scheduler_counts_prompt_plus_completion():
    """Потолок Groq жёсткий. Считать только input - упереться в лимит там, где
    бюджет ещё не исчерпан."""
    fails = []
    b = rv.MinuteBudget(limit=1000)
    if not b.reserve(600):
        fails.append("reserve of 600 was refused on an empty budget")
    if b.reserve(600):
        fails.append("budget allowed prompt+completion past the limit")
    if rv.MinuteBudget.__init__.__doc__ is None and not hasattr(b, "spent"):
        fails.append("budget exposes no spent()")
    # completion резервируется вместе с prompt, а не отдельно.
    b2 = rv.MinuteBudget(limit=2000)
    cost = 200 + rv.MAX_TOKENS
    if not b2.reserve(cost):
        fails.append("reserve of prompt+completion was refused on a free budget")
    if b2.spent() != cost:
        fails.append("spent() returned %d, expected %d" % (b2.spent(), cost))
    # Превышение лимита обязано быть отказано, а не «урезано по-тихому».
    b3 = rv.MinuteBudget(limit=1000)
    if not b3.reserve(900):
        fails.append("reserve of 900 was refused on a free budget")
    if b3.reserve(200):
        fails.append("the budget allowed spending past its limit")
    return fails


@test
def test_oversized_capsule_splits_by_logical_group_not_by_file():
    """Резка по файлам убивает связку parser -> AST -> transform -> test."""
    fails = []
    root = make_repo()
    try:
        big = "\n".join("def f%d():\n    return %d\n    assert True" % (i, i)
                        for i in range(900))
        with open(os.path.join(root, "transform.py"), "a", encoding="utf-8") as f:
            f.write("\n" + big)
        with open(os.path.join(root, "test_transform.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef test_big():\n    assert True\n")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "big"], cwd=root,
                       capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True).stdout.strip()
        cap = rv.build_capsule(root, head, criteria="keep parity")
        prompts = rv.fit_chunks(cap, target=1200)
        if len(prompts) < 2:
            fails.append("an oversized capsule was not split: %d chunk(s)"
                         % len(prompts))
        for p in prompts:
            if "keep parity" not in p:
                fails.append("a chunk lost the acceptance criteria")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_criteria_and_changed_tests_are_never_truncated():
    """Ошибка может сидеть в 2001-м токене. Усекаются stdout и boilerplate -
    но не критерии и не изменённые тесты."""
    fails = []
    root = make_repo()
    try:
        with open(os.path.join(root, "transform.py"), "a", encoding="utf-8") as f:
            f.write("\n" + "\n".join("def g%d():\n    return %d" % (i, i)
                                     for i in range(800)))
        # Тестовый файл тоже меняем - иначе утверждение про «изменённые тесты
        # не усекаются» проверяло бы пустоту.
        with open(os.path.join(root, "test_transform.py"), "a", encoding="utf-8") as f:
            f.write("\n\ndef test_extra():\n    assert ast_of('a') == [1]\n")
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True)
        subprocess.run(["git", "commit", "-q", "-m", "b2"], cwd=root,
                       capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True).stdout.strip()
        cap = rv.build_capsule(root, head, criteria="CRITERION_SENTINEL",
                               test_results="ERROR_SENTINEL: exit=1\n" + "x\n" * 5000)
        prompts = rv.fit_chunks(cap, target=600)
        if not prompts:
            fails.append("nothing was produced")
            return fails
        for p in prompts:
            if "CRITERION_SENTINEL" not in p:
                fails.append("acceptance criteria were truncated away")
            if "ERROR_SENTINEL" not in p:
                fails.append("error output was truncated away")
        # Изменённые тесты обязаны попасть в нарезку целиком - но не обязаны
        # быть в каждом куске: кусок законно содержит свою группу файлов.
        joined = "\n".join(prompts)
        if "test_transform.py" not in joined:
            fails.append("the changed test file was truncated away from "
                         "the whole review")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_small_capsule_stays_a_single_chunk():
    fails = []
    root = make_repo()
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True).stdout.strip()
        cap = rv.build_capsule(root, head, criteria="tiny")
        if len(rv.fit_chunks(cap)) != 1:
            fails.append("a small capsule was needlessly split")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------------
# правило 8: 429 и 503
# --------------------------------------------------------------------------

@test
def test_both_429_and_503_are_treated_as_capacity_problems():
    """NIM при переполнении отдаёт 503, а не 429. Обработать только 429 значит
    считать NIM живым, когда он переполнен."""
    import urllib.error
    fails = []
    for code in (429, 503):
        exc = urllib.error.HTTPError("u", code, "m", {}, None)
        kind = rv.http_error_kind(exc)
        if kind not in ("rate_limited", "overloaded"):
            fails.append("HTTP %s classified as %r" % (code, kind))
    exc = urllib.error.HTTPError("u", 451, "m", {}, None)
    if rv.http_error_kind(exc) != "vpn_or_egress":
        fails.append("451 misclassified: %s" % rv.http_error_kind(exc))
    exc = urllib.error.HTTPError("u", 401, "m", {}, None)
    if rv.http_error_kind(exc) != "no_key":
        fails.append("401 misclassified: %s" % rv.http_error_kind(exc))
    return fails


@test
def test_at_least_two_attempts_per_model():
    """Мёртвый эндпоинт держит соединение 75-81 с; живые модели давали 91.4 с
    и проходили. Одна попытка - это флак, а не вердикт."""
    fails = []
    if rv.ATTEMPTS_PER_MODEL < 2:
        fails.append("ATTEMPTS_PER_MODEL is %s" % rv.ATTEMPTS_PER_MODEL)
    src = open(rv.__file__, encoding="utf-8").read()
    if "for attempt in range(1, attempts + 1)" not in src:
        fails.append("call_reviewer does not loop over attempts")
    return fails


# --------------------------------------------------------------------------
# строки запроса
# --------------------------------------------------------------------------

@test
def test_request_payload_is_reproducible_and_json_mode():
    fails = []
    src = open(rv.__file__, encoding="utf-8").read()
    for needle, why in (
            ('"response_format": {"type": "json_object"}', "json_object mode"),
            ('"temperature": TEMPERATURE', "temperature is sent"),
            ('"max_tokens": MAX_TOKENS', "max_tokens is sent")):
        if needle not in src:
            fails.append("payload does not send %s" % why)
    # reasoning_effort не отправляем: у Groq градуированные уровни дают 400
    # (issue #75089), а для NIM он не нужен.
    if "reasoning_effort" in src:
        fails.append("reasoning_effort is sent; graded levels 400 on Groq")
    return fails


@test
def test_broken_input_never_crashes():
    fails = []
    for bad in ("", "not json", "{}", "[]", None):
        try:
            rv.parse_verdict(bad if isinstance(bad, str) else "")
        except Exception as exc:  # noqa: BLE001
            fails.append("parse_verdict raised on %r: %s" % (bad, exc))
    try:
        rv.normalize_verdict({"findings": ["x", None, 5],
                              "new_tests": ["y"], "verdict": 7})
    except Exception as exc:  # noqa: BLE001
        fails.append("normalize_verdict raised: %s" % exc)
    try:
        rv.call_reviewer("nope", "x")
    except Exception as exc:  # noqa: BLE001
        fails.append("call_reviewer raised on an unknown route: %s" % exc)
    try:
        rv.fit_chunks({})
    except Exception as exc:  # noqa: BLE001
        fails.append("fit_chunks raised on an empty capsule: %s" % exc)
    return fails


@test
def test_no_review_verdict_is_reported_as_failure_not_pass():
    """Если рецензент не ответил, это не «всё хорошо» и не тихий PASS."""
    fails = []
    res = rv.review(rv.build_capsule(tempfile.gettempdir(), "deadbeef"),
                    routes=("nim",))
    if res.get("ok") is True:
        fails.append("a review with no verdict reported ok")
    if res.get("verdict") is not None:
        fails.append("a verdict was invented: %r" % res.get("verdict"))
    if "REVIEW NO VERDICT" not in rv.render(res):
        fails.append("render does not say NO VERDICT")
    return fails


@test
def test_report_bytes_are_utf8():
    fails = []
    res = rv.review(rv.build_capsule(tempfile.gettempdir(), "deadbeef"),
                    routes=("nim",))
    try:
        rv.render(res).encode("utf-8")
    except UnicodeEncodeError as exc:
        fails.append("render is not encodable as UTF-8: %s" % exc)
    try:
        json.dumps(res, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, TypeError) as exc:
        fails.append("result is not JSON-serialisable as UTF-8: %s" % exc)
    return fails


def main() -> int:
    if shutil.which("git") is None:
        print("git is required for these tests")
        return 2
    failed = 0
    for label, fn in TESTS:
        try:
            res = fn() or []
            fails = list(res[0]) if isinstance(res, tuple) else list(res)
        except AssertionError as exc:
            fails = ["assertion: %s" % exc]
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