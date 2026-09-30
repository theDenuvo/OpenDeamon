"""OpenDeamon hands bridge (v2, minimal glue, localhost only).

Exposes Hermes (`hermes -z`) as an OpenAI-compatible chat endpoint so the
LiteLLM gateway (and therefore OWUI chat) can use the full Hermes agent —
tools, skills, persona — as just another model tier (`opendeamon`).

  GET  /v1/models
  POST /v1/chat/completions  {"model":..., "messages":[...], "stream":bool}

v2 features:
- OWUI auxiliary calls (title/tags/follow-ups) answered locally: instant,
  free, no agent turn burned.
- Live progress: while hermes runs, new rows from Hermes state.db
  (tool calls) stream out as `reasoning_content` deltas, so the chat shows
  what the agent is doing instead of a dead spinner.

Env is self-contained (mirrors bootstrap.ps1); OPENROUTER_API_KEY is read
in-process from the pre-existing secrets file and never logged.
One hermes run at a time (lock); concurrent callers get HTTP 429 so the
gateway can fall back to a chat tier.
"""

import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = r"A:\OpenDeamon"
HERMES_BIN = r"A:\hermes\bin\hermes.exe"
SECRETS = r"A:\AI\daemon\config\secrets.local.toml"
STATE_DB = os.path.join(ROOT, "hermes-home", "state.db")
PORT = 9131
HERMES_TIMEOUT = 540
LOG = os.path.join(ROOT, "bridge", "hands.log")
POLL_INTERVAL = 1.5

LOCK = threading.Lock()

TITLE_RE = re.compile(
    r"(concise.{0,30}title|title.{0,30}concise|3-5 word title|"
    r"generate.{0,20}title|create.{0,20}title|short title)",
    re.IGNORECASE | re.DOTALL)
TAGS_RE = re.compile(
    r"(generate.{0,20}tags|chat tags|suggest.{0,20}tags)",
    re.IGNORECASE | re.DOTALL)
FOLLOWUP_RE = re.compile(
    r"(follow.?up.{0,40}(question|prompt)|suggest.{0,40}relevant.{0,20}"
    r"(question|prompt))",
    re.IGNORECASE | re.DOTALL)


def log(msg):
    line = "%s %s\n" % (time.strftime("%H:%M:%S"), msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line)


def build_env():
    env = dict(os.environ)
    env["HERMES_HOME"] = os.path.join(ROOT, "hermes-home")
    env["UV_CACHE_DIR"] = os.path.join(ROOT, "cache", "uv")
    env["PIP_CACHE_DIR"] = os.path.join(ROOT, "cache", "pip")
    env["NPM_CONFIG_CACHE"] = os.path.join(ROOT, "cache", "npm")
    env["PLAYWRIGHT_BROWSERS_PATH"] = os.path.join(
        ROOT, "cache", "ms-playwright")
    env["TEMP"] = os.path.join(ROOT, "cache", "tmp")
    env["TMP"] = os.path.join(ROOT, "cache", "tmp")
    env["UV_NO_MANAGED_PYTHON"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if r"A:\hermes\bin" not in env.get("PATH", ""):
        env["PATH"] = r"A:\hermes\bin;" + env.get("PATH", "")
    text = open(SECRETS, encoding="utf-8").read()
    m = re.search(r"api_key\s*=\s*[\"']([^\"']+)", text)
    if m:
        env["OPENROUTER_API_KEY"] = m.group(1)
    return env


def local_aux_answer(prompt):
    """Answer OWUI auxiliary calls (title/tags/follow-ups) locally: instant,
    free, no agent turn. Returns text or None if not an auxiliary call."""
    if len(prompt) > 3000:
        return None
    low = prompt[:2000]
    if TITLE_RE.search(low):
        lines = [ln.strip() for ln in prompt.splitlines() if ln.strip()]
        cands = [ln for ln in lines
                 if len(ln) > 12
                 and "title" not in ln.lower()
                 and not TITLE_RE.search(ln)]
        if not cands:
            longest = max(lines, key=len) if lines else "chat"
            return " ".join(longest.split()[:6])[:60] or "chat"
        users = [ln for ln in cands if ln.lower().startswith("user:")]
        base = users[-1] if users else cands[0]
        base = re.sub(r"^(user|assistant)\s*:\s*", "", base,
                      flags=re.IGNORECASE)
        words = base.split()
        return " ".join(words[:6])[:60] or "chat"
    if TAGS_RE.search(low):
        return "[]"
    if FOLLOWUP_RE.search(low):
        return '{"follow_ups": []}'
    return None


def prompt_from_messages(messages):
    parts = []
    img_dir = os.path.join(ROOT, "cache", "tmp", "hands-img")
    os.makedirs(img_dir, exist_ok=True)
    img_n = 0
    for m in messages or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if isinstance(content, list):
            texts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    texts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    url = (p.get("image_url") or {}).get("url", "")
                    if url.startswith("data:"):
                        import base64
                        header, _, b64 = url.partition(",")
                        ext = "png" if "png" in header else "jpg"
                        img_n += 1
                        fp = os.path.join(
                            img_dir, "img_%d_%d.%s" % (os.getpid(), img_n, ext))
                        with open(fp, "wb") as f:
                            f.write(base64.b64decode(b64))
                        texts.append("[attached image saved at %s — "
                                     "use vision_analyze on it]" % fp)
                    elif url.startswith(("http://", "https://", "A:/", "A:\\")):
                        texts.append("[attached image: %s — "
                                     "use vision_analyze on it]" % url)
            content = " ".join(texts)
        if role == "system":
            parts.append("System: " + str(content))
        elif role == "user":
            parts.append(str(content))
    return "\n\n".join(parts).strip()


def db_max_id():
    try:
        con = sqlite3.connect("file:%s?mode=ro" % STATE_DB.replace("\\", "/"),
                              uri=True, timeout=5)
        row = con.execute("SELECT MAX(id) FROM messages").fetchone()
        con.close()
        return row[0] or 0
    except Exception:
        return 0


def db_new_rows(after_id):
    """Rows committed since after_id: (id, role, tool_name, snippet)."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % STATE_DB.replace("\\", "/"),
                              uri=True, timeout=5)
        rows = list(con.execute(
            "SELECT id, role, tool_name, substr(content,1,160) FROM messages "
            "WHERE id > ? ORDER BY id", (after_id,)))
        con.close()
        return rows
    except Exception:
        return []


def render_progress(role, tool_name, content):
    # Only tool calls become progress lines: assistant texts would
    # duplicate the final answer (it is also stored as a message row).
    if role == "tool":
        return "\u2699 %s" % (tool_name or "tool")
    return None


def sse_chunk(model, delta):
    return {"id": "chatcmpl-od", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": delta,
                         "finish_reason": None}]}


def sse_final(model):
    return {"id": "chatcmpl-od", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model,
            "choices": [{"index": 0, "delta": {},
                         "finish_reason": "stop"}]}


class Handler(BaseHTTPRequestHandler):
    server_version = "OpenDeamonHands/2"

    def _send(self, code, obj=None, raw=None, ctype="application/json"):
        if raw is None:
            raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        body = raw
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_sse(self, events):
        raw = "".join("data: %s\n\n" % json.dumps(e, ensure_ascii=False)
                      for e in events)
        raw += "data: [DONE]\n\n"
        self._send(200, raw=raw.encode("utf-8"),
                   ctype="text/event-stream")

    def _answer(self, model, text, stream, reasoning=()):
        if stream:
            events = [sse_chunk(model, {"reasoning_content": r})
                      for r in reasoning]
            words = text.split(" ")
            step = max(1, len(words) // 40)
            for i in range(0, len(words), step):
                piece = " ".join(words[i:i + step])
                if i > 0:
                    piece = " " + piece
                events.append(sse_chunk(model, {"content": piece}))
            events.append(sse_final(model))
            return self._send_sse(events)
        out = {"id": "chatcmpl-od", "object": "chat.completion",
               "created": int(time.time()), "model": model,
               "choices": [{"index": 0,
                            "message": {"role": "assistant",
                                        "content": text},
                            "finish_reason": "stop"}]}
        if reasoning:
            out["choices"][0]["message"]["reasoning_content"] = \
                "\n".join(reasoning)
        return self._send(200, out)

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            return self._send(200, {"data": [
                {"id": "hermes-hands", "object": "model",
                 "created": 0, "owned_by": "opendeamon"}]})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            return self._send(404, {"error": "not found"})
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        prompt = prompt_from_messages(req.get("messages"))
        if not prompt:
            return self._send(400, {"error": "empty prompt"})
        model = req.get("model", "hermes-hands")
        stream = bool(req.get("stream"))
        aux = local_aux_answer(prompt)
        if aux is not None:
            log("AUX short-circuit chars=%d" % len(prompt))
            return self._answer(model, aux, stream)
        if not LOCK.acquire(blocking=False):
            return self._send(429, {"error": "hands busy"})
        try:
            log("RUN prompt_chars=%d stream=%s" % (len(prompt), stream))
            text, reasoning, sent = self._run_live(prompt, model, stream)
            log("DONE reply_chars=%d steps=%d" % (len(text), len(reasoning)))
        except subprocess.TimeoutExpired:
            return self._send(504, {"error": "hermes timeout"})
        except Exception as e:
            log("ERR %r" % e)
            return self._send(500, {"error": "hermes failed"})
        finally:
            LOCK.release()
        if sent:
            return None
        return self._answer(model, text, stream, reasoning)

    def _run_live(self, prompt, model, stream):
        """Run hermes, polling state.db for live tool progress. In stream
        mode forwards reasoning deltas as they arrive; otherwise collects
        them for the final payload."""
        start_id = db_max_id()
        start_ts = time.time()
        proc = subprocess.Popen(
            [HERMES_BIN, "-z", prompt],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace",
            cwd=ROOT, env=build_env())
        reasoning = []
        seen_ts = start_ts
        sent = False
        if stream:
            self.send_response(200)
            self.send_header("Content-Type",
                             "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        try:
            while True:
                rc = proc.poll()
                if rc is not None:
                    break
                for _id, role, tool, content in db_new_rows(start_id):
                    start_id = max(start_id, _id)
                    line = render_progress(role, tool, content)
                    if line and line not in reasoning:
                        reasoning.append(line)
                        if stream:
                            ev = ("data: %s\n\n" % json.dumps(
                                sse_chunk(model, {"reasoning_content": line}),
                                ensure_ascii=False))
                            self.wfile.write(ev.encode("utf-8"))
                            self.wfile.flush()
                if stream and time.time() - seen_ts > 20:
                    seen_ts = time.time()
                    ev = ("data: %s\n\n" % json.dumps(
                        sse_chunk(model, {"reasoning_content":
                                          "\u2026 still working"}),
                        ensure_ascii=False))
                    self.wfile.write(ev.encode("utf-8"))
                    self.wfile.flush()
                time.sleep(POLL_INTERVAL)
            out, _ = proc.communicate(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
        for _id, role, tool, content in db_new_rows(start_id):
            line = render_progress(role, tool, content)
            if line and line not in reasoning:
                reasoning.append(line)
        text = (out or "").strip()
        if not text:
            text = "(empty response)"
        if stream:
            words = text.split(" ")
            step = max(1, len(words) // 40)
            for i in range(0, len(words), step):
                piece = " ".join(words[i:i + step])
                if i > 0:
                    piece = " " + piece
                ev = ("data: %s\n\n" % json.dumps(
                    sse_chunk(model, {"content": piece}),
                    ensure_ascii=False))
                self.wfile.write(ev.encode("utf-8"))
            fin = ("data: %s\n\n" % json.dumps(sse_final(model),
                                              ensure_ascii=False))
            self.wfile.write(fin.encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            sent = True
        return text, reasoning, sent

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    for d in ("cache", "bridge"):
        os.makedirs(os.path.join(ROOT, d), exist_ok=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    log("LISTEN 127.0.0.1:%d" % PORT)
    srv.serve_forever()
