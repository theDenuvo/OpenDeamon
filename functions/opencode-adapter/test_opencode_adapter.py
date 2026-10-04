"""Тесты адаптера и сервиса кодинга. Только stdlib, без сети и без моделей.

Приёмка из TODO (А2-bis) проверяется на живом сервере отдельным скриптом
`acceptance_live.py`: там нужны настоящие ходы opencode. Этот набор проверяет
то, что обязано быть верным независимо от того, есть ли opencode под рукой:
идемпотентность, отказ без авторизации, закон $0, громкий порог диска,
health и запрет второго экземпляра супервизора.

Транспорт в тестах - подставной: он записывает вызовы и умеет вернуть
непустой diff, задержку и ошибку. Настоящий HTTP-клиент проверен живым
прогоном, а не догадкой.

Запуск: py A:/OpenDeamon/functions/opencode-adapter/test_opencode_adapter.py
"""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adapter  # noqa: E402
import service as svc  # noqa: E402
from adapter import CodingSession, Task  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SUPERVISOR = os.path.join(HERE, "supervisor.sh")

TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


# --------------------------------------------------------------------------
# подставной транспорт
# --------------------------------------------------------------------------

class FakeTransport(CodingSession):
    """Записывает всё, что просят, и отдаёт заранее заданное."""

    name = "fake"

    def __init__(self, files=("calc.py",), free_bytes=99 * 1024 ** 3,
                 turn_ok=True, turn_seconds=0.0, cost=0.0, tmp=""):
        self.calls = []
        self.files = list(files)
        self.free_bytes = free_bytes
        self.turn_ok = turn_ok
        self.turn_seconds = turn_seconds
        self.cost = cost
        self._tmp = tmp

    def models(self):
        self.calls.append(("models",))
        return [{"provider": "opencode", "id": "space-bunny-free",
                 "ref": "opencode/space-bunny-free", "free": True}]

    def create(self, task):
        task.check_free()
        self.calls.append(("create", task.digest))
        return "ses_fake_%d" % len(self.calls)

    def set_agent(self, session_id, agent):
        self.calls.append(("set_agent", session_id, agent))

    def send(self, session_id, text):
        self.calls.append(("send", session_id, text))

    def wait_for_turn(self, session_id, timeout=900, poll=10):
        self.calls.append(("wait", session_id))
        if self.turn_seconds:
            time.sleep(self.turn_seconds)
        return ({"ok": True, "reason": "fake"}
                if self.turn_ok else
                {"ok": False, "reason": "no assistant turn"})

    def diff(self, session_id):
        self.calls.append(("diff", session_id))
        return [{"file": f, "patch": "diff --git a/%s b/%s" % (f, f),
                 "additions": 1, "deletions": 1, "status": "modified"}
                for f in self.files]

    def session(self, session_id):
        return {"cost": self.cost}

    def delete(self, session_id):
        self.calls.append(("delete", session_id))
        return True

    def tmp_dir(self):
        return self._tmp

    def disk_free_bytes(self, path):
        # Настоящее свободное место подставлять нельзя: проверка порога
        # должна быть воспроизводимой, а не зависеть от того, сколько места
        # осталось на tmpfs в момент прогона.
        return self.free_bytes


class RealTransport:
    """Методы HttpV2Transport, которые не должны вызываться в тестах."""

    def __getattr__(self, name):
        def boom(*a, **k):
            raise AssertionError("the test must not reach the network: %s"
                                 % name)
        return boom


def workdir():
    d = tempfile.mkdtemp(prefix="oc-adapter-")
    return d


def wait_for(predicate, timeout=10.0, poll=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(poll)
    return False


def drain(runner, timeout=20.0):
    """Дождаться, пока очередь опустеет и воркер закончит текущую задачу."""
    return wait_for(lambda: runner.depth() == 0 and runner.q.unfinished_tasks == 0,
                    timeout=timeout)


# --------------------------------------------------------------------------
# задача и отпечаток
# --------------------------------------------------------------------------

@test
def test_digest_is_stable_and_task_specific():
    """Отпечаток - ключ идемпотентности, поэтому он обязан быть
    воспроизводимым и различать разные задачи.

    Если бы отпечаток зависел от порядка полей или от времени, повтор задачи
    после рестарта создал бы вторую сессию - ровно то, чего мы добиваемся."""
    d = workdir()
    try:
        a = Task("do the thing", d)
        b = Task("do the thing", d)
        c = Task("do another thing", d)
        e = Task("do the thing", d, model_id="ling-3.1-flash-free")
        assert a.digest == b.digest, "same task must digest identically"
        assert a.digest != c.digest, "different text must differ"
        assert a.digest != e.digest, "different model must differ"
        other = Task("do the thing", tempfile.mkdtemp())
        assert a.digest != other.digest, "different directory must differ"
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def test_only_opencode_free_is_allowed():
    """Закон $0 на входе, до создания сессии.

    Платная модель обязана быть отказом, а не предупреждением: вызов уже
    нельзя отменить, и деньги уже списаны."""
    d = workdir()
    try:
        Task("x", d, model_id="space-bunny-free").check_free()
        for bad in ("gpt-4", "gpt-5.5", "space-bunny"):
            try:
                Task("x", d, model_id=bad).check_free()
            except adapter.ModelNotFree:
                continue
            return ["model %r was allowed; the $0 rail is broken" % bad]
        try:
            Task("x", d, provider="openrouter",
                 model_id="ling-3.0-flash-fin-free").check_free()
        except adapter.ModelNotFree:
            return []
        return ["a non-opencode provider was allowed"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def test_no_credentials_means_no_start():
    """Сервис без авторизации не запускается. Литералов в коде нет."""
    saved = {k: os.environ.pop(k, None) for k in
             ("OC_URL", "OC_USER", "OC_PASSWORD")}
    old_file = adapter.DEFAULT_ENV_FILE
    try:
        adapter.DEFAULT_ENV_FILE = os.path.join(
            tempfile.mkdtemp(prefix="oc-noenv-"), "absent.env")
        try:
            adapter.load_endpoint()
        except adapter.AuthRequired as exc:
            text = str(exc)
            if "OC_URL" not in text:
                return ["the refusal must name what is missing: %r" % text]
            return []
        return ["load_endpoint() succeeded without credentials"]
    finally:
        adapter.DEFAULT_ENV_FILE = old_file
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


@test
def test_credentials_come_only_from_env_or_file():
    """Файл `oc-client.env` - единственный источник, и пароль не утёкает в
    repr: в лог супервизора попадает repr, а не содержимое секрета."""
    d = workdir()
    try:
        env_path = os.path.join(d, "oc-client.env")
        # Пароль должен быть узнаваемым: проверка «нет ли его в repr»
        # бессмысленна, если пароль - это одна буква, которую напечатает
        # само слово `password`.
        with open(env_path, "w", encoding="utf-8") as f:
            f.write("# comment\nOC_URL=http://127.0.0.1:4096\n"
                    "OC_USER='oc-user'\nOC_PASSWORD=\"oc-s3cret-pw\"\n\n")
        saved = {k: os.environ.pop(k, None) for k in
                 ("OC_URL", "OC_USER", "OC_PASSWORD")}
        try:
            ep = adapter.load_endpoint(env_path)
            if (ep.url, ep.user, ep.password) != (
                    "http://127.0.0.1:4096", "oc-user", "oc-s3cret-pw"):
                return ["parsed wrong: %r" % (ep,)]
            if "oc-s3cret-pw" in repr(ep):
                return ["the password is visible in repr: %r" % repr(ep)]
            os.environ["OC_URL"] = "http://override:1"
            ep2 = adapter.load_endpoint(env_path)
            if ep2.url != "http://override:1":
                return ["the environment must override the file"]
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# идемпотентность
# --------------------------------------------------------------------------

@test
def test_repeat_does_not_create_a_second_session():
    """Главное требование приёмки (2): повтор той же задачи не плодит сессии.

    Клиент круглосуточного эндпоинта повторяет по таймауту; без этого
    каждый повтор стоил бы отдельного хода модели и отдельной сессии."""
    d = workdir()
    try:
        transport = FakeTransport()
        store = svc.JobStore(os.path.join(d, "jobs.json"))
        service = svc.Service(transport=transport, store=store)
        service.runner.turn_timeout = 20
        service.start()
        try:
            task = Task("make add() return a+b", d)
            first = service.submit(task)
            assert drain(service.runner), "the queue never drained"
            assert wait_for(lambda: (service.store.get(task.digest) or {})
                            .get("status") == "done"), "task never finished"
            second = service.submit(Task("make add() return a+b", d))
            if not second.get("idempotent_replay"):
                return ["the repeat was not recognised as a replay: %r" % second]
            if second.get("status") != "done":
                return ["the repeat lost the finished result: %r" % second]
            if not second.get("diff"):
                return ["the replay returned no diff"]
            creates = [c for c in transport.calls if c[0] == "create"]
            if len(creates) != 1:
                return ["expected exactly 1 session, got %d" % len(creates)]
        finally:
            service.runner.stop()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def test_a_replay_after_restart_still_knows_the_task():
    """Идемпотентность должна переживать перезапуск сервиса, иначе рестарт
    24/7-эндпоинта превращается в способ удвоить работу."""
    d = workdir()
    try:
        path = os.path.join(d, "jobs.json")
        store = svc.JobStore(path)
        task = Task("survive a restart", d)
        store.put(task.digest, {"status": "done", "diff": [{"file": "a"}],
                                "session_id": "ses_old", "error": ""})
        # Новый процесс, новое хранилище, тот же файл на диске.
        reopened = svc.JobStore(path)
        job = reopened.get(task.digest)
        if not job or job.get("status") != "done":
            return ["the record did not survive the reopen: %r" % (job,)]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def test_one_task_makes_exactly_one_session_and_it_is_deleted():
    """Требование (5): одна задача - одна сессия, и сессия убирается после
    приёмки результата. Порядок важен: результат пишется ДО удаления."""
    d = workdir()
    try:
        transport = FakeTransport()
        store = svc.JobStore(os.path.join(d, "jobs.json"))
        service = svc.Service(transport=transport, store=store)
        service.start()
        try:
            task = Task("one task one session", d)
            service.submit(task)
            assert drain(service.runner), "queue never drained"
            assert wait_for(lambda: (service.store.get(task.digest) or {})
                            .get("status") == "done"), "not finished"
            kinds = [c[0] for c in transport.calls]
            if kinds.count("create") != 1:
                return ["expected 1 create, got %d" % kinds.count("create")]
            if kinds.count("delete") != 1:
                return ["the session was not deleted: %s" % kinds]
            job = service.store.get(task.digest)
            if kinds.index("diff") > kinds.index("delete"):
                return ["the diff was read after the session was deleted"]
            if not job.get("session_removed"):
                return ["the store does not record the cleanup"]
        finally:
            service.runner.stop()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# уборка и порог диска
# --------------------------------------------------------------------------

@test
def test_low_disk_refuses_loudly_instead_of_dying_quietly():
    """Требование (5): порог диска обязан быть громким.

    Проверяется не «вернулся ли отказ», а попала ли причина в stderr: молчаливое
    «съело место и упало» - это худший вариант, потому что чинить уже поздно.
    Ниже жёсткого порога задача не принимается ВООБЩЕ."""
    d = tempfile.mkdtemp(prefix="oc-lowdisk-")
    real = os.path.join(d, "tmp")
    os.makedirs(real)
    try:
        transport = FakeTransport(tmp=real,
                                  free_bytes=svc.MIN_FREE_BYTES - 1024 ** 2)
        store = svc.JobStore(os.path.join(d, "jobs.json"))
        service = svc.Service(transport=transport, store=store)
        service.start()
        stderr = io.StringIO()
        real_stderr, sys.stderr = sys.stderr, stderr
        try:
            task = Task("refused because the disk is full", d)
            service.submit(task)
            assert drain(service.runner), "queue never drained"
            assert wait_for(lambda: (service.store.get(task.digest) or {})
                            .get("status") == "refused"), "not refused"
        finally:
            sys.stderr = real_stderr
            service.runner.stop()
        job = service.store.get(task.digest)
        if "low disk" not in stderr.getvalue():
            return ["nothing was written to stderr: %r" % stderr.getvalue()[:200]]
        if "low disk" not in (job.get("error") or ""):
            return ["the refusal reason is missing from the job: %r" % job]
        if [c for c in transport.calls if c[0] == "create"]:
            return ["a session was created despite the refusal"]
        if service.health()["ok"]:
            return ["/health claims ok while the disk is full: %r"
                    % service.health()]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def test_low_disk_below_warn_still_works_but_says_so():
    """Между порогами работа не блокируется, но молчать нельзя.

    Блокировать задачу, которой нужно килобайты, из-за нехватки места -
    это тоже отказ: на этом хосте временный каталог opencode лежит на
    `/tmp` в 2 ГБ, и строгий запас просто сделал бы сервис мёртвым."""
    d = tempfile.mkdtemp(prefix="oc-warndisk-")
    real = os.path.join(d, "tmp")
    os.makedirs(real)
    try:
        between = (svc.MIN_FREE_BYTES + svc.WARN_FREE_BYTES) // 2
        transport = FakeTransport(tmp=real, free_bytes=between)
        store = svc.JobStore(os.path.join(d, "jobs.json"))
        service = svc.Service(transport=transport, store=store)
        service.start()
        stderr = io.StringIO()
        real_stderr, sys.stderr = sys.stderr, stderr
        try:
            task = Task("small task on a nearly full disk", d)
            service.submit(task)
            assert drain(service.runner), "queue never drained"
            assert wait_for(lambda: (service.store.get(task.digest) or {})
                            .get("status") == "done"), "the task was blocked"
        finally:
            sys.stderr = real_stderr
            service.runner.stop()
        if "WARNING" not in stderr.getvalue():
            return ["the warning was not printed: %r" % stderr.getvalue()[:200]]
        if not service.health().get("loud"):
            return ["/health does not carry the warning"]
        if not service.health().get("ok"):
            return ["/health claims the service is down over a warning"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def test_health_reports_the_queue_and_the_rail():
    """`/health` - то, чем супервизор решает, жив ли процесс. Пустой или
    вечно зелёный health бесполезен."""
    transport = FakeTransport()
    store = svc.JobStore(os.path.join(workdir(), "jobs.json"))
    service = svc.Service(transport=transport, store=store)
    h = service.health()
    for key in ("ok", "transport", "queue_depth", "jobs", "disk",
                "min_free_bytes"):
        if key not in h:
            return ["/health has no %r: %r" % (key, h)]
    if h["transport"] != "fake":
        return ["/health does not name the transport: %r" % h]
    if not h["ok"]:
        return ["/health not ok on a healthy fake: %r" % h]
    return []


# --------------------------------------------------------------------------
# HTTP-поверх
# --------------------------------------------------------------------------

@test
def test_http_task_and_health_endpoints():
    """Поверхность эндпоинта проверяется по-настоящему: поднимаем сервер на
    порту 0 (操作系统 выдаст свободный), бьём настоящим HTTP-запросом."""
    d = workdir()
    try:
        transport = FakeTransport()
        store = svc.JobStore(os.path.join(d, "jobs.json"))
        service = svc.Service(transport=transport, store=store)
        handler = type("H", (svc.Handler,), {"service": service})
        httpd = __import__("http.server", fromlist=["x"]).ThreadingHTTPServer(
            ("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        service.start()
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            health = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/health" % port, timeout=10).read())
            if not health.get("ok"):
                return ["/health not ok: %r" % health]
            body = json.dumps({"text": "fix add()", "directory": d}).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:%d/task" % port, data=body,
                headers={"Content-Type": "application/json"}, method="POST")
            job = json.loads(urllib.request.urlopen(req, timeout=10).read())
            if job.get("status") != "accepted":
                return ["POST /task did not accept: %r" % job]
            if not job.get("digest"):
                return ["the accepted job has no digest: %r" % job]
            assert drain(service.runner), "queue never drained"
            assert wait_for(lambda: (store.get(job["digest"]) or {})
                            .get("status") == "done"), "never done"
            got = json.loads(urllib.request.urlopen(
                "http://127.0.0.1:%d/task/%s" % (port, job["digest"]),
                timeout=10).read())
            if not got.get("diff"):
                return ["the finished job carries no diff: %r" % got]
        finally:
            service.runner.stop()
            httpd.shutdown()
            httpd.server_close()
    finally:
        shutil.rmtree(d, ignore_errors=True)


@test
def test_a_failing_turn_is_reported_and_the_session_still_removed():
    """Провал хода не должен оставлять сессию висеть и не должен выдавать
    «успех» с пустым diff."""
    d = workdir()
    try:
        transport = FakeTransport(turn_ok=False)
        store = svc.JobStore(os.path.join(d, "jobs.json"))
        service = svc.Service(transport=transport, store=store)
        service.start()
        try:
            task = Task("this will not get an answer", d)
            service.submit(task)
            assert drain(service.runner), "queue never drained"
            assert wait_for(lambda: (store.get(task.digest) or {})
                            .get("status") == "failed"), "not failed"
            job = store.get(task.digest)
            if not job.get("error"):
                return ["a failed turn must carry a reason"]
            kinds = [c[0] for c in transport.calls]
            if kinds.count("delete") != 1:
                return ["a failed turn must still delete its session, calls=%s"
                        % kinds]
            if kinds.count("create") != 1:
                return ["a failed turn made %d sessions" % kinds.count("create")]
            if job.get("diff"):
                return ["a failed task must not carry a diff"]
        finally:
            service.runner.stop()
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# супервизор
# --------------------------------------------------------------------------

@test
def test_supervisor_refuses_a_second_instance():
    """Требование (4): второй экземпляр подняться не должен. Две очереди к
    одному репозиторию - это две сессии на одну задачу."""
    if not os.path.isfile(SUPERVISOR):
        return ["supervisor.sh is missing"]
    state = tempfile.mkdtemp(prefix="oc-sup-")
    try:
        env = dict(os.environ)
        env.update({"OC_STATE_DIR": state, "OC_LOG": os.path.join(state, "s.log"),
                    "OC_PYTHON": sys.executable})
        # Живой «сервис»: процесс, который можно увидеть через PID-файл.
        proc = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(60)"])
        try:
            with open(os.path.join(state, "service.pid"), "w") as f:
                f.write(str(proc.pid))
            out = subprocess.run(["sh", SUPERVISOR], env=env,
                                 capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                return ["the supervisor started a second instance"]
            if "already alive" not in (out.stderr + out.stdout):
                return ["the refusal is not explained: %r" % out.stderr[-200:]]
        finally:
            proc.kill()
            proc.wait()
    finally:
        shutil.rmtree(state, ignore_errors=True)


@test
def test_supervisor_restarts_after_the_service_is_killed():
    """Требование (4) и приёмка (3): убийство эндпоинта приводит к
    автоподъёму.

    Подменяем сервис крошечной заглушкой, которая поднимает HTTP на том же
    порту с живым /health, чтобы проверялся именно супервизор, а не opencode."""
    if not os.path.isfile(SUPERVISOR):
        return ["supervisor.sh is missing"]
    state = tempfile.mkdtemp(prefix="oc-sup2-")
    stub = os.path.join(state, "stub_service.py")
    with open(stub, "w", encoding="utf-8") as f:
        # Заглушка должна принимать РОВНО те же флаги, что и настоящий
        # service.py: супервизор зовёт `--host H --port P`, и подмена,
        # которая разбирает argv иначе, проверяла бы не супервизор.
        f.write(
            "import argparse, json\n"
            "from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer\n"
            "class H(BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        raw=json.dumps({'ok':True,'stub':True}).encode()\n"
            "        self.send_response(200)\n"
            "        self.send_header('Content-Length',str(len(raw)))\n"
            "        self.end_headers(); self.wfile.write(raw)\n"
            "    def log_message(self,*a): pass\n"
            "ap=argparse.ArgumentParser(); ap.add_argument('--host')\n"
            "ap.add_argument('--port', type=int); a=ap.parse_args()\n"
            "print('STUB START', flush=True)\n"
            "ThreadingHTTPServer((a.host,a.port),H).serve_forever()\n")
    try:
        port = _free_port()
        env = dict(os.environ)
        env.update({"OC_STATE_DIR": state, "OC_LOG": os.path.join(state, "s.log"),
                    "OC_SERVICE_HOST": "127.0.0.1", "OC_SERVICE_PORT": str(port),
                    "OC_RESTART_DELAY": "1", "OC_PYTHON": sys.executable})
        # Супервизор зовёт `$HERE/service.py`, поэтому подмена делается
        # копированием в отдельный каталог: сам supervisor.sh не правится,
        # проверяется ровно тот файл, который поедет в репозиторий.
        sandbox = os.path.join(state, "pkg")
        os.makedirs(sandbox, exist_ok=True)
        shutil.copy(SUPERVISOR, os.path.join(sandbox, "supervisor.sh"))
        shutil.copy(stub, os.path.join(sandbox, "service.py"))
        env["OC_PYTHON"] = sys.executable
        sup = subprocess.Popen(["sh", os.path.join(sandbox, "supervisor.sh")],
                               env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True)
        try:
            up = wait_for(lambda: _health(port), timeout=45)
            if not up:
                out = sup.stdout.read(2000) if sup.stdout else ""
                return ["the stub never came up under the supervisor: %r" % out]
            first = _pid_from(state)
            os.kill(int(first), 9)
            back = wait_for(lambda: _health(port)
                            and _pid_from(state) not in (first, ""), timeout=45)
            if not back:
                return ["the endpoint did not come back after a kill "
                        "(pid was %s)" % first]
            if _pid_from(state) in ("", first):
                return ["the pidfile was not updated after the restart"]
        finally:
            sup.terminate()
            try:
                sup.wait(timeout=10)
            except subprocess.TimeoutExpired:
                sup.kill()
    finally:
        shutil.rmtree(state, ignore_errors=True)


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _health(port: int) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/health" % port,
                                    timeout=3) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def _pid_from(state: str) -> str:
    try:
        with open(os.path.join(state, "service.pid")) as f:
            return f.read().strip()
    except OSError:
        return ""


def main() -> int:
    failed = 0
    for label, fn in TESTS:
        try:
            fails = list(fn() or [])
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
    print("groups: %d total, %d passed, %d failed"
          % (len(TESTS), len(TESTS) - failed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())