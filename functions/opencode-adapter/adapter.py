"""CodingSession: один интерфейс к opencode, два транспорта.

Зачем интерфейс, если «адаптер» можно назвать функцией. Потому что у проекта
два места с разной физикой доступа к opencode:

  * Windows-машина разработчика - opencode живёт как CLI (`opencode run`),
    HTTP-сервера там нет;
  * сервер, круглосуточный - opencode живёт как HTTP v2 (`/api/session`,
    `/api/session/{id}/prompt`), и CLI на сервере не установлен вообще.

Адapter должен быть одна и та же поверхность в обоих местах, иначе очередь,
идемпотентность и приёмка результата пришлось бы писать дважды - и они
разъехались бы ровно так, как разъезжаются все две реализации.

Проверено против живого сервера 2.0.22, а не по документации:

  * `GET /api/info` -> `{"version":"2.0.22", "paths":{"tmp":"/tmp/opencode"}}`;
  * v1 не существует: `/api/info` на v1 отдаёт 404;
  * авторизация обязательна: без учётных данных `/api/info` отдаёт
    `{"_tag":"UnauthorizedError","message":"Authentication required"}`;
  * ответ обёрнут в `{"data": ...}`, а ошибка приходит как
    `{"_tag":"...Error","message":"..."}` - не как код 4xx/5xx;
  * `Model.Ref` = `{providerID, id}`, и `id` - ГОЛЫЙ, без префикса провайдера.
    `providerID=opencode` + `id=opencode/space-bunny-free` даёт маршрут
    `opencode/opencode/space-bunny-free` и `provider.no-route`. Это стоило
    одного запроса и зафиксировано в тесте, чтобы второй раз не искали.

Закон $0 проверяется на входе, а не post factum: допускается только
`opencode/*-free`. Любая другая модель - отказ до создания сессии, потому
что платный вызов уже нельзя отменить.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

# Модель по умолчанию. Замер 2026-09-30 (SCHEME.md): из 8 моделей лестницы
# opencode работали 7, `space-bunny-free` - основная.
DEFAULT_MODEL = "space-bunny-free"
DEFAULT_PROVIDER = "opencode"

# Агент, который реально правит файлы. Без него сессия создаётся, сообщение
# принимается, а хода не происходит: пустая сессия и тишина - худший вид
# «успеха», потому что выглядит как «задача отработана».
DEFAULT_AGENT = "build"

# `delivery: queue` вместо `steer`: steer вмешивается в текущий ход, а нам
# нужна постановка в очередь, чтобы задача выполнилась целиком.
DELIVERY_QUEUE = "queue"

# Единственное допустимое семейство моделей. Закон $0.
FREE_SUFFIX = "-free"
FREE_PROVIDER_PREFIX = "opencode/"


class AdapterError(RuntimeError):
    """Ошибка транспорта. Никогда не проглатывается наружу молча."""


class AuthRequired(AdapterError):
    """Нет учётных данных. Сервис без авторизации не запускается вовсе."""


class ModelNotFree(AdapterError):
    """Модель вне `opencode/*-free`. Закон $0 проверяется до вызова."""


# --------------------------------------------------------------------------
# конфигурация: адрес и пароль только из окружения
# --------------------------------------------------------------------------

# Файл с учётными данными по умолчанию. Значение - путь, а НЕ секрет:
# содержимое читается из окружения, в коде литералов нет и быть не должно.
DEFAULT_ENV_FILE = os.environ.get("OC_CLIENT_ENV", "/home/server/oc-client.env")


class Endpoint:
    """Адрес и учётные данные. Собирается ТОЛЬКО из окружения/файла."""

    __slots__ = ("url", "user", "password")

    def __init__(self, url: str, user: str, password: str):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password

    def __repr__(self) -> str:                 # пароль не должен утекать в лог
        return "Endpoint(url=%r, user=%r, password=<set:%s>)" % (
            self.url, self.user, bool(self.password))


def _read_env_file(path: str) -> dict:
    """Разбор `KEY=value` без сторонних зависимостей.

    Подмножество shell-формата, который используется в `oc-client.env`:
    `KEY=value`, комментарии и пустые строки пропускаются."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                out[key.strip()] = value.strip().strip('"').strip("'")
    except OSError as exc:
        raise AuthRequired("cannot read %s: %s" % (path, exc)) from None
    return out


def load_endpoint(env_file: str | None = None) -> Endpoint:
    """Адрес и пароль из окружения, затем из файла. Никаких значений по умолчанию.

    Порядок именно такой: переменная окружения перекрывает файл, чтобы
    супервизор мог подложить другой адрес без правки файла.
    """
    values = {}
    path = env_file or DEFAULT_ENV_FILE
    if path and os.path.isfile(path):
        values.update(_read_env_file(path))
    for key in ("OC_URL", "OC_USER", "OC_PASSWORD"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    missing = [k for k in ("OC_URL", "OC_USER", "OC_PASSWORD")
               if not values.get(k)]
    if missing:
        raise AuthRequired(
            "no credentials for opencode: %s missing (looked in the "
            "environment and in %s). There are deliberately no defaults: a "
            "service without authorisation must not start."
            % (", ".join(missing), path))
    return Endpoint(values["OC_URL"], values["OC_USER"], values["OC_PASSWORD"])


# --------------------------------------------------------------------------
# задача и её отпечаток
# --------------------------------------------------------------------------

class Task:
    """Задача кодинга. `digest` - ключ идемпотентности.

    Отпечаток берётся не только из текста: та же просьба в том же каталоге
    той же модели - это одна и та же задача, а повтор должен вернуть
    прежний результат, а не создать вторую сессию.
    """

    __slots__ = ("text", "directory", "model_id", "provider", "agent", "title")

    def __init__(self, text: str, directory: str,
                 model_id: str = DEFAULT_MODEL,
                 provider: str = DEFAULT_PROVIDER,
                 agent: str = DEFAULT_AGENT,
                 title: str | None = None):
        self.text = text
        self.directory = os.path.abspath(directory)
        self.model_id = model_id
        self.provider = provider
        self.agent = agent
        self.title = title or ("opendeamon: " + text.strip().splitlines()[0][:60])

    @property
    def model(self) -> str:
        """`provider/model` - форма, в которой модель называется в отчётах."""
        return "%s/%s" % (self.provider, self.model_id)

    @property
    def digest(self) -> str:
        """sha256 от канонического описания задачи.

        Канонизация - через sort_keys и без случайных полей, иначе один и
        тот же заказ давал бы разные отпечатки и идемпотентность была бы
        фикцией.
        """
        payload = json.dumps(
            {"text": self.text, "directory": self.directory,
             "provider": self.provider, "model": self.model_id,
             "agent": self.agent},
            sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def check_free(self) -> None:
        """Закон $0 на входе. До создания сессии, а не после вызова."""
        if self.provider + "/" not in FREE_PROVIDER_PREFIX:
            raise ModelNotFree(
                "provider %r is not %s: the $0 rail allows only opencode"
                % (self.provider, FREE_PROVIDER_PREFIX))
        if not self.model_id.endswith(FREE_SUFFIX):
            raise ModelNotFree(
                "model %r is not a free SKU: only opencode/*%s is allowed"
                % (self.model, FREE_SUFFIX))


# --------------------------------------------------------------------------
# транспорты
# --------------------------------------------------------------------------

class CodingSession:
    """Единая поверхность. Наследники реализуют физику доступа."""

    name = "abstract"

    def models(self) -> list:
        raise NotImplementedError

    def create(self, task: Task) -> str:
        raise NotImplementedError

    def send(self, session_id: str, text: str) -> None:
        raise NotImplementedError

    def diff(self, session_id: str) -> list:
        raise NotImplementedError

    def delete(self, session_id: str) -> bool:
        raise NotImplementedError

    def tmp_dir(self) -> str:
        raise NotImplementedError

    def disk_free_bytes(self, path: str) -> int:
        """Свободное место на томе, где лежит `path`. -1 - измерить нельзя.

        Отдельный метод, а не вызов `shutil` внутри сервиса: порог диска -
        это политика сервиса, а «как мерить» - свойство транспорта. Так его
        можно проверить тестом, не создавая настоящей нехватки места.
        """
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return -1

    def close(self) -> None:
        pass


class HttpV2Transport(CodingSession):
    """Транспорт сервера: opencode как HTTP v2.

    Проверен на 2.0.22. Особенности, которые неочевидны и потому записаны
    здесь, а не оставлены на память:

      * ответ обёрнут в `{"data": ...}`; ошибка - `{"_tag": "...Error"}`
        внутри ответа 200, поэтому HTTP-код недостаточен для проверки
        успеха;
      * `GET /api/session/{id}/message` отдаёт ТИПЫ сообщений с `payload: null`.
        Этого достаточно, чтобы отличить «ход был» от «хода не было», но
        текст ответа так не прочитать - за текстом идут в
        `/api/experimental/session/{id}/log`;
      * падение хода не оставляет сообщения об ошибке, а приходит событием
        `session.execution.failed` в `/api/event`. Поэтому «нет хода
        ассистента за отведённое время» - это провал с честным текстом, а
        не выдуманная причина.
    """

    name = "http-v2"

    def __init__(self, endpoint: Endpoint, timeout: int = 60):
        self.endpoint = endpoint
        self.timeout = timeout

    # -- низкий уровень ---------------------------------------------------
    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: int | None = None) -> object:
        url = self.endpoint.url + path
        data = None
        headers = {"Accept": "application/json"}
        token = base64.b64encode(
            ("%s:%s" % (self.endpoint.user, self.endpoint.password))
            .encode("utf-8")).decode("ascii")
        headers["Authorization"] = "Basic " + token
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                raw = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            raise AdapterError("%s %s -> HTTP %s: %s"
                               % (method, path, exc.code,
                                  exc.read()[:200].decode("utf-8", "replace"))
                               ) from None
        except urllib.error.URLError as exc:
            raise AdapterError("%s %s -> %s" % (method, path, exc.reason)) from None
        if not raw.strip():
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            raise AdapterError("%s %s returned non-JSON: %r"
                               % (method, path, raw[:120])) from None
        # Ошибка приходит внутри 200.
        if isinstance(payload, dict) and payload.get("_tag", "").endswith("Error"):
            raise AdapterError("%s %s -> %s: %s"
                               % (method, path, payload.get("_tag"),
                                  payload.get("message")))
        if isinstance(payload, dict) and "data" in payload:
            return payload["data"]
        return payload

    # -- интерфейс --------------------------------------------------------
    def info(self) -> dict:
        return self._request("GET", "/api/info") or {}

    def models(self) -> list:
        data = self._request("GET", "/api/model") or []
        out = []
        for m in data if isinstance(data, list) else []:
            provider = str(m.get("providerID") or "")
            ident = str(m.get("id") or "")
            out.append({"provider": provider, "id": ident,
                        "ref": "%s/%s" % (provider, ident),
                        "free": provider == DEFAULT_PROVIDER
                        and ident.endswith(FREE_SUFFIX)})
        return out

    def create(self, task: Task) -> str:
        task.check_free()
        if not os.path.isdir(task.directory):
            raise AdapterError("working directory does not exist: %s"
                               % task.directory)
        data = self._request("POST", "/api/session", {
            "title": task.title,
            # id - ГОЛЫЙ. С префиксом провайдера получается
            # opencode/opencode/... и provider.no-route.
            "model": {"providerID": task.provider, "id": task.model_id},
            "location": {"directory": task.directory},
            "agent": task.agent,
            "metadata": {"task_digest": task.digest,
                          "opendeamon": "A2-bis"},
        })
        sid = (data or {}).get("id")
        if not sid:
            raise AdapterError("session created without an id: %r" % (data,))
        return sid

    def set_agent(self, session_id: str, agent: str) -> None:
        self._request("POST", "/api/session/%s/agent" % session_id,
                      {"agent": agent})

    def send(self, session_id: str, text: str) -> None:
        self._request("POST", "/api/session/%s/prompt" % session_id,
                      {"text": text, "delivery": DELIVERY_QUEUE})

    def message_types(self, session_id: str) -> list:
        data = self._request("GET", "/api/session/%s/message" % session_id) or []
        return [str(m.get("type")) for m in data if isinstance(m, dict)]

    def diff(self, session_id: str) -> list:
        data = self._request("GET", "/api/session/%s/diff" % session_id) or []
        return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []

    def session(self, session_id: str) -> dict:
        return self._request("GET", "/api/session/%s" % session_id) or {}

    def delete(self, session_id: str) -> bool:
        self._request("DELETE", "/api/session/%s" % session_id)
        return True

    def tmp_dir(self) -> str:
        return str((self.info().get("paths") or {}).get("tmp") or "")

    # -- ожидание результата ----------------------------------------------
    def wait_for_turn(self, session_id: str, timeout: int = 900,
                      poll: int = 10) -> dict:
        """Дождаться хода ассистента.

        Критерий - появилось сообщение типа `assistant`. Не «diff стал
        непустым»: задача может быть «объясни, ничего не меняя», и тогда
        правильный результат - пустой diff при состоявшемся ходе.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if "assistant" in self.message_types(session_id):
                return {"ok": True, "reason": "assistant turn observed"}
            time.sleep(poll)
        # Честная причина провала: хода не было. Не выдумываем текст ошибки,
        # которого в этом API нет - он приходит событием в /api/event.
        return {"ok": False,
                "reason": "no assistant turn within %ds; opencode reports "
                          "failures on /api/event as session.execution.failed"
                          % timeout}


class CliTransport(CodingSession):
    """Транспорт машины разработчика: opencode как CLI.

    Один запуск - одна задача, поэтому «сессия» здесь синтетическая: её
    идентификатор это отпечаток задачи. Настоящий opencode при этом
    ничего не знает и сессий не плодит, что для CLI-лестницы и требовалось.

    На сервере этот транспорт непригоден: `opencode` в PATH нет. Это
    проверяется, а не предполагается.
    """

    name = "cli"

    def __init__(self, model_id: str = DEFAULT_MODEL, cwd: str | None = None,
                 attach: str | None = None, timeout: int = 1800):
        self.model_id = model_id
        self.cwd = cwd
        self.attach = attach
        self.timeout = timeout

    def available(self) -> bool:
        return shutil.which("opencode") is not None

    def _require(self) -> None:
        if not self.available():
            raise AdapterError("the `opencode` CLI is not in PATH; this "
                               "transport is for the developer machine, the "
                               "server uses HttpV2Transport")

    def models(self) -> list:
        self._require()
        out = subprocess.run(["opencode", "models"], capture_output=True,
                             text=True, timeout=120)
        return [{"provider": DEFAULT_PROVIDER, "id": line.strip(),
                 "ref": "%s/%s" % (DEFAULT_PROVIDER, line.strip()),
                 "free": line.strip().endswith(FREE_SUFFIX)}
                for line in (out.stdout or "").splitlines() if line.strip()]

    def _argv(self, task_text: str) -> list:
        argv = ["opencode", "run", "-m", "%s/%s" % (DEFAULT_PROVIDER,
                                                     self.model_id)]
        if self.attach:
            # Живой сервер вместо локального демона: тот же HTTP v2 изнутри.
            argv += ["--attach", self.attach]
        argv.append(task_text)
        return argv

    def create(self, task: Task) -> str:
        task.check_free()
        return "cli:" + task.digest[:32]

    def send(self, session_id: str, text: str) -> None:
        self._require()
        task = Task(text, self.cwd or os.getcwd(), self.model_id)
        argv = self._argv(text)
        if self.cwd:
            out = subprocess.run(argv, cwd=self.cwd, capture_output=True,
                                 text=True, timeout=self.timeout)
        else:
            out = subprocess.run(argv, capture_output=True, text=True,
                                 timeout=self.timeout)
        if out.returncode != 0:
            raise AdapterError("opencode run failed (%s): %s"
                               % (out.returncode, (out.stderr or "")[:200]))

    def diff(self, session_id: str) -> list:
        # У CLI нет хранилища диффов: результат приходит в stdout задачи.
        return []

    def delete(self, session_id: str) -> bool:
        return True       # сессии не было - убирать нечего

    def tmp_dir(self) -> str:
        return ""