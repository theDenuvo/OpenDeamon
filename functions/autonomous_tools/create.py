#!/usr/bin/env python3
"""
Autonomous Tools — Creation Module
Generates a new tool via opencode worker when no existing solution found.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

try:
    from hermes_tools import terminal
except ImportError:
    terminal = None

# Resolve opencode path - prefer .cmd to avoid Win32 errors
OPENCODE_PATH = None
try:
    result = subprocess.run(
        ["where", "opencode.cmd"],
        capture_output=True, text=True, timeout=5
    )
    if result.returncode == 0:
        OPENCODE_PATH = result.stdout.strip().split("\n")[0]
except Exception:
    pass

if not OPENCODE_PATH:
    try:
        result = subprocess.run(
            ["where", "opencode"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            OPENCODE_PATH = result.stdout.strip().split("\n")[0]
    except Exception:
        pass

if not OPENCODE_PATH:
    OPENCODE_PATH = "opencode"  # fallback


def build_opencode_prompt(spec: dict) -> str:
    """Build a detailed prompt for opencode worker."""
    name = spec.get("name", "tool")
    description = spec.get("description", "")
    interface = spec.get("interface", {})
    requirements = spec.get("requirements", {})
    test_cases = spec.get("test_cases", [])

    iface_desc = ""
    if interface:
        iface_desc = f"""
Interface (MUST implement exactly):
{json.dumps(interface, ensure_ascii=False, indent=2)}
"""

    req_desc = ""
    if requirements:
        req_desc = f"""
Requirements:
{json.dumps(requirements, ensure_ascii=False, indent=2)}
"""

    test_desc = ""
    if test_cases:
        test_desc = f"""
Test cases (MUST pass):
{json.dumps(test_cases, ensure_ascii=False, indent=2)}
"""

    prompt = f"""Create a Python tool/module: {name}

Description: {description}
{iface_desc}
{req_desc}
{test_desc}

Constraints:
- Single Python file (or minimal package)
- No external dependencies beyond stdlib + requests (if HTTP needed)
- All configuration via environment variables (no hardcoded keys)
- Free/keyless only — no paid APIs
- Must run on Windows (Python 3.11+)
- Output: ONLY the implementation file(s), no explanations

Structure:
- Main function/class matching the interface
- If HTTP: use requests with timeout, proper error handling
- If CLI wrapper: use subprocess with proper escaping
- Include __main__ block for quick manual test
- Follow the existing autonomous-tools patterns

Acceptance: All test cases must pass when run.
"""
    return prompt


def run_opencode(prompt: str, workdir: str, model: str = "opencode/nemotron-3.5-lightning-free") -> dict:
    """Run opencode CLI with the given prompt."""
    prompt_file = Path(workdir) / "opencode_prompt.txt"
    prompt_file.write_text(prompt, encoding="utf-8")

    # Build env with npm in PATH
    env = os.environ.copy()
    npm_paths = [
        r"C:\Users\cheli\AppData\Roaming\npm",
        r"C:\Program Files\nodejs",
    ]
    current_path = env.get("PATH", "")
    for npm_path in npm_paths:
        if npm_path not in current_path:
            current_path = npm_path + ";" + current_path
    env["PATH"] = current_path

    cmd = [
        OPENCODE_PATH, "run",
        "-m", model,
        str(prompt_file)
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=workdir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            env=env
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "opencode timeout (300s)"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def extract_generated_files(workdir: str, expected_name: str) -> list[Path]:
    """Find files created by opencode in workdir."""
    workdir_path = Path(workdir)
    py_files = list(workdir_path.glob("*.py"))
    py_files = [f for f in py_files if f.name != "opencode_prompt.txt"]
    return py_files


def create_tool(spec: dict, output_dir: str) -> dict:
    """
    Main creation function.
    spec: {
        "name": "tool_name",
        "description": "what it does",
        "interface": {"function": "generate_image", "params": {"prompt": "str"}, "returns": "url"},
        "requirements": {"free": true, "keyless": true, "no_keys": true},
        "test_cases": [{"input": {"prompt": "cat"}, "expect": "url_or_bytes"}]
    }
    output_dir: where to write the generated tool (e.g., A:/OpenDeamon/functions/pollinations-image/)
    """
    name = spec.get("name", "unnamed_tool")
    print(f"[create] Generating tool: {name}")

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    prompt = build_opencode_prompt(spec)
    result = run_opencode(prompt, str(output_path))

    if not result["success"]:
        return {
            "success": False,
            "error": f"opencode failed: {result.get('stderr', result.get('error', 'unknown'))}",
            "details": result
        }

    generated = extract_generated_files(str(output_path), name)

    if not generated:
        return {
            "success": False,
            "error": "No files generated by opencode",
            "details": result
        }

    main_file = generated[0]

    return {
        "success": True,
        "path": str(main_file),
        "generated_files": [str(f) for f in generated],
        "opencode_output": result["stdout"][:2000]
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--desc", default="")
    parser.add_argument("--out", default="A:/OpenDeamon/functions/test-tool")
    args = parser.parse_args()

    spec = {
        "name": args.name,
        "description": args.desc,
        "interface": {},
        "requirements": {"free": True, "keyless": True},
        "test_cases": []
    }
    result = create_tool(spec, args.out)
    print(json.dumps(result, ensure_ascii=False, indent=2))