#!/usr/bin/env python3
"""
Autonomous Tools — Main Orchestrator
Entry point for the core to discover/create/verify/register tools.
"""

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from functions.autonomous_tools.discover import discover
from functions.autonomous_tools.create import create_tool
from functions.autonomous_tools.verify import verify
from functions.autonomous_tools.register import register_tool, rollback_registration


def autonomous_tool(
    capability: str,
    requirements: dict,
    test_cases: list,
    quality_threshold: int = 70,
    force_create: bool = False
) -> dict:
    """
    Main entry point for the core.

    capability: short name (e.g., "image_generation", "video_download", "pdf_ocr")
    requirements: {
        "description": "detailed description",
        "must_have": ["feature1", "feature2"],
        "constraints": ["free", "keyless", "open_source"]
    }
    test_cases: list of {"input": {...}, "expect": ..., "function": "name"}
    quality_threshold: minimum quality score (0-100)
    force_create: skip discovery, go straight to creation
    """
    print(f"\n{'='*60}")
    print(f"AUTONOMOUS TOOLS: {capability}")
    print(f"{'='*60}")

    tool_name = capability.replace(" ", "-").replace("_", "-").lower()

    # Phase 1: Discovery (unless forced)
    if not force_create:
        print("\n[1/4] DISCOVERY")
        discovery_result = discover(tool_name, requirements)

        if discovery_result["found"]:
            best = discovery_result["best"]
            print(f"[discover] Found existing solution: {best['title']} (score: {best['score']})")
            print(f"[discover] URL: {best['url']}")

            # TODO: Implement wrapper generation for existing solution
            # For now, return the found candidate for core to decide
            return {
                "status": "found_existing",
                "capability": capability,
                "candidate": best,
                "all_candidates": discovery_result["candidates"][:5],
                "search_log": discovery_result["search_log"],
                "message": "Existing free/keyless solution found. Core can wrap it or proceed to creation."
            }

        print("[discover] No suitable existing solution found. Proceeding to creation...")

    # Phase 2: Creation
    print("\n[2/4] CREATION")
    output_dir = f"A:/OpenDeamon/functions/{tool_name}"
    spec = {
        "name": tool_name,
        "description": requirements.get("description", ""),
        "interface": requirements.get("interface", {}),
        "requirements": requirements,
        "test_cases": test_cases
    }

    create_result = create_tool(spec, output_dir)

    if not create_result["success"]:
        return {
            "status": "creation_failed",
            "capability": capability,
            "error": create_result.get("error"),
            "details": create_result
        }

    tool_path = create_result["path"]
    print(f"[create] Generated: {tool_path}")

    # Phase 3: Verification
    print("\n[3/4] VERIFICATION")
    verify_result = verify(tool_path, test_cases, quality_threshold)

    if not verify_result["passed"]:
        print(f"[verify] FAILED - rolling back")
        rollback_registration(tool_name)
        return {
            "status": "verification_failed",
            "capability": capability,
            "tool_path": tool_path,
            "metrics": verify_result["metrics"],
            "details": verify_result["details"]
        }

    print(f"[verify] PASSED - quality: {verify_result['metrics']['quality_score']}")

    # Phase 4: Registration
    print("\n[4/4] REGISTRATION")
    interface = requirements.get("interface", {"function": "main", "description": requirements.get("description", "")})
    register_result = register_tool(
        tool_path=tool_path,
        tool_name=tool_name,
        description=requirements.get("description", ""),
        interface=interface,
        source="autonomous-tools"
    )

    return {
        "status": "success",
        "capability": capability,
        "tool_name": tool_name,
        "skill_dir": register_result["skill_dir"],
        "config_update": register_result["config_update"],
        "verification_metrics": verify_result["metrics"]
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Autonomous Tools Orchestrator")
    parser.add_argument("capability")
    parser.add_argument("--desc", default="")
    parser.add_argument("--must", nargs="*", default=[])
    parser.add_argument("--interface", default="{}")
    parser.add_argument("--tests", default="[]")
    parser.add_argument("--threshold", type=int, default=70)
    parser.add_argument("--force-create", action="store_true")
    args = parser.parse_args()

    requirements = {
        "description": args.desc,
        "must_have": args.must,
        "interface": json.loads(args.interface),
        "constraints": ["free", "keyless", "open_source"]
    }
    test_cases = json.loads(args.tests)

    result = autonomous_tool(
        capability=args.capability,
        requirements=requirements,
        test_cases=test_cases,
        quality_threshold=args.threshold,
        force_create=args.force_create
    )

    print(f"\n{'='*60}")
    print("FINAL RESULT:")
    print(json.dumps(result, ensure_ascii=False, indent=2))