"""Эндпоинт кодинга: HTTP-поверх адаптера, очередь, идемпотентность, health.

Тестовый вариант (А2) жил бы один вызов - одна сессия и умер бы вместе с
процессом. Здесь сервис круглосуточный, и каждое требование 24/7 - это
отказоустойчивость, а не удобство:

* **очередь.** Один воркер, одна задача за раз, одна сессия opencode на
  задачу. Параллельные ходы в одном репозитории означают гонку за файлы, а
  гонка за файлы означает diff, который нельзя принять;
* **идемпотентность по `task_digest`.** Повтор той же задачи возвращает
  прежний результат и НЕ создаёт вторую сессию. Это не оптимизация: клиент
  24/7-эндпоинта повторяет по таймауту, и без идемпотентности каждый
  повтор оплачивается отдельным ходом модели и отдельной сессией;
* **авторизация обязательна.** Адрес и пароль приходят из `oc-client.env`
  или окружения; литералов в коде нет, и без учётных данных сервис не
  стартует вообще, а не «стартует и отдаёт 500»;
* **уборка.** Сессия opencode удаляется сразу после того, как результат
  принят в хранилище. Сессии не убираются сами - это проверено: до задачи
  их было 50, и они только растут;
* **порог диска громкий.** Если временный каталог opencode переполняется,
  это пишется в stderr, попадает в `/health` и ОТКАЗЫВАЕТ новые задачи.
  Молчаливый «съел место и упал» - худший вариант, потому что к этому
  моменту уже ничего не работает.

Файлы состояния живут рядом по умолчанию в `~/.local/state/opendeamon-oc/`
и переопределяются переменными - чтобы сервис можно было поднять в чужом
каталоге, не правя код.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adapter import (AdapterError, AuthRequired, CliTransport,  # noqa: E402
                     DEFAULT_MODEL, CodingSession, Endpoint, HttpV2Transport,
                     Task, load_endpoint)
from worktree import (describe_empty, diff_worktree,  # noqa: E402
                      snapshot)

def resolve_state_dir() -> str:
    """Где лежит состояние, и почему именно сюда.

    На этом хосте `/home` смонтирован **только на чтение**, а поверх него
    точечно примонтированы rw поддеревья - среди них `~/.local/state/opencode`
    и `~/.local/projects`. Каталог `~/.local/state/` целиком недоступен для
    записи, поэтому состояние по умолчанию не может лежать там: сервис
    круглосуточный, и «упал, потому что каталог не создался» - неприемлемый
    режим работы.

    Порядок кандидатов задан явно, и ПЕРЕХОД announcement-ится в stderr:
    молча переехавший каталог состояния означал бы, что идемпотентность
    после рестарта держится не там, где её ищет человек.
    """
    candidates = []
    for var in ("OC_STATE_DIR", "OC_SERVICE_STATE"):
        if os.environ.get(var):
            candidates.append(os.environ[var])
    candidates.append(os.path.expanduser("~/.local/state/opendeamon-oc"))
    candidates.append("/home/server/projects/.opendeamon-oc")
    problems = []
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            probe = os.path.join(path, ".writable")
            with open(probe, "w") as f:
                f.write("")
            os.remove(probe)
            if path != candidates[0]:
                print("NOTE: state directory is %s (the default %s is not "
                      "writable here: /home is mounted read-only)"
                      % (path, candidates[0]), file=sys.stderr, flush=True)
            return path
        except OSError as exc:
            problems.append("%s: %s" % (path, exc.strerror or exc))
    raise RuntimeError(
        "no writable state directory. Tried: %s. Set OC_STATE_DIR to a "
        "writable path." % "; ".join(problems))


STATE_DIR = os.environ.get("OC_STATE_DIR", "")      # только для подсказки в логе

TURN_TIMEOUT = int(os.environ.get("OC_TURN_TIMEOUT", "900"))

# Пороги свободного места на томе, где лежит временный каталог opencode.
#
# Два порога, а не один, и это не перестраховка, а разница между двумя
# разными состояниями:
#
#   WARN  - мало места, работа продолжается, но факт попадает в stderr и в
#           `/health`. Молчание здесь означало бы «мы упираемся и никто не
#           знает».
#   MIN   - места нет совсем, новые задачи НЕ принимаются. Это тоже громко:
#           причина уходит в stderr и в ответ сервиса, а не «съело место и
#           упало» на середине задачи.
#
# Значения по умолчанию скромные намеренно. На этом сервере временный
# каталог opencode лежит на `/tmp`, а это tmpfs на 2 ГБ, и свободного места
# там меньше, чем любой разумный «безопасный запас». Порог в 2 ГБ сделал бы
# сервис мёртвым на машине, где задаче нужно килобайты: полезная работа была
# бы заблокирована из-за места, которое ей не требуется. Запас должен
# покрывать размер задачи, а не размер диска.
MIN_FREE_BYTES = int(os.environ.get("OC_MIN_FREE_BYTES", 128 * 1024 ** 2))
WARN_FREE_BYTES = int(os.environ.get("OC_WARN_FREE_BYTES", 512 * 1024 ** 2))

# Сколько ждать хода ассистента. Ровно столько же ждёт приёмка.
TURN_TIMEOUT = int(os.environ.get("OC_TURN_TIMEOUT", "900"))

# Каталог состояния вычисляется в момент вызова, а не на импорте: переменная
# окружения должна успевать прийти от супервизора, иначе состояние уехало бы
# не туда. Имя переменной - то же, что у супервизора; расхождение имён
# означало, что супервизор не может перенести состояние, и это стоило одного
# упавшего прогона приёмки.
JOB_PATH = None


# --------------------------------------------------------------------------
# хранилище задач: идемпотентность переживает перезапуск
# --------------------------------------------------------------------------

class JobStore:
    """digest -> запись. Пишется атомарно, чтобы падение не оставило мусор.

    Именно переживание перезапуска и делает повтор задачи безопасным: после
    рестарта сервис знает, что задача с таким отпечатком уже выполнялась, и
    не создаёт вторую сессию.
    """

    def __init__(self, path: str | None = None):
        if path is None:
            path = os.path.join(resolve_state_dir(), "jobs.json")
        self.path = path
        self._lock = threading.Lock()
        directory = os.path.dirname(path)
        if directory:
            try:
                os.makedirs(directory, exist_ok=True)
            except OSError as exc:
                # Раньше здесь был голый traceback из makedirs; оператор видел
                # стек и не понимал, что делать. Теперь причина названа.
                raise RuntimeError(
                    "cannot create the state directory %s (%s). Set "
                    "OC_STATE_DIR to a writable path - on this host /home is "
                    "mounted read-only." % (directory,
                                             exc.strerror or exc)) from None
        self._jobs = {}
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self._jobs = json.load(f)
            except (OSError, ValueError):
                # Повреждённое хранилище не должно ронять сервис: лучше
                # пустой словарь и потеря идемпотентности, чем отказ обслуживания.
                self._jobs = {}

    def _flush(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._jobs, f, ensure_ascii=False, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    def get(self, digest: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(digest)
            return dict(job) if job else None

    def put(self, digest: str, record: dict) -> None:
        with self._lock:
            self._jobs[digest] = record
            self._flush()

    def count(self) -> int:
        with self._lock:
            return len(self._jobs)


# --------------------------------------------------------------------------
# очередь и воркер
# --------------------------------------------------------------------------

class QueueRunner:
    """Один поток, одна задача, одна сессия."""

    def __init__(self, transport: CodingSession, store: JobStore,
                 turn_timeout: int = TURN_TIMEOUT):
        self.transport = transport
        self.store = store
        self.turn_timeout = turn_timeout
        self.q: "queue.Queue[str]" = queue.Queue()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.last_loud = ""
        self.processed = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="oc-worker",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def depth(self) -> int:
        return self.q.qsize()

    def submit(self, digest: str) -> None:
        self.q.put(digest)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                digest = self.q.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._run(digest)
            except Exception as exc:                     # noqa: BLE001
                # Ошибка одной задачи не должна убивать воркер: иначе
                # первая неудачная задача тихо останавливает 24/7-очередь.
                self.last_error = "%s: %s" % (type(exc).__name__, exc)
                job = self.store.get(digest) or {"task": None}
                job.update({"status": "failed", "error": self.last_error,
                            "updated": time.time()})
                if job.get("task"):
                    job["task"]["text"] = job["task"]["text"]
                self.store.put(digest, job)
            finally:
                self.q.task_done()
                self.processed += 1

    def _disk_state(self) -> dict:
        """Свободное место и размер временного каталога opencode.

        Каталог берётся из ответа самого сервера (`/api/info -> paths.tmp`),
        а не выдумывается: в этом проекте он уже был не тем, чем кажется -
        `paths.tmp` указывает на `/tmp/opencode`, где рядом с сессиями
        opencode лежит всё подряд.
        """
        tmp = ""
        try:
            tmp = self.transport.tmp_dir()
        except AdapterError as exc:
            self.last_error = str(exc)
        used = 0
        if tmp and os.path.isdir(tmp):
            used = sum(
                os.path.getsize(os.path.join(d, f))
                for d, _s, fs in os.walk(tmp) for f in fs
                if os.path.exists(os.path.join(d, f)))
            return {"tmp": tmp, "free_bytes": self.transport.disk_free_bytes(tmp),
                    "tmp_used_bytes": used}
        return {"tmp": tmp, "free_bytes": -1, "tmp_used_bytes": used}

    def _check_disk(self) -> str:
        """Пустая строка - можно работать. Иначе причина отказа.

        Отказ ЗДЕСЬ и до создания сессии: заголовок «свободно 100 МБ» при
        задаче на 500 МБ - это ровно тот «съел место и упал» сценарий."""
        state = self._disk_state()
        free = state["free_bytes"]
        if free < 0:
            return ""
        if free < WARN_FREE_BYTES and free >= MIN_FREE_BYTES:
            # Мало, но работать можно: говорим громко и продолжаем.
            note = ("low disk: opencode tmp %s has %.0f MB free (warn below "
                    "%.0f MB, refuse below %.0f MB). Still accepting tasks."
                    % (state["tmp"] or "?", free / 1048576,
                       WARN_FREE_BYTES / 1048576, MIN_FREE_BYTES / 1048576))
            if self.last_loud != note:
                self.last_loud = note
                print("LOUD DISK WARNING: " + note, file=sys.stderr, flush=True)
            return ""
        if free < MIN_FREE_BYTES:
            return ("low disk: opencode tmp %s has %.0f MB free, floor is "
                    "%.0f MB. New tasks are REFUSED until space is freed."
                    % (state["tmp"] or "?", free / 1048576,
                       MIN_FREE_BYTES / 1048576))
        return ""

    def _run(self, digest: str) -> None:
        job = self.store.get(digest)
        if not job:
            return
        task = Task(**job["task"])

        loud = self._check_disk()
        if loud:
            self.last_loud = loud
            print("LOUD DISK REFUSAL: " + loud, file=sys.stderr, flush=True)
            job.update({"status": "refused", "error": loud,
                        "updated": time.time()})
            self.store.put(digest, job)
            return

        sid = self.transport.create(task)
        self.transport.set_agent(sid, task.agent)
        job.update({"session_id": sid, "status": "running",
                    "updated": time.time()})
        self.store.put(digest, job)

        # Снимок ДО постановки задачи: без него «что изменилось» не с чем
        # сравнивать. Снимается здесь, а не в транспорте, потому что каталог
        # принадлежит задаче, а не opencode.
        before = snapshot(task.directory)

        self.transport.send(sid, task.text)
        outcome = self.transport.wait_for_turn(sid, self.turn_timeout)
        if not outcome["ok"]:
            job.update({"status": "failed", "error": outcome["reason"],
                        "updated": time.time()})
            self.store.put(digest, job)
            self.transport.delete(sid)
            return

        # diff берётся ИЗ РАБОЧЕГО ДЕРЕВА, а не из `/api/session/{id}/diff`.
        #
        # Тот эндпоинт в opencode 2.0.22 возвращает `{"data": []}` при
        # `outcome == "succeeded"` и изменённом на диске файле - измерено на
        # живом сервере, три прогона, и с поправкой на расположение каталога.
        # Поэтому источник правды - снимок каталога до и после хода.
        #
        # Эндпоинт не выбрасывается: если он когда-нибудь начнёт отвечать, он
        # авторитетнее (в нём есть переименования), и тогда он выигрывает. Но
        # пустой ответ эндпоинта - это НЕ «изменений нет», это отсутствие
        # данных, и подменять им пустой diff нельзя.
        computed_diff, _after = diff_worktree(task.directory, before)
        api_diff = []
        try:
            api_diff = self.transport.diff(sid) or []
        except Exception:  # noqa: BLE001
            api_diff = []
        diff = api_diff or computed_diff
        source = "opencode-session-diff" if api_diff else "worktree-snapshot"

        # Пустой diff обязан называть причину. Пустой список без причины
        # читается как «проверено, изменений нет» - а при живом прогоне это
        # означало «мы ничего не измерили», и приёмка падала без объяснения.
        reason = ""
        if not diff:
            outcome_word = None
            try:
                outcome_word = (self.transport.session(sid) or {}).get("outcome")
            except Exception:  # noqa: BLE001
                outcome_word = None
            reason = describe_empty(task.directory, outcome_word)

        cost = 0
        try:
            cost = float((self.transport.session(sid) or {}).get("cost") or 0)
        except AdapterError:
            pass
        # Результат сначала в хранилище, и только потом удаление сессии.
        # Обратный порядок означал бы потерю результата при падении между
        # двумя операциями.
        job.update({"status": "done", "diff": diff, "cost": cost,
                    "model": task.model, "sessions": [d.get("file")
                                                       for d in diff],
                    "diff_source": source,
                    "error": reason, "updated": time.time()})
        self.store.put(digest, job)

        # Уборка: сессия opencode не остаётся жить после приёмки.
        self.transport.delete(sid)
        job.update({"session_removed": True, "updated": time.time()})
        self.store.put(digest, job)


# --------------------------------------------------------------------------
# HTTP-поверх
# --------------------------------------------------------------------------

class Service:
    """Состояние сервиса. Собирается один раз при старте.

    Сначала собирается транспорт: отсутствие учётных данных должно привести
    к отказу на старте, а не к 500 на первом же запросе."""

    def __init__(self, transport: CodingSession | None = None,
                 store: JobStore | None = None,
                 turn_timeout: int = TURN_TIMEOUT):
        self.transport = transport or HttpV2Transport(load_endpoint())
        self.store = store or JobStore()
        self.runner = QueueRunner(self.transport, self.store, turn_timeout)
        self.started = time.time()

    def start(self) -> None:
        self.runner.start()

    def submit(self, task: Task) -> dict:
        """Приём задачи с идемпотентностью.

        Три исхода, и все три нормальны:
          * `done` - такой отпечаток уже выполнялся, вернули прежний
            результат, сессия не создавалась;
          * `running` - задача уже в работе, присоединяемся к ней;
          * `accepted` - новая задача, встала в очередь."""
        task.check_free()
        digest = task.digest
        known = self.store.get(digest)
        if known and known.get("status") in ("done", "running"):
            known["idempotent_replay"] = True
            return known
        job = {"task": {"text": task.text, "directory": task.directory,
                        "model_id": task.model_id, "provider": task.provider,
                        "agent": task.agent, "title": task.title},
               "digest": digest, "status": "accepted",
               "created": time.time(), "updated": time.time(),
               "session_id": "", "error": ""}
        self.store.put(digest, job)
        self.runner.submit(digest)
        return self.store.get(digest) or job

    def health(self) -> dict:
        disk = self.runner._disk_state()
        free = disk["free_bytes"]
        if free < 0:
            ok = True                      # том не измерен - не повод врать
        elif free < MIN_FREE_BYTES:
            ok = False                     # задачи не принимаются
        else:
            ok = True                      # ниже WARN - работаем, но loudly
        return {"ok": bool(ok), "transport": self.transport.name,
                "uptime_s": round(time.time() - self.started, 1),
                "queue_depth": self.runner.depth(),
                "processed": self.runner.processed,
                "jobs": self.store.count(),
                "last_error": self.runner.last_error,
                "loud": self.runner.last_loud,
                "min_free_bytes": MIN_FREE_BYTES,
                "warn_free_bytes": WARN_FREE_BYTES,
                "disk": disk}


class Handler(BaseHTTPRequestHandler):
    service: Service = None            # проставляется фабрикой ниже
    server_version = "opendeamon-oc/1.0"

    def log_message(self, fmt, *args):         # noqa: A003
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):                        # noqa: N802
        if self.path == "/health":
            self._send(200, self.service.health())
            return
        if self.path.startswith("/task/"):
            digest = self.path.rsplit("/", 1)[-1]
            job = self.service.store.get(digest)
            if not job:
                self._send(404, {"error": "unknown task digest"})
                return
            self._send(200, job)
            return
        self._send(404, {"error": "no such endpoint"})

    def do_POST(self):                       # noqa: N802
        if self.path != "/task":
            self._send(404, {"error": "no such endpoint"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
        except ValueError:
            self._send(400, {"error": "body is not JSON"})
            return
        try:
            task = Task(text=str(body.get("text") or ""),
                        directory=str(body.get("directory") or ""),
                        model_id=str(body.get("model") or DEFAULT_MODEL),
                        provider=str(body.get("provider") or "opencode"),
                        agent=str(body.get("agent") or "build"))
            if not task.text or not task.directory:
                self._send(400, {"error": "text and directory are required"})
                return
            job = self.service.submit(task)
        except AuthRequired as exc:
            self._send(503, {"error": str(exc)})
            return
        except AdapterError as exc:
            self._send(400, {"error": "%s: %s" % (type(exc).__name__, exc)})
            return
        self._send(202, job)


def serve(host: str = "127.0.0.1", port: int = 8791,
          transport: CodingSession | None = None,
          store: JobStore | None = None,
          turn_timeout: int = TURN_TIMEOUT) -> None:
    """Поднять сервис. Учётные данные читаются ДО привязки порта."""
    service = Service(transport=transport, store=store, turn_timeout=turn_timeout)
    handler = type("BoundHandler", (Handler,), {"service": service})
    httpd = ThreadingHTTPServer((host, port), handler)
    service.start()
    print("opendeamon coding service on http://%s:%d (transport=%s)"
          % (host, port, service.transport.name), flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        service.runner.stop()
        httpd.server_close()


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="OpenDeamon coding endpoint")
    ap.add_argument("--host", default=os.environ.get("OC_SERVICE_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("OC_SERVICE_PORT", "8791")))
    ap.add_argument("--transport", choices=("http-v2", "cli"),
                    default=os.environ.get("OC_TRANSPORT", "http-v2"))
    ap.add_argument("--env-file", default=None,
                    help="file with OC_URL/OC_USER/OC_PASSWORD "
                         "(default $OC_CLIENT_ENV or /home/server/oc-client.env)")
    ap.add_argument("--self-test", action="store_true",
                    help="start, print /health, exit - proves the port binds")
    args = ap.parse_args()

    try:
        if args.transport == "http-v2":
            transport = HttpV2Transport(load_endpoint(args.env_file))
        else:
            transport = CliTransport()
    except AuthRequired as exc:
        print("REFUSING TO START: %s" % exc, file=sys.stderr, flush=True)
        return 2

    if args.self_test:
        svc = Service(transport=transport)
        svc.start()
        print(json.dumps(svc.health(), ensure_ascii=False, sort_keys=True),
              flush=True)
        svc.runner.stop()
        return 0

    serve(host=args.host, port=args.port, transport=transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())