#!/usr/bin/env python
"""skill_finder — find an existing skill instead of reinventing the capability.

Why this exists
---------------
The core repeatedly invents capabilities that already exist in the Skills Hub
(101k+ skills). The image-generation incident is the reference: the core
went to a web API while a local GPU and an official stable-diffusion skill
were both available, and shipped a 2-byte "image".

The fix is discovery that costs zero context: the hub index is a local JSON
cache, so searching it is an in-process lookup — no network, no tool call
into the model's context window, no prompt budget spent.

Priority (project decision 2026-09-29):
    local installed  ->  Hermes Skill Hub  ->  GitHub  ->  web  ->  build
with ONE hard exception: if this machine can do the task locally at higher
quality (GPU, CPU, installed model), local wins over any web service. That
exception is the whole point of the image-generation failure.

Stdlib only. Safe to call from a hook or the core.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HUB_INDEX = HOME / "skills" / ".hub" / "index-cache" / "hermes-index.json"
FUNCTIONS = Path(os.environ.get("OPENDEAMON_FUNCTIONS", r"A:\OpenDeamon\functions"))

# Categories where a local GPU/CPU is materially better than a web API.
# Checked against local capability before any remote source is offered.
LOCAL_BEATS_WEB = {
    "image", "image-generation", "text-to-image", "img2img", "upscale",
    "video", "diffusion", "photo", "render", "3d", "speech", "tts",
    "transcribe", "embedding", "ocr",
}

_STOP = {
    "the", "a", "an", "for", "to", "of", "and", "or", "in", "on", "with",
    "make", "create", "generate", "build", "tool", "using", "use", "get",
    "from", "into", "via", "that", "this", "it", "is", "are", "be", "my",
    "me", "i", "do", "does", "how", "what", "which", "when", "where",
}


def _norm(tok: str) -> str:
    """Crude stemming so images/image and diffusion/diffuse compare equal.

    Learned live 2026-09-29: without this, the canonical answer for
    "generate images from text" (official/mlops/stable-diffusion, described as
    "Text-to-image generation") scored 0.0 and was dropped — the tool failed
    exactly on the case it was built for.
    """
    if len(tok) > 4:
        if tok.endswith("ies"):
            return tok[:-3] + "y"
        if tok.endswith("sses") or tok.endswith("shes") or tok.endswith("ches"):
            return tok[:-2]
        if tok.endswith("s") and not tok.endswith("ss") and not tok.endswith("us"):
            return tok[:-1]
    return tok


def _tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", str(text)).lower()
    text = re.sub(r"[^a-z0-9\s.+#]", " ", text)
    raw = [t for t in text.split() if t]
    out: list[str] = []
    for t in raw:
        if "-" in t or "/" in t:
            out += [p for p in re.split(r"[-/]", t) if p]
        else:
            out.append(t)
    return [_norm(t) for t in out if t and t not in _STOP and len(t) > 1]


def _haystack(name: str, desc: str) -> tuple[str, str]:
    """Normalised name/description for comparison (same stemming as the query).

    The name MUST go through _tokens too, not just _norm: otherwise
    "stable-diffusion" stays one token, "image" never matches it, and the
    official skill for "generate images from text" scores 0.
    """
    name_l = " ".join(_tokens(name))
    desc_l = " ".join(_tokens(desc))
    return name_l, desc_l


def _score(query_tokens: list[str], name: str, desc: str) -> float:
    """Weighted token match.

    Name hits outweigh description hits, but a description-only match scores
    low (0.25/token): a skill that merely mentions the word is not a solution
    for the task. Without that floor the search returned unrelated skills that
    happened to contain the word ("document", "create") ranked above real ones.
    """
    if not query_tokens:
        return 0.0
    name_l, desc_l = _haystack(name, desc)
    score = 0.0
    name_hits = 0
    desc_hits = 0
    for tok in query_tokens:
        if tok in name_l:
            name_hits += 1
            score += 3.0 if len(tok) > 3 else 1.5
        if tok in desc_l:
            desc_hits += 1
            score += 1.0 if len(tok) > 3 else 0.5
    joined = " ".join(query_tokens)
    if joined and joined in name_l:
        score += 4.0
    # Every query token hit somewhere (name or description) counts as a real
    # match. A name-only floor was too strict: it dropped skills whose name
    # differs but whose description is exactly the capability asked for.
    if name_hits == 0 and desc_hits < max(1, len(query_tokens) // 2):
        return 0.0
    return score


def local_capability() -> dict:
    """What this machine can do without any network service."""
    caps = {"gpu": None, "gpu_name": None, "vram_mb": 0, "functions": []}
    try:
        import subprocess
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode == 0 and out.stdout.strip():
            first = out.stdout.strip().splitlines()[0]
            name, _, mem = first.partition(",")
            caps["gpu"] = True
            caps["gpu_name"] = name.strip()
            digits = re.sub(r"[^\d]", "", mem or "")
            caps["vram_mb"] = int(digits) if digits else 0
    except Exception:
        caps["gpu"] = None
    try:
        if FUNCTIONS.is_dir():
            caps["functions"] = sorted(
                p.name for p in FUNCTIONS.iterdir()
                if p.is_dir() and (p / "SKILL.md").is_file()
            )
    except OSError:
        pass
    return caps


def search_local(query: str, limit: int = 5) -> list[dict]:
    """Installed skills + our own function packs (tier 1 of the priority)."""
    toks = _tokens(query)
    hits: list[dict] = []
    roots = [(HOME / "skills", "builtin/hub"),
              (FUNCTIONS, "local-function-pack")]
    for root, origin in roots:
        if not root.is_dir():
            continue
        for skill_md in root.rglob("SKILL.md"):
            try:
                head = skill_md.read_text(encoding="utf-8", errors="replace")[:1200]
            except OSError:
                continue
            m = re.search(r"^name:\s*(.+)$", head, re.M)
            d = re.search(r"^description:\s*(.+)$", head, re.M)
            name = (m.group(1).strip() if m else skill_md.parent.name)
            desc = (d.group(1).strip().strip('"\'') if d else "")
            s = _score(toks, name, desc)
            if s > 0:
                hits.append({"name": name, "description": desc[:160],
                             "origin": origin, "score": round(s, 1),
                             "path": str(skill_md)})
    hits.sort(key=lambda h: -h["score"])
    return hits[:limit]


def search_hub(query: str, limit: int = 8) -> list[dict]:
    """Query the local hub index cache (offline, instant)."""
    if not HUB_INDEX.is_file():
        return []
    toks = _tokens(query)
    if not toks:
        return []
    try:
        data = json.loads(HUB_INDEX.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return []
    skills = data.get("skills") or []
    out = []
    for sk in skills:
        if not isinstance(sk, dict):
            continue
        # Hub records are inconsistent. Observed 2026-09-29: clawhub rows put
        # a quoted multi-line DESCRIPTION in "name" and a clean slug in
        # "identifier". Prefer the identifier whenever the name looks like prose.
        name = sk.get("name") or ""
        ident = sk.get("identifier") or ""
        slug = ident.rsplit("/", 1)[-1] if ident else ""
        looks_like_prose = (
            len(name) > 48 or '"' in name or "\n" in name or len(name.split()) > 7
        )
        if looks_like_prose and slug:
            name = slug
        if not name:
            name = slug or "(unnamed)"
        s = _score(toks, name, sk.get("description", ""))
        if s > 0:
            out.append({"name": name,
                        "description": (sk.get("description") or "")[:160],
                        "identifier": sk.get("identifier"),
                        "source": sk.get("source"),
                        "trust": sk.get("trust_level"),
                        "score": round(s, 1)})
    out.sort(key=lambda h: -h["score"])
    return out[:limit]


def is_local_preferred(query: str) -> dict | None:
    """Return a local-first warning when the task is a creative/GPU class task
    AND this machine has the hardware. This is the rule the core broke."""
    caps = local_capability()
    toks = set(_tokens(query))
    creative = bool(toks & LOCAL_BEATS_WEB) or any(
        w in query.lower() for w in ("image", "photo", "picture", "video", "diffusion"))
    if creative and caps.get("gpu"):
        return {"prefer": "local",
                "reason": "creative task + local %s (%s MiB VRAM)" % (
                    caps.get("gpu_name"), caps.get("vram_mb")),
                "caps": caps}
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Find an existing skill for a capability.")
    ap.add_argument("query", help='capability, e.g. "generate images from text"')
    ap.add_argument("--limit", type=int, default=8)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--hub-only", action="store_true")
    args = ap.parse_args()

    result: dict = {"query": args.query}
    warning = is_local_preferred(args.query)
    if warning:
        result["local_first"] = warning
    if not args.hub_only:
        result["local"] = search_local(args.query, args.limit)
    result["hub"] = search_hub(args.query, args.limit)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    print("query: %s" % args.query)
    if "local_first" in result:
        w = result["local_first"]
        print("\n!! PREFER LOCAL: %s" % w["reason"])
        print("   Web APIs are markedly lower quality for this class of task.")
        print("   Check local capability first (functions/, installed models, GPU).")
    if result.get("local"):
        print("\n[1] installed / local function packs")
        for h in result["local"]:
            print("   %-34s %s" % (h["name"], h["description"][:90]))
    else:
        print("\n[1] installed / local function packs: nothing matching")
    print("\n[2] skills hub (offline index)")
    for h in result["hub"]:
        print("   %-34s [%s] %s" % (h["name"], h.get("source"),
                                     h["description"][:80]))
    print("\nnext: hermes skills inspect <identifier>  ->  "
          "hermes skills install <identifier>")
    print("only if nothing above fits, consider building it via the opencode worker.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
