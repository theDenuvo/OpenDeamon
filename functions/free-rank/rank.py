"""free-rank: order the free pool by rating, rewrite configs.

Signals: live OpenRouter :free catalog + models.dev capability flags
(Hermes cache) + own micro-probes + Aider leaderboard snapshot.
Usage: rank.py [--weekly] [--apply]
  --weekly : run live probes (~15 free calls) before scoring
  --apply  : rewrite hermes config + gateway config (else dry-run print)
Key handling: OR key read in-process from pre-existing secrets file.
"""
import base64
import copy
import datetime
import json
import re
import sys
import urllib.request

import yaml

HERE = r"A:\OpenDeamon\functions\free-rank"
HERMES_CFG = r"A:\OpenDeamon\hermes-home\config.yaml"
GW_CFG = r"A:\OpenDeamon\gateway\litellm-config.yaml"
CACHE = r"A:\hermes\models_dev_cache.json"
SECRETS = r"A:\AI\daemon\config\secrets.local.toml"
RANKING = HERE + r"\ranking.json"
AIDER_URL = "https://aider.chat/docs/leaderboards/"

PROBE_REASON = "What is 17*23? Reply with just the number, nothing else."
PROBE_FORMAT = 'Reply with exactly this and nothing else: {"a": 1}'
PROBE_VISION = ("Look at the attached chart image. Reply with exactly: "
                "CHART-SEEN")
CHART = r"A:\AI\tests\images\04_chart_ru.png"

# Empirically dead on the real vision path (stress test 2026-09-29):
# models.dev claims vision support, OpenRouter answers 404 to images.
VISION_DENY = {
    # Both verified live on 2026-09-29 (STRESS_TEST_REPORT.md, vision table):
    # HTTP 404 "No endpoints support image input" on a real image request.
    # models.dev still advertises vision: true for both, which is why the
    # catalogue flag cannot be trusted here.
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    "google/gemma-4-31b-it:free",
}


def vision_usable(e):
    """May this model hold a vision role?

    models.dev advertising image input is not enough: VISION_DENY holds models
    that 404 on real images. This is a pure predicate, deliberately not a flag
    stashed on the entry. The scoring functions are called from sort keys, from
    the >= 0 filter and from the hysteresis check, and the first of those to run
    used to decide eligibility by side effect: if score_reason returned -1
    before reaching eff(), the deny list was never applied and the model scored
    as if it were healthy.
    """
    return bool(e.get("vision")) and e["id"] not in VISION_DENY


def or_key():
    text = open(SECRETS, encoding="utf-8").read()
    return re.search(r"api_key\s*=\s*[\"']([^\"']+)", text).group(1)


def or_chat(model, messages, max_tokens=30, timeout=120):
    body = json.dumps({"model": model, "messages": messages,
                       "max_tokens": max_tokens}).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + or_key()})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def live_free():
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/models",
        headers={"Authorization": "Bearer " + or_key()})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    return sorted(x["id"] for x in data["data"]
                  if str(x["id"]).endswith(":free"))


def caps():
    d = json.load(open(CACHE, encoding="utf-8"))
    return d.get("openrouter", {}).get("models", {})


def aider_bonus():
    """Names of open/free-ish models present on Aider leaderboard."""
    try:
        req = urllib.request.Request(AIDER_URL, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        html = urllib.request.urlopen(req, timeout=60).read().decode(
            "utf-8", "replace")
        names = set(re.findall(r"<td[^>]*>([^<>]{3,80})</td>", html))
        low = " ".join(names).lower()
        with open(HERE + r"\aider_snapshot.md", "w",
                  encoding="utf-8") as f:
            f.write("# Aider snapshot %s\n\n%s\n" % (
                datetime.date.today().isoformat(),
                "\n".join(sorted(names)[:200])))
        return low
    except Exception as e:
        print("AIDER-FETCH-FAILED:", e)
        return ""


def fam(model_id):
    return model_id.split(":free")[0].lower()


def classify(exc):
    s = str(exc)
    if re.search(r"40[134]", s):
        return "dead"       # gone / forbidden for this account: exclude
    return "congested"      # 429/5xx/upstream: transient, don't punish


def probe(model_id, vision_ok):
    """(score 0..3, status ok|congested|dead)."""
    score = 0
    try:
        r = or_chat(model_id, [{"role": "user", "content": PROBE_REASON}])
        if "391" in (r["choices"][0]["message"]["content"] or ""):
            score += 1
    except Exception as e:
        st = classify(e)
        print("PROBE", model_id, "reason FAIL", st, str(e)[:80])
        return 0, st
    status = "ok"
    try:
        r = or_chat(model_id, [{"role": "user", "content": PROBE_FORMAT}])
        txt = (r["choices"][0]["message"]["content"] or "").strip()
        if txt == '{"a": 1}':
            score += 1
    except Exception as e:
        st = classify(e)
        print("PROBE", model_id, "format FAIL", st, str(e)[:80])
        status = st if st != "ok" else status
    if vision_ok:
        try:
            img = base64.b64encode(open(CHART, "rb").read()).decode()
            r = or_chat(model_id, [{"role": "user", "content": [
                {"type": "text", "text": PROBE_VISION},
                {"type": "image_url",
                 "image_url": {"url": "data:image/png;base64," + img}}]}],
                max_tokens=20)
            if "CHART-SEEN" in (r["choices"][0]["message"]["content"] or ""):
                score += 1
        except Exception as e:
            st = classify(e)
            print("PROBE", model_id, "vision FAIL", st, str(e)[:80])
            status = st if st != "ok" else status
    else:
        score += 1  # non-vision models aren't penalized on vision probe
    return score, status


def main():
    weekly = "--weekly" in sys.argv
    apply = "--apply" in sys.argv
    free = live_free()
    print("LIVE-FREE:", len(free))
    cap = caps()
    aider_low = aider_bonus()

    table = []
    for mid in free:
        c = cap.get(mid, {})
        mods = ((c.get("modalities") or {}).get("input") or [])
        ctx = (c.get("limit") or {}).get("context") or 0
        entry = {
            "id": mid,
            "tool": bool(c.get("tool_call")),
            "reason": bool(c.get("reasoning")),
            "ctx": ctx,
            "vision": ("image" in mods),
            "probe": None,
            "aider": any(k in aider_low for k in
                         fam(mid).replace("/", " ").split()[:2]),
        }
        entry["vision_usable"] = vision_usable(entry)
        table.append(entry)

    if weekly:
        for e in table:
            e["probe"], e["status"] = probe(e["id"], e["vision"])
            print("PROBE", e["id"], "=", e["probe"], e["status"])
    else:
        try:
            prev = {t["id"]: t for t in
                    json.load(open(RANKING, encoding="utf-8"))["table"]}
            for e in table:
                if e["id"] in prev:
                    e["probe"] = prev[e["id"]].get("probe")
                    e["status"] = prev[e["id"]].get("status", "ok")
        except Exception as ex:
            print("NO-PREV-RANKING:", ex)

    def eff(e, congested_baseline=False):
        """Effective probe score, or -1 when the model must not be used.

        ``congested_baseline`` decides what a rate-limited model scores.
        Default False keeps the historical behaviour (probe as-is, so a
        congested model ranks last) for the MAIN model chain - free-tier
        congestion is real and TODO phase 2.3 requires measuring success rate
        before changing which model serves the main turn.

        Vision passes True: a 404-on-images model must be excluded by
        VISION_DENY rather than by its probe score, and a vision role should
        not be handed to a model that merely rate-limited during a probe when
        a proven one exists.
        """
        status = e.get("status") or "ok"
        if status == "dead":
            return -1
        if congested_baseline and (status == "congested" or e.get("probe") is None):
            return 2
        return e["probe"]

    prev_win = {}
    try:
        live = yaml.safe_load(open(HERMES_CFG, encoding="utf-8"))
        prev_win = {
            "reasoning": (live.get("model") or {}).get("default"),
            "vision": ((live.get("auxiliary") or {}).get("vision") or {})
            .get("model"),
            "cheap": (live.get("delegation") or {}).get("model"),
        }
    except Exception as ex:
        print("NO-LIVE-CFG:", ex)

    def score_reason(e):
        if not (e["tool"] and e["reason"]):
            return -1
        p = eff(e)
        if p < 0:
            return -1
        s = (e["ctx"] >= 100000) + (e["ctx"] >= 262144)
        s += p
        s += 1 if e["aider"] else 0
        return s

    def score_vision(e):
        if not vision_usable(e):
            return -1
        p = eff(e, congested_baseline=True)
        if p < 0:
            return -1
        return 1 + p + (1 if e["aider"] else 0)

    def score_cheap(e):
        if not e["tool"]:
            return -1
        p = eff(e)
        if p < 0:
            return -1
        return p + (1 if e["ctx"] and e["ctx"] <= 262144 else 0)

    winners = {}
    by_id = {e["id"]: e for e in table}
    for role, fn in (("reasoning", score_reason), ("vision", score_vision),
                     ("cheap", score_cheap)):
        ranked = sorted(table, key=lambda e: (fn(e), e["ctx"] or 0),
                        reverse=True)
        ranked = [e for e in ranked if fn(e) >= 0]
        best = ranked[0] if ranked else None
        # Hysteresis: keep incumbent unless it died or a verified-perfect
        # challenger beats a non-healthy incumbent. Kills config thrash
        # from momentary 429 luck.
        inc = by_id.get(prev_win.get(role, "")) if prev_win else None
        pick = None
        if best is None:
            pick = None
        elif inc is None or (inc.get("status") or "ok") == "dead" \
                or fn(inc) < 0:
            # dead incumbent: prefer live-verified replacements first
            verified = [e for e in ranked
                        if (e.get("probe"), e.get("status")) == (3, "ok")]
            pick = verified[0] if verified else best
        elif best["id"] == inc["id"]:
            pick = inc
        elif (best.get("probe"), best.get("status")) == (3, "ok") \
                and (inc.get("status") or "ok") != "ok":
            pick = best
        else:
            pick = inc
        winners[role] = pick["id"] if pick else None
        print("WINNER", role, "=", winners[role],
              "(incumbent %s)" % ((inc or {}).get("id")))
    order = [e["id"] for e in sorted(
        table, key=lambda e: (score_reason(e), e["ctx"] or 0), reverse=True)
        if score_reason(e) >= 0]
    print("ORDER:", order)
    # Vision fallbacks must be ranked among vision-capable models. Taking them
    # from the reasoning order hands the vision role text-only models that
    # 400 on an image, which is worse than having no fallback at all.
    vision_order = [e["id"] for e in sorted(
        (e for e in table if vision_usable(e)),
        key=lambda e: (score_vision(e), e["ctx"] or 0), reverse=True)
        if score_vision(e) >= 0]
    print("VISION-ORDER:", vision_order)

    ranking = {"ts": datetime.datetime.now().isoformat(timespec="seconds"),
               # `winners` is the ranker's opinion for THIS run.
               "winners": winners,
               # `applied` is what config.yaml actually carries right now.
               # These two differ BY DESIGN and routinely: hysteresis keeps a
               # proven model in place, and the owner hand-edits the vision
               # chain to the one model that passed a live image test. Before
               # this split the file was read as "this is the configuration",
               # which is exactly how a broken vision winner looked correct.
               "applied": {k: v for k, v in prev_win.items() if v},
               "order": order,
               "table": [{k: v for k, v in e.items()} for e in table]}
    if not apply:
        print("DRY-RUN (add --apply to rewrite configs)")
        return
    with open(RANKING, "w", encoding="utf-8") as f:
        json.dump(ranking, f, indent=1)
    cfg = yaml.safe_load(open(HERMES_CFG, encoding="utf-8"))
    cfg["model"] = {"provider": "openrouter", "default": winners["reasoning"]}
    cfg["fallback_providers"] = [
        {"provider": "openrouter", "model": m} for m in order[1:4]]
    if winners["vision"]:
        cfg.setdefault("auxiliary", {})["vision"] = {
            "provider": "openrouter", "model": winners["vision"],
            "fallback_chain": [{"provider": "openrouter", "model": m}
                               for m in vision_order
                               if m != winners["vision"]][:2]}
    else:
        # Nothing eligible: leave the existing vision block untouched rather
        # than writing "model: null" and breaking every vision call.
        print("NO-VISION-CANDIDATE (keeping existing auxiliary.vision)")
    cfg.setdefault("delegation", {})["model"] = winners["cheap"]
    cfg["delegation"]["provider"] = "openrouter"
    header = ("# OpenDeamon — Hermes configuration (managed by free-rank).\n"
              "# OPENROUTER_API_KEY via environment, never stored here.\n")
    with open(HERMES_CFG, "w", encoding="utf-8") as f:
        f.write(header)
        yaml.safe_dump(cfg, f, sort_keys=False, allow_unicode=True)
    print("HERMES-CFG-REWRITTEN")

    gw = yaml.safe_load(open(GW_CFG, encoding="utf-8"))
    # od-chat <- best reasoning, od-fb1 <- runner-up (Groq/Pollinations
    # groups untouched: different providers, own fate)
    for grp, mid in (("od-chat", order[0] if order else None),
                     ("od-fb1", order[1] if len(order) > 1 else None)):
        for d in gw.get("model_list", []):
            if d.get("model_name") == grp and mid:
                d["litellm_params"]["model"] = "openrouter/" + mid
    for d in gw.get("model_list", []):
        if d.get("model_name") in ("opendeamon-vision", "od-vision") \
                and winners["vision"]:
            d["litellm_params"]["model"] = "openrouter/" + winners["vision"]
    with open(GW_CFG, "w", encoding="utf-8") as f:
        yaml.safe_dump(gw, f, sort_keys=False, allow_unicode=True)
    print("GW-CFG-REWRITTEN (takes effect on next gateway start)")


if __name__ == "__main__":
    main()
