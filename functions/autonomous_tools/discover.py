#!/usr/bin/env python3
"""
Autonomous Tools — Discovery Module
Searches for existing free/keyless tools/APIs for a given capability.
Standalone: uses urllib (no hermes_tools needed).
Hermes-runtime: optional hermes_tools for better search.
"""

import json
import re
import sys
import urllib.request
import urllib.parse
from pathlib import Path
from typing import Any


def _web_search_fallback(query: str, limit: int = 5) -> list[dict]:
    """Fallback web search via DuckDuckGo HTML (no API key needed)."""
    results = []
    try:
        url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        title_pattern = re.compile(r'<a class="result__a"[^>]*>(.*?)</a>', re.DOTALL)
        titles = title_pattern.findall(html)
        for title in titles[:limit]:
            clean_title = re.sub(r'<[^>]+>', '', title).strip()
            if clean_title:
                results.append({
                    "source": "web",
                    "url": "",
                    "title": clean_title,
                    "description": ""
                })
    except Exception as e:
        print(f"[discover] DuckDuckGo fallback error: {e}")
    return results


# Optional: hermes_tools if running inside Hermes
try:
    from hermes_tools import web_search, web_extract, terminal
    _HERMES_AVAILABLE = True
except ImportError:
    _HERMES_AVAILABLE = False

# Local cached Skills Hub catalog (generated from installed skills)
SKILLS_HUB_CACHE = Path(__file__).parent / "skills_hub_cache.json"

def load_skills_hub_cache() -> dict:
    """Load cached skills catalog from local file."""
    try:
        if SKILLS_HUB_CACHE.exists():
            return json.loads(SKILLS_HUB_CACHE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[discover] Skills Hub cache load error: {e}")
    return {"skills": []}

def search_skills_hub(query: str, limit: int = 5) -> list[dict]:
    """Search local Skills Hub cache first - curated, verified installed skills."""
    results = []
    catalog = load_skills_hub_cache()
    
    # Split query into individual words for flexible matching
    query_words = [w.lower() for w in query.split() if len(w) > 3]
    
    for skill in catalog.get("skills", []):
        name = skill.get("name", "").lower()
        desc = skill.get("description", "").lower()
        tags = " ".join(skill.get("tags", [])).lower()
        
        # Count matches in title and tags (more relevant than description)
        title_tags = skill.get("name", "").lower() + " " + " ".join(skill.get("tags", []))
        title_matches = sum(1 for w in query_words if w in title_tags)
        desc_matches = sum(1 for w in query_words if w in desc)
        
        # Require at least 2 title/tag matches OR 1 strong title match
        matched = title_matches >= 2 or (title_matches >= 1 and len(query_words) <= 3)
        if not matched and desc_matches >= 3:
            matched = True  # fallback: many description matches
        
        if matched:
            results.append({
                "source": "skills_hub",
                "url": f"file://{skill.get('path', '')}",
                "title": skill.get("name", ""),
                "description": skill.get("description", ""),
                "tags": skill.get("tags", []),
                "install_cmd": skill.get("install", f"hermes skill install {skill.get('name', '')}"),
                "category": skill.get("category", ""),
                "author": skill.get("author", ""),
                "platforms": skill.get("platforms", [])
            })
            if len(results) >= limit:
                break
    return results


def search_github(query: str, limit: int = 5) -> list[dict]:
    """Search GitHub for relevant repos."""
    if _HERMES_AVAILABLE:
        results = web_search(f"{query} site:github.com", limit=limit)
        return [r for r in results.get("data", {}).get("web", []) if "github.com" in r.get("url", "")]
    return _web_search_fallback(f"{query} site:github.com", limit)


def search_pypi(query: str, limit: int = 5) -> list[dict]:
    """Search PyPI for packages via PyPI JSON API (no key needed)."""
    results = []
    try:
        url = f"https://pypi.org/search/?q={urllib.parse.quote(query)}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            html = resp.read().decode("utf-8", errors="replace")
        pattern = re.compile(r'class="package-snippet"[^>]*>.*?href="/project/([^/]+)/')
        matches = pattern.findall(html)
        for m in matches[:limit]:
            results.append({
                "source": "pypi",
                "url": f"https://pypi.org/project/{m}/",
                "title": m,
                "description": ""
            })
    except Exception as e:
        print(f"[discover] PyPI search error: {e}")
    return results


def search_npm(query: str, limit: int = 5) -> list[dict]:
    """Search npm via npm search (basic)."""
    if _HERMES_AVAILABLE:
        results = web_search(f"{query} site:npmjs.com", limit=limit)
        return [r for r in results.get("data", {}).get("web", []) if "npmjs.com" in r.get("url", "")]
    return _web_search_fallback(f"{query} site:npmjs.com", limit)


def search_huggingface(query: str, limit: int = 5) -> list[dict]:
    """Search HuggingFace via HF API (no key for basic search)."""
    results = []
    try:
        url = f"https://huggingface.co/api/models?search={urllib.parse.quote(query)}&limit={limit}"
        req = urllib.request.Request(url, headers={"User-Agent": "OpenDeamon/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        for item in data:
            results.append({
                "source": "huggingface",
                "url": item.get("url", f"https://huggingface.co/{item.get('id', '')}"),
                "title": item.get("id", ""),
                "description": item.get("tags", [])[:3]
            })
    except Exception as e:
        print(f"[discover] HF search error: {e}")
    return results


def search_general(query: str, limit: int = 5) -> list[dict]:
    """General web search for APIs, services, keyless endpoints."""
    if _HERMES_AVAILABLE:
        results = web_search(f"{query} free API keyless OR open source", limit=limit)
        return results.get("data", {}).get("web", [])
    return _web_search_fallback(f"{query} free API keyless", limit)


def evaluate_candidate(candidate: dict, requirements: dict) -> dict:
    """Score candidate against requirements."""
    score = 0
    reasons = []

    # Skills Hub candidates get base bonus (curated, verified)
    if candidate.get("source") == "skills_hub":
        score += 3
        reasons.append("skills_hub: curated, verified")

    # Default source if missing
    if "source" not in candidate:
        candidate["source"] = "web"

    text = (candidate.get("title", "") + " " + candidate.get("description", "")).lower()
    free_keywords = ["free", "keyless", "no api key", "open source", "mit", "apache", "bsd"]
    paid_keywords = ["paid", "subscription", "credit card", "billing", "pricing", "tier"]

    if any(k in text for k in free_keywords):
        score += 3
        reasons.append("free/keyless indicators found")
    if any(k in text for k in paid_keywords):
        score -= 2
        reasons.append("paid indicators found")

    if "mit" in text or "apache" in text or "bsd" in text:
        score += 2
        reasons.append("permissive license")

    if "api" in text or "cli" in text or "python" in text:
        score += 1
        reasons.append("programmatic access")

    for req in requirements.get("must_have", []):
        if req.lower() in text:
            score += 2
            reasons.append(f"matches requirement: {req}")

    # Relevance bonus: query words matching name/tags (Skills Hub)
    if candidate.get("source") == "skills_hub":
        query_words = requirements.get("description", "").lower().split()
        name_tags = candidate.get("title", "").lower() + " " + " ".join(candidate.get("tags", []))
        relevance_matches = sum(1 for w in query_words if len(w) > 3 and w in name_tags)
        if relevance_matches > 0:
            bonus = min(relevance_matches * 2, 6)  # cap at 6
            score += bonus
            reasons.append(f"relevance: {relevance_matches} keyword matches")

    return {
        **candidate,
        "score": score,
        "eval_reasons": reasons
    }


def discover(tool_name: str, requirements: dict) -> dict:
    """
    Main discovery function.
    requirements: {
        "description": "what the tool should do",
        "must_have": ["list", "of", "required", "features"],
        "nice_to_have": ["optional", "features"],
        "constraints": ["free", "keyless", "open_source"]
    }
    """
    print(f"[discover] Searching for: {tool_name}")
    print(f"[discover] Requirements: {json.dumps(requirements, ensure_ascii=False)}")

    search_log = []
    all_candidates = []

    search_str = f"{tool_name} {requirements.get('description', '')}"

    for source, func in [
        ("skills_hub", search_skills_hub),
        ("github", search_github),
        ("pypi", search_pypi),
        ("npm", search_npm),
        ("huggingface", search_huggingface),
        ("web", search_general)
    ]:
        query = f"{tool_name} {requirements.get('description', '')}"
        candidates = func(query)
        search_log.append({"query": query, "source": source, "count": len(candidates)})
        all_candidates.extend(candidates)

    # Deduplicate by URL
    seen = set()
    unique = []
    for c in all_candidates:
        url = c.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(c)

    evaluated = [evaluate_candidate(c, requirements) for c in unique]
    evaluated.sort(key=lambda x: x["score"], reverse=True)

    best = evaluated[0] if evaluated else None
    found = best is not None and best["score"] >= 3

    return {
        "found": found,
        "tool_name": tool_name,
        "candidates": evaluated[:10],
        "best": best,
        "search_log": search_log
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("tool_name")
    parser.add_argument("--desc", default="")
    parser.add_argument("--must", nargs="*", default=[])
    args = parser.parse_args()

    req = {"description": args.desc, "must_have": args.must}
    result = discover(args.tool_name, req)
    print(json.dumps(result, ensure_ascii=False, indent=2))