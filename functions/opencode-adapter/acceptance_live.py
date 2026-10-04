"""Живая приёмка А2-bis против настоящего opencode v2. Пять критериев из TODO.

Этот скрипт НЕ для CI: он поднимает реальный эндпоинт, гоняет настоящий ход
модели и проверяет приёмку:

  1. задача, отданная по HTTP, доходит до opencode v2 и возвращает непустой diff;
  2. повтор той же задачи не плодит сессии;
  3. убийство эндпоинта приводит к автоподъёму;
  4. после приёмки временный каталог opencode не растёт на размер задачи;
  5. закон $0 соблюдён - только `opencode/*-free`.

Запуск (нужны учётные данные, обычно в /home/server/oc-client.env):
    /home/server/venv/bin/python functions/opencode-adapter/acceptance_live.py

Каждый критерий печатает факты, а не только «PASS»: номер сессии, размер
диффа, число сессий до/после, PID до/после kill, размер каталога до/после.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adapter  # noqa: E402
from adapter import HttpV2Transport, Task, load_endpoint  # noqa: E402
import service as svc  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SUPERVISOR = os.path.join(HERE, "supervisor.sh")
PY = sys.executable

RESULTS = []
endpoint_file = ""   # заполняется в main(), нужен супервизору


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print("%-4s %s\n       %s" % ("PASS" if ok else "FAIL", name, detail),
          flush=True)


def free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def health(port: int, timeout: float = 3.0) -> dict:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port,
                                    timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return {}


def wait_health(port: int, timeout: float = 60.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        h = health(port)
        if h.get("ok"):
            return h
        time.sleep(1)
    return {}


def post_task(port: int, text: str, directory: str,
              model: str = "space-bunny-free") -> dict:
    body = json.dumps({"text": text, "directory": directory,
                       "model": model}).encode()
    req = urllib.request.Request("http://127.0.0.1:%d/task" % port, data=body,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def get_task(port: int, digest: str) -> dict:
    with urllib.request.urlopen("http://127.0.0.1:%d/task/%s"
                                % (port, digest), timeout=30) as r:
        return json.loads(r.read())


def wait_done(port: int, digest: str, timeout: float = 900.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        last = get_task(port, digest)
        if last.get("status") in ("done", "failed", "refused"):
            return last
        time.sleep(5)
    return last


def dir_bytes(path: str) -> int:
    if not path or not os.path.isdir(path):
        return 0
    total = 0
    for d, _s, fs in os.walk(path):
        for f in fs:
            try:
                total += os.path.getsize(os.path.join(d, f))
            except OSError:
                pass
    return total


# --------------------------------------------------------------------------

def criterion_1_and_2(port: int, transport: HttpV2Transport, workdir: str,
                       tmp_dir: str) -> None:
    """(1) непустой diff через HTTP; (2) повтор не плодит сессии."""
    before = len(transport._request("GET", "/api/session") or [])
    tmp_before = dir_bytes(tmp_dir)
    text = ("In calc.py the function add(a, b) returns 0, which is wrong. "
            "Make it return a + b. Change nothing else.")
    job = post_task(port, text, workdir)
    digest = job["digest"]
    done = wait_done(port, digest)
    diff = done.get("diff") or []
    added = sum(int(d.get("additions") or 0) for d in diff)
    patch = next((d.get("patch") for d in diff if d.get("patch")), "")
    record("1. HTTP task reaches opencode v2 and returns a non-empty diff",
           bool(diff) and added > 0 and "+" in str(patch),
           "status=%s session=%s files=%s additions=%d patch_head=%r cost=%s"
           % (done.get("status"), done.get("session_id"),
              [d.get("file") for d in diff], added,
              str(patch)[:70].replace("\n", " | "), done.get("cost")))

    if diff:
        body = open(os.path.join(workdir, "calc.py"), encoding="utf-8").read()
        record("1b. the file on disk actually changed",
               "return a + b" in body, "calc.py=%r" % body.strip())

    # Повтор той же задачи. Признак идемпотентности живёт в ОТВЕТЕ на повтор:
    # он не пишется в хранилище, потому что это свойство ответа, а не записи.
    after_pre = len(transport._request("GET", "/api/session") or [])
    tmp_after_repeat = dir_bytes(tmp_dir)
    again = post_task(port, text, workdir)
    after_post = len(transport._request("GET", "/api/session") or [])
    record("2. the repeat does not create a second session",
           again.get("idempotent_replay") is True
           and again["digest"] == digest
           and after_post == before
           and again.get("session_id") == done.get("session_id"),
           "same digest=%s, replay flagged=%s, same session=%s (%s), "
           "opencode sessions before=%d after_repeat=%d after_post=%d, "
           "tmp growth=%d bytes"
           % (again["digest"] == digest,
              again.get("idempotent_replay"),
              again.get("session_id") == done.get("session_id"),
              done.get("session_id"), before, after_pre, after_post,
              tmp_after_repeat - tmp_before))

    # (4) tmp не растёт на размер задачи.
    tmp_final = dir_bytes(tmp_dir)
    record("4. opencode tmp does not grow by the size of the task",
           tmp_final - tmp_before <= 1024 * 1024,
           "tmp=%s before=%.2f MB after_repeat=%.2f MB after=%.2f MB "
           "growth=%d bytes (session deleted after acceptance)"
           % (tmp_dir, tmp_before / 1048576, tmp_after_repeat / 1048576,
              tmp_final / 1048576, tmp_final - tmp_before))

    # (5) $0: задача должна была отработать на opencode/*-free и стоить $0.
    models = transport.models()
    free = [m for m in models if m.get("free")]
    non_free = [m["ref"] for m in models if not m.get("free")]
    model_used = str(done.get("model") or "")
    record("5. the $0 rail: the task ran on opencode/*-free only",
           model_used.startswith("opencode/") and model_used.endswith("-free")
           and done.get("cost") == 0,
           "model_used=%r cost=%s (opencode reports cost per session); "
           "free models in catalogue=%d/%d, non-free ones refused by the "
           "adapter before any call: %s"
           % (model_used, done.get("cost"), len(free), len(models),
              non_free[:4]))
    return done


def criterion_3(state_dir: str, endpoint) -> None:
    """(3) убийство эндпоинта приводит к автоподъёму - на настоящем сервисе.

    Здесь нет заглушки: поднимается реальный service.py под настоящим
    супервизором, его дочерний процесс убивается, и супервизор обязан
    поднять новый. Проверяется и факт подъёма, и смена PID.
    """
    port = free_port()
    state = tempfile.mkdtemp(prefix="oc-sup-accept-")
    env = dict(os.environ)
    env.update({"OC_STATE_DIR": state, "OC_PYTHON": PY,
                "OC_LOG": os.path.join(state, "s.log"),
                "OC_SERVICE_HOST": "127.0.0.1", "OC_SERVICE_PORT": str(port),
                "OC_RESTART_DELAY": "1", "OC_MAX_RESTARTS": "20",
                "OC_CLIENT_ENV": endpoint_file or ""})
    sup = subprocess.Popen(["sh", SUPERVISOR], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True)
    try:
        if not wait_health(port, 90):
            record("3. killing the endpoint leads to auto-restart", False,
                   "the service never came up under the supervisor: %r"
                   % ((sup.stdout.read(1500) if sup.stdout else "") or ""))
            return
        pidfile = os.path.join(state, "service.pid")
        with open(pidfile) as f:
            first = f.read().strip()
        os.kill(int(first), 9)
        deadline = time.time() + 90
        second = first
        while time.time() < deadline:
            if health(port).get("ok"):
                with open(pidfile) as f:
                    second = f.read().strip()
                if second and second != first:
                    break
            time.sleep(2)
        record("3. killing the endpoint leads to auto-restart",
               bool(second) and second != first and bool(health(port).get("ok")),
               "pid %s killed with SIGKILL, supervisor brought back pid %s and "
               "/health answers again" % (first, second))
    finally:
        sup.terminate()
        try:
            sup.wait(timeout=15)
        except subprocess.TimeoutExpired:
            sup.kill()
        shutil.rmtree(state, ignore_errors=True)



def adapter_default_env_file() -> str:
    """Путь к файлу с учётными данными, который сам адаптер считает
    умолчанием. Супервизор запускает сервис в другом окружении, поэтому путь
    передаётся явно, а не наследуется по случаю."""
    return adapter.DEFAULT_ENV_FILE

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--env-file", default=None)
    ap.add_argument("--model", default="space-bunny-free")
    args = ap.parse_args()

    global endpoint_file
    try:
        endpoint = load_endpoint(args.env_file)
        endpoint_file = args.env_file or adapter_default_env_file()
    except Exception as exc:  # noqa: BLE001
        print("cannot start: %s" % exc, file=sys.stderr)
        return 2
    transport = HttpV2Transport(endpoint)
    info = transport.info()
    tmp_dir = transport.tmp_dir()
    print("opencode %s at %s (pid %s, tmp %s)"
          % (info.get("version"), endpoint.url, info.get("pid"), tmp_dir),
          flush=True)

    workdir = tempfile.mkdtemp(prefix="oc-accept-")
    state_dir = tempfile.mkdtemp(prefix="oc-accept-state-")
    port = args.port or free_port()
    try:
        with open(os.path.join(workdir, "calc.py"), "w", encoding="utf-8") as f:
            f.write("def add(a, b):\n    return 0\n")
        subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=workdir,
                       check=False)
        for k, v in (("user.email", "t@t"), ("user.name", "t")):
            subprocess.run(["git", "config", k, v], cwd=workdir, check=False)

        store = svc.JobStore(os.path.join(state_dir, "jobs.json"))
        service = svc.Service(transport=transport, store=store,
                              turn_timeout=900)
        handler = type("H", (svc.Handler,), {"service": service})
        httpd = __import__("http.server", fromlist=["x"]).ThreadingHTTPServer(
            ("127.0.0.1", port), handler)
        service.start()
        import threading
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        print("service on http://127.0.0.1:%d" % port, flush=True)
        time.sleep(1)

        criterion_1_and_2(port, transport, workdir, tmp_dir)
        criterion_3(state_dir, endpoint)

        service.runner.stop()
        httpd.shutdown()
        httpd.server_close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(state_dir, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print()
    print("acceptance: %d/%d criteria green" % (len(RESULTS) - len(failed),
                                                len(RESULTS)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())