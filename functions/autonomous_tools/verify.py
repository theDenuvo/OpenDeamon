#!/usr/bin/env python3
"""
Autonomous Tools — Verification Module
Runs tests, checks quality thresholds for generated tools.
"""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


def run_python_test(file_path: str, test_code: str, timeout: int = 60) -> dict:
    """Run a Python test snippet against the generated file."""
    # Create a test script that imports the module and runs tests
    test_script = f"""
import sys
sys.path.insert(0, r'{str(Path(file_path).parent)}')

{test_code}
"""

    try:
        result = subprocess.run(
            [sys.executable, "-c", test_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout
        )
        return {
            "passed": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"passed": False, "error": "test timeout"}
    except Exception as e:
        return {"passed": False, "error": str(e)}


def run_integration_test(file_path: str, test_case: dict) -> dict:
    """Run a single integration test case."""
    # Build test code from test_case
    input_data = test_case.get("input", {})
    expected = test_case.get("expect", None)
    func_name = test_case.get("function", "main")

    test_code = f"""
from {Path(file_path).stem} import {func_name}
import json

input_data = {json.dumps(input_data)}
expected = {json.dumps(expected) if expected is not None else "None"}

result = {func_name}(**input_data)
print("RESULT:", json.dumps(result, ensure_ascii=False))

if expected is not None:
    if result != expected:
        print("FAIL: expected", expected, "got", result)
        sys.exit(1)
    else:
        print("PASS")
else:
    print("NO EXPECTATION - manual verification needed")
"""

    return run_python_test(file_path, test_code)


def check_code_quality(file_path: str) -> dict:
    """Basic code quality checks."""
    path = Path(file_path)
    content = path.read_text(encoding="utf-8")

    issues = []
    score = 100

    # Size check
    lines = content.count("\n")
    if lines > 500:
        issues.append(f"File too long: {lines} lines")
        score -= 10

    # Hardcoded secrets check
    secret_patterns = [
        r"api_key\s*=\s*[\"'][^\"']+[\"']",
        r"password\s*=\s*[\"'][^\"']+[\"']",
        r"secret\s*=\s*[\"'][^\"']+[\"']",
        r"token\s*=\s*[\"'][^\"']+[\"']",
    ]
    import re
    for pattern in secret_patterns:
        if re.search(pattern, content, re.IGNORECASE):
            issues.append(f"Possible hardcoded secret: {pattern}")
            score -= 30

    # Error handling
    if "try:" not in content and "except" not in content:
        issues.append("No error handling (try/except) found")
        score -= 10

    # Timeout on HTTP
    if "requests.get" in content or "requests.post" in content:
        if "timeout" not in content:
            issues.append("HTTP requests without timeout")
            score -= 15

    # Docstring
    if not content.strip().startswith('"""') and not content.strip().startswith("'''"):
        issues.append("Missing module docstring")
        score -= 5

    return {
        "score": max(0, score),
        "issues": issues,
        "lines": lines
    }


def verify(tool_path: str, test_cases: list, quality_threshold: int = 70) -> dict:
    """
    Main verification function.
    Returns: {passed: bool, metrics: {}, details: [...]}
    """
    print(f"[verify] Verifying: {tool_path}")

    path = Path(tool_path)
    if not path.exists():
        return {"passed": False, "error": f"File not found: {tool_path}"}

    all_passed = True
    details = []

    # 1. Code quality
    quality = check_code_quality(str(path))
    details.append({"type": "quality", **quality})
    if quality["score"] < quality_threshold:
        all_passed = False
        print(f"[verify] Quality below threshold: {quality['score']} < {quality_threshold}")

    # 2. Syntax check
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
        details.append({"type": "syntax", "passed": True})
    except SyntaxError as e:
        details.append({"type": "syntax", "passed": False, "error": str(e)})
        all_passed = False

    # 3. Integration tests
    for i, tc in enumerate(test_cases):
        print(f"[verify] Running test {i+1}/{len(test_cases)}")
        result = run_integration_test(str(path), tc)
        details.append({"type": "integration_test", "index": i, **result})
        if not result.get("passed", False):
            all_passed = False
            print(f"[verify] Test {i+1} FAILED: {result.get('stderr', result.get('error'))}")

    # 4. Import test (can it be imported?)
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(path.stem, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        details.append({"type": "import", "passed": True})
    except Exception as e:
        details.append({"type": "import", "passed": False, "error": str(e)})
        all_passed = False

    metrics = {
        "quality_score": quality["score"],
        "tests_total": len(test_cases),
        "tests_passed": sum(1 for d in details if d.get("type") == "integration_test" and d.get("passed")),
        "issues_count": len(quality["issues"])
    }

    print(f"[verify] Result: {'PASS' if all_passed else 'FAIL'}")
    return {
        "passed": all_passed,
        "metrics": metrics,
        "details": details
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("tool_path")
    parser.add_argument("--threshold", type=int, default=70)
    parser.add_argument("--test", action="append", help="JSON test case")
    args = parser.parse_args()

    test_cases = []
    for t in args.test or []:
        test_cases.append(json.loads(t))

    result = verify(args.tool_path, test_cases, args.threshold)
    print(json.dumps(result, ensure_ascii=False, indent=2))