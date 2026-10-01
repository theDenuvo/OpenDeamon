"""Стык слоёв: механика и рецензент -> вердикт.

Существующие тесты проверяли каждый слой на РУЧНЫХ JSON-фикстурах. Фикстуры
были написаны в том формате, который реальные слои не производят, поэтому
190 зелёных групп не заметили, что слой 6 не подключён: ни mechanical.py, ни
reviewer.py не умели отдать JSON через CLI. Планировщик нашёл это сквозным
прогоном, а не тестами.

Этот набор закрывает именно стык: запускаются РЕАЛЬНЫЕ CLI, их stdout
разбирается и скармливается настоящему `verdict.decide`.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
for _d in ("baseline", "mechanical", "reviewer", "verdict"):
    _p = os.path.join(_ROOT, "functions", _d)
    if _p not in sys.path:
        sys.path.insert(0, _p)

import verdict as vd  # noqa: E402

MECH = os.path.join(_ROOT, "functions", "mechanical", "mechanical.py")
REV = os.path.join(_ROOT, "functions", "reviewer", "reviewer.py")
TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def git(*a, cwd):
    return subprocess.run(["git"] + list(a), cwd=cwd, capture_output=True,
                          text=True)


def run_json(script, args, cwd=None):
    p = subprocess.run([sys.executable, script] + list(args), cwd=cwd,
                       capture_output=True, text=True)
    try:
        return json.loads(p.stdout), p.returncode
    except ValueError:
        return None, p.returncode


def make_repo(prefix="seam-"):
    root = tempfile.mkdtemp(prefix=prefix)
    proj = os.path.join(root, "proj")
    os.makedirs(proj)
    git("init", "-q", cwd=proj)
    git("config", "user.email", "t@t", cwd=proj)
    git("config", "user.name", "t", cwd=proj)
    with open(os.path.join(proj, "app.py"), "w", encoding="utf-8") as f:
        f.write("def norm(s):\n    return s.strip()\n")
    with open(os.path.join(proj, "test_app.py"), "w", encoding="utf-8") as f:
        f.write("import app\n\n\ndef test_norm():\n"
                "    assert app.norm(' a ') == 'a'\n")
    git("add", "-A", cwd=proj)
    git("commit", "-q", "-m", "base", cwd=proj)
    return root, proj


def spec_for(checker: str) -> dict:
    return {"criteria": [{"id": "norm", "text": "norm trims whitespace",
                          "method": "command", "check": checker,
                          "severity": "LOW"}]}


@test
def test_mechanical_cli_emits_valid_json():
    """Разрыв стыка 4→6: слой 6 читает JSON-файлы, а механика отдавала текст."""
    fails = []
    root, proj = make_repo()
    try:
        import baseline as bl
        os.environ["HERMES_HOME"] = os.path.join(root, "hermes")
        bar = bl.create_barrier(proj, spec_id="seam")
        checker = os.path.join(bar["worktree"], "check_norm.py")
        with open(checker, "w", encoding="utf-8") as f:
            f.write("import app\nassert app.norm(' a ') == 'a'\n")
        spec_path = os.path.join(root, "spec.json")
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec_for("%s check_norm.py" % sys.executable), f)
        out, rc = run_json(MECH, ["run", bar["ledger_path"], bar["worktree"],
                                  "--spec", spec_path, "--no-commit", "--json"])
        if out is None:
            fails.append("mechanical --json did not produce valid JSON (rc=%s)"
                         % rc)
            return fails
        for key in ("passed", "kind", "action", "status_after",
                    "criteria_evidence", "tampering"):
            if key not in out:
                fails.append("mechanical JSON lacks %r" % key)
        if out.get("passed") is not True:
            fails.append("a clean tree did not pass: %s" % out.get("reasons"))
        rows = out.get("criteria_evidence") or []
        if not rows or rows[0].get("satisfied") is not True:
            fails.append("criterion evidence missing in JSON output: %s" % rows)
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_reviewer_cli_emits_valid_json():
    fails = []
    root, proj = make_repo()
    try:
        out, rc = run_json(REV, ["fit", "--root", proj, "--commit", "HEAD",
                                 "--json"])
        if out is None:
            fails.append("reviewer fit --json produced no JSON (rc=%s)" % rc)
        elif "chunks" not in out:
            fails.append("reviewer fit JSON lacks 'chunks': %s" % out)
        # Без сети review вернёт ok=False, но JSON обязан быть валидным.
        out2, rc2 = run_json(REV, ["review", "--root", proj, "--commit", "HEAD",
                                   "--routes", "nim", "--json"])
        if out2 is None:
            fails.append("reviewer review --json produced no JSON (rc=%s)" % rc2)
        else:
            for key in ("ok", "verdict", "attempts"):
                if key not in out2:
                    fails.append("reviewer JSON lacks %r" % key)
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_verdict_consumes_the_real_cli_output():
    """Полный стык: реальный stdout слоя 4 -> decide(). Раньше фикстуры были
    написаны руками и могли не совпадать с форматом слоёв."""
    fails = []
    root, proj = make_repo()
    try:
        import baseline as bl
        os.environ["HERMES_HOME"] = os.path.join(root, "hermes")
        bar = bl.create_barrier(proj, spec_id="seam2")
        wt = bar["worktree"]
        shutil.copy(os.path.join(proj, "test_app.py"),
                    os.path.join(wt, "test_app.py"))
        checker = os.path.join(wt, "check_norm.py")
        with open(checker, "w", encoding="utf-8") as f:
            f.write("import app\nassert app.norm(' a ') == 'a'\n")
        spec_path = os.path.join(root, "spec.json")
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec_for("%s check_norm.py" % sys.executable), f)
        out, _rc = run_json(MECH, ["run", bar["ledger_path"], wt, "--spec",
                                   spec_path, "--no-commit", "--json"])
        if out is None:
            return ["mechanical JSON unavailable; the seam cannot be tested"]
        review = {"ok": False, "verdict": None, "attempts": [],
                  "reasons": [], "error": "no network in this test"}
        info = vd.independence({"provider": "openrouter", "model": "ultra"},
                               {"provider": "nvidia", "model": "gpt-oss-20b"})
        res = vd.decide(out, review, info, core_provider="openrouter",
                        criteria="norm trims whitespace")
        if res["task_status"] == "verified":
            fails.append("a missing reviewer verdict produced verified")
        if res["kind"] != "REVIEW_UNAVAILABLE":
            fails.append("kind %s" % res["kind"])
        if not res["notes"] and not res["reasons"]:
            fails.append("the seam result carries no explanation at all")
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_check_with_a_space_in_the_path_is_not_a_silent_failure():
    """`split()` по пробелу ломал пути с пробелом: команда падала с exit=2,
    неотличимо от «проверка не прошла». На A:\\ и Z:\\ работало, поэтому
    дефект не проявлялся."""
    fails = []
    spaced = tempfile.mkdtemp(prefix="has space ")
    checker = os.path.join(spaced, "check ok.py")
    with open(checker, "w", encoding="utf-8") as f:
        f.write("assert True\n")
    argv = vd  # noqa: F841 - для ясности стека импортов
    import mechanical as mech
    got = mech.split_check('%s "check ok.py"' % sys.executable)
    if len(got) != 2:
        fails.append("split_check produced %r" % got)
    elif got[1].strip('"') != "check ok.py":
        fails.append("the quoted path was mangled: %r" % got[1])
    # Критерий с таким путём обязан исполняться, а не падать с exit=2.
    try:
        os.environ["HERMES_HOME"] = os.path.join(spaced, "hermes")
        import baseline as bl
        proj = os.path.join(spaced, "proj")
        os.makedirs(proj, exist_ok=True)
        git("init", "-q", cwd=proj)
        git("config", "user.email", "t@t", cwd=proj)
        git("config", "user.name", "t", cwd=proj)
        with open(os.path.join(proj, "a.py"), "w", encoding="utf-8") as f:
            f.write("x = 1\n")
        git("add", "-A", cwd=proj)
        git("commit", "-q", "-m", "b", cwd=proj)
        bar = bl.create_barrier(proj, spec_id="spaced")
        wt2 = bar["worktree"]
        with open(os.path.join(wt2, "check ok.py"), "w", encoding="utf-8") as f:
            f.write("assert True\n")
        out, _ = run_json(MECH, ["run", bar["ledger_path"], wt2, "--spec",
                                 _dump(spec_for('%s "check ok.py"'
                                                % sys.executable), spaced),
                                 "--no-commit", "--json"])
        if out is None:
            fails.append("no JSON for a spaced path")
        else:
            rows = out.get("criteria_evidence") or []
            if not rows or rows[0].get("exit_code") != 0:
                fails.append("a spaced path failed: %s" % rows)
    except Exception as exc:  # noqa: BLE001
        fails.append("spaced-path run raised %s: %s" % (type(exc).__name__, exc))
    finally:
        shutil.rmtree(spaced, ignore_errors=True)
    return fails


def _dump(obj, where):
    path = os.path.join(where, "spec.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    return path


@test
def test_independence_survives_none_arguments():
    fails = []
    try:
        got = vd.independence(None, None)
    except Exception as exc:  # noqa: BLE001
        return ["independence(None, None) raised %s: %s" % (type(exc).__name__, exc)]
    if got["level"] != vd.UNKNOWN:
        fails.append("independence(None, None) gave %s" % got["level"])
    for a, b in ((None, {"provider": "nvidia", "model": "m"}),
                 ({"provider": "openrouter", "model": "m"}, None)):
        try:
            if vd.independence(a, b)["level"] != vd.UNKNOWN:
                fails.append("%r vs %r did not yield UNKNOWN" % (a, b))
        except Exception as exc:  # noqa: BLE001
            fails.append("raised for %r: %s" % (a, exc))
    return fails


@test
def test_cli_modules_expose_a_json_flag():
    """Дешёвая защита от возврата разрыва: если флаг исчезнет, тест упадёт
    раньше, чем планировщик найдёт это сквозным прогоном."""
    fails = []
    for script, label in ((MECH, "mechanical"), (REV, "reviewer")):
        help_text = subprocess.run([sys.executable, script, "--help"],
                                   capture_output=True, text=True).stdout
        if "review" not in help_text and "run" not in help_text:
            fails.append("%s --help looks wrong" % label)
        p = subprocess.run([sys.executable, script, "run", "--help"]
                           if label == "mechanical"
                           else [sys.executable, script, "review", "--help"],
                           capture_output=True, text=True)
        if "--json" not in p.stdout:
            fails.append("%s subcommand has no --json" % label)
    return fails


@test
def test_cli_survives_non_ascii_in_the_payload():
    """Найдено на живом прогоне: `--json` ПАДАЛ целиком, а не частично.

    Консоль Windows в cp1251, а дифф может содержать что угодно, включая
    U+2011. Процесс умирал с UnicodeEncodeError уже после того, как результат
    был посчитан, и слой 6 получал пустой файл - то есть стык выглядел
    сломанным без всякой видимой причины. ASCII-фикстуры этого не показывали.
    """
    fails = []
    root, proj = make_repo()
    try:
        import baseline as bl
        os.environ["HERMES_HOME"] = os.path.join(root, "hermes")
        # Неразрывный дефис U+2011 и кириллица в самом проверяемом скрипте.
        with open(os.path.join(proj, "app.py"), "a", encoding="utf-8") as f:
            f.write("\n# проверка — non\u2011breaking dash, U+2011\n")
        git("add", "-A", cwd=proj)
        git("commit", "-q", "-m", "unicode", cwd=proj)
        bar = bl.create_barrier(proj, spec_id="uni")
        wt = bar["worktree"]
        checker = os.path.join(wt, "check_unicode.py")
        with open(checker, "w", encoding="utf-8") as f:
            f.write("import app\nassert app.norm('a') == 'a'\n"
                    "# комментарий с U+2011\n")
        spec_path = os.path.join(root, "spec.json")
        with open(spec_path, "w", encoding="utf-8") as f:
            json.dump(spec_for("%s check_unicode.py" % sys.executable), f,
                      ensure_ascii=False)
        p = subprocess.run([sys.executable, MECH, "run", bar["ledger_path"],
                            wt, "--spec", spec_path, "--no-commit", "--json"],
                           capture_output=True)
        if p.returncode not in (0, 1):
            fails.append("mechanical --json died with rc=%d: %s"
                         % (p.returncode, p.stderr.decode("utf-8", "replace")[-200:]))
        elif not p.stdout.strip():
            fails.append("mechanical --json produced NO output on non-ASCII")
        else:
            try:
                data = json.loads(p.stdout.decode("utf-8"))
                if "passed" not in data:
                    fails.append("JSON missing 'passed'")
            except (ValueError, UnicodeDecodeError) as exc:
                fails.append("output is not valid UTF-8 JSON: %s" % exc)
        p2 = subprocess.run([sys.executable, REV, "fit", "--root", proj,
                             "--commit", "HEAD", "--json"],
                            capture_output=True)
        if not p2.stdout.strip():
            fails.append("reviewer --json produced no output on non-ASCII")
        else:
            try:
                json.loads(p2.stdout.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                fails.append("reviewer JSON is not valid UTF-8: %s" % exc)
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


@test
def test_verdict_reads_powershell_utf16_redirection():
    """Windows PowerShell 5.1 пишет `>` как UTF-16 LE. Слой не должен падать
    трейсбеком на файле, который сам же и породил своей командой."""
    fails = []
    root = tempfile.mkdtemp(prefix="u16-")
    try:
        good = os.path.join(root, "m.json")
        with open(good, "w", encoding="utf-8") as f:
            json.dump({"passed": True, "status_after": "verified",
                       "criteria_evidence": [], "reasons": []}, f)
        with open(good, "rb") as fh:
            data = fh.read()
        with open(good, "wb") as fh:
            fh.write(b"\xff\xfe" + data.decode("utf-8").encode("utf-16-le"))
        p = subprocess.run([sys.executable, os.path.join(_ROOT, "functions",
                                                         "verdict", "verdict.py"),
                            "decide", "--mechanical", good, "--review", good,
                            "--core-provider", "openrouter",
                            "--core-model", "ultra", "--review-provider",
                            "nvidia", "--review-model", "g"],
                           capture_output=True, text=True)
        if "Traceback" in (p.stderr or ""):
            fails.append("a UTF-16 layer file produced a traceback")
        if p.returncode == 2:
            fails.append("a UTF-16 layer file was refused (rc=2): %s"
                         % (p.stderr or "").strip()[:160])
        return fails
    finally:
        shutil.rmtree(root, ignore_errors=True)


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