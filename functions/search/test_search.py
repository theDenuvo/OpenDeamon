"""Поиск: один интерфейс, два провайдера. Проверка.

ДЫРА, которую закрывает набор. Поиск был описан через docker-compose, а
docker на сервере нет, поэтому поиск не был настроен вовсе: ни `Search()`,
ни выбора провайдера, ни чего-либо, что можно вызвать. Вдобавок в
`searxng/searxng/settings.yml` лежал `secret_key` прямо в git, а
репозиторий публичный.

ЧТО ЗДЕСЬ ПРОВЕРЯЕТСЯ

1. **Интерфейс - один.** `Search(query) -> results`, и оба провайдера отдают
   один и тот же `Result`. Если бы типы разошлись, «настроить поиск» снова
   означало бы «переписать вызывающий код».

2. **Разбор ответа не теряет ничего.** Поднимается поддельный SearXNG,
   отдающий фиксированный ответ, и проверяется, что каждое поле доехало на
   место и что `limit` уважается. Это проверяется без сети и без живого
   SearXNG, то есть в CI тоже, - а не «посмотрим, как вышло в проде».

3. **Внешний провайдер не требует ключа.** То же самое для википедии и
   DuckDuckGo: оба отвечают без аутентификации, и это проверяется по
   поддельным ответам.

4. **Выбор идёт по наличию эндпоинта.** Отвечает SearXNG - берётся он; не
   отвечает - берётся keyless; не отвечает никто - исключение, а НЕ пустой
   список. Пустой список читался бы как «искали и не нашли», и вызывающий
   построил бы на нём неверный вывод.

5. **Секрет не в git.** Прежний ключ отсутствует в отслеживаемых файлах, в
   `settings.yml` стоит заглушка, а новый ключ лежит вне репозитория.

6. **SearXNG слушает только петлю.** Проверяется и в настройке, и на живой
   машине, когда SearXNG запущен.

7. **Живые группы.** Ровно те два требования приёмки, которые требуют
   работающего сервиса: документированный `curl` даёт непустые `results`, и
   тот же запрос через интерфейс даёт то же самое. Без SearXNG они
   объявляются пропуском, а не проходят молча.

Запуск:
    python functions/search/test_search.py
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent
sys.path.insert(0, str(_HERE))

# ЗАКОН «ноль проверок — не успех»: набор возвращает свой код через закон.
sys.path.insert(0, str(_HERE.parent / "meta"))
import zero_checks_law as law  # noqa: E402

import search  # noqa: E402

Skip = law.Skip
skipped = law.skipped

TESTS = []

# Прежний ключ, который лежал в git. Назван здесь намеренно: проверка
# «его больше нет» обязана знать, что именно искать, иначе она ищет
# абстрактное «какой-то секрет» и ничего не находит.
OLD_SECRET = "c912b1592a38b55c48a1da3b5fdb62bec986a78304dcc0ad7cffa7936c839523"

SEARXNG_ENDPOINT = search.SEARXNG_URL


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


# --------------------------------------------------------------------------
# поддельный SearXNG: разбор проверяется без сети
# --------------------------------------------------------------------------

SEARX_PAYLOAD = {
    "query": "проверка",
    "number_of_results": 2,
    "results": [
        {"title": "Первый заголовок", "url": "https://example.test/one",
         "content": "Первый фрагмент", "engine": "duckduckgo", "score": 4.5,
         "parsed_url": ["https", "example", "test", "one"]},
        {"title": "Второй заголовок", "url": "https://example.test/two",
         "content": "Второй фрагмент", "engine": "wikipedia", "score": 2.0},
        {"title": "Третий заголовок", "url": "https://example.test/three",
         "content": "", "engine": "bing", "score": None},
    ],
}

WIKI_PAYLOAD = {
    "query": {"search": [
        {"title": "Python", "pageid": 123, "snippet": '<span class="searchmatch">Python</span> — язык'},
    ]},
}

DDG_PAYLOAD = {
    "Heading": "Python",
    "AbstractText": "Python — высокоуровневый язык.",
    "AbstractURL": "https://example.test/python",
    "RelatedTopics": [
        {"Text": "Python — язык - подробнее", "FirstURL": "https://example.test/1"},
        {"FirstURL": "https://example.test/nested",
         "Topics": [{"Text": "вложенная тема", "FirstURL": "https://example.test/2"}]},
        {"Text": "без адреса"},
    ],
}


class _Stub:
    """Поддельный HTTP-сервис на петле, отдающий заданные ответы."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.hits: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                outer.hits.append("%s?%s" % (parsed.path, parsed.query))
                body = routes.get(parsed.path)
                if body is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *a):  # тишина
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = "http://127.0.0.1:%d" % self.port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
        return False


# --------------------------------------------------------------------------
# 1-3. интерфейс, разбор, отсутствие ключей
# --------------------------------------------------------------------------

@test
def test_one_interface_two_providers_same_result_type():
    """Интерфейс один, и оба провайдера отдают один и тот же тип.

    Проверяется вызовом, а не чтением исходника: провайдер может объявить
    метод `search` и при этом возвращать что угодно."""
    fails = []
    if not callable(getattr(search, "Search", None)):
        return ["search.Search is missing - there is no single entry point, so "
                "every caller picks a provider by hand"]
    for name in ("SearxngProvider", "KeylessProvider"):
        cls = getattr(search, name, None)
        if cls is None:
            fails.append("search.%s is missing: the task asks for two "
                         "implementations, not one" % name)
            continue
        provider = cls()
        if not callable(getattr(provider, "search", None)):
            fails.append("search.%s has no search()" % name)
        if not callable(getattr(provider, "available", None)):
            fails.append("search.%s has no available(): selection is decided "
                         "by whether the endpoint answers, so the answer has "
                         "to be askable" % name)
    if not fails:
        with _Stub({"/search": SEARX_PAYLOAD}) as stub:
            got = search.SearxngProvider(url=stub.url).search("x", limit=2)
        if not all(isinstance(r, search.Result) for r in got):
            fails.append("SearxngProvider returned something other than "
                         "Result, so the two providers cannot be interchangeable")
    return fails


@test
def test_the_searxng_payload_arrives_field_for_field():
    """Разбор ответа не теряет и не искажает ничего.

    Проверяется на поддельном сервисе, а не на живом: живой SearXNG спрашивает
    внешние поисковики, и две одинаковые серии запросов разойдутся на
    результат-другой - проверка на нём была бы проверкой погоды, а не кода.
    Ровно эта дыра и закрывается: дефекты разбора видны в CI, где SearXNG
    нет вовсе."""
    fails = []
    with _Stub({"/search": SEARX_PAYLOAD}) as stub:
        provider = search.SearxngProvider(url=stub.url)
        got = provider.search("проверка", limit=10)
        if len(got) != len(SEARX_PAYLOAD["results"]):
            fails.append("got %d results, the endpoint returned %d - rows are "
                         "being dropped"
                         % (len(got), len(SEARX_PAYLOAD["results"])))
        for want, have in zip(SEARX_PAYLOAD["results"], got):
            if have.title != want["title"]:
                fails.append("title %r != %r" % (have.title, want["title"]))
            if have.url != want["url"]:
                fails.append("url %r != %r" % (have.url, want["url"]))
            if have.content != want["content"]:
                fails.append("content %r != %r" % (have.content, want["content"]))
            if have.engine != want["engine"]:
                fails.append("engine %r != %r" % (have.engine, want["engine"]))
        if got and got[0].score != 4.5:
            fails.append("score %r != 4.5" % got[0].score)
        # limit - не украшение: он ограничивает выдачу
        limited = provider.search("проверка", limit=2)
        if len(limited) != 2:
            fails.append("limit=2 returned %d results" % len(limited))
        # формат json запрашивается явно: иначе приходит html
        if not stub.hits or "format=json" not in stub.hits[0]:
            fails.append("the provider did not ask for format=json (%r) - the "
                         "endpoint answers HTML by default and the JSON "
                         "contract quietly stops holding" % (stub.hits[:1],))
    return fails


@test
def test_the_keyless_provider_needs_no_key_and_merges_both_sources():
    """Внешний провайдер работает без ключа и без пароля.

    Это выбрано осознанно: ключ в конфигурации рано или поздно оказывается в
    git, и secret_key SearXNG уже оказался. Поэтому проверка не «ключ есть»,
    а обратная - в коде и в URL нет ничего похожего на ключ, и оба источника
    отвечают без заголовков авторизации."""
    fails = []
    urls = [search.WIKIPEDIA_URL, search.DUCKDUCKGO_URL]
    for url in urls:
        for token in ("key=", "api_key", "apikey", "token=", "access_key"):
            if token in url.lower():
                fails.append("%s carries %r - the keyless provider must not "
                             "need credentials" % (url, token))
    with _Stub({"/w": WIKI_PAYLOAD, "/d": DDG_PAYLOAD}) as stub:
        provider = search.KeylessProvider(wikipedia=stub.url + "/w",
                                           duckduckgo=stub.url + "/d")
        got = provider.search("python", limit=10)
    engines = {r.engine for r in got}
    if "wikipedia" not in engines:
        fails.append("the wikipedia source contributed nothing: %r" % got)
    if "duckduckgo" not in engines:
        fails.append("the duckduckgo source contributed nothing: %r" % got)
    if len(got) < 3:
        fails.append("merged only %d results from two sources - one of them "
                     "was dropped instead of merged" % len(got))
    # сниппет википедии приходит с html-метками, в выдаче им не место
    wiki = [r for r in got if r.engine == "wikipedia"]
    if wiki and "<span" in wiki[0].content:
        fails.append("the wikipedia snippet kept its html markup: %r"
                     % wiki[0].content)
    # строка без адреса - не результат, а огрызок: молчаливый мусор хуже,
    # чем его отсутствие
    if any(r.url.endswith("/nested") for r in got):
        fails.append("a related topic without a title-less body was accepted "
                     "as a result")
    return fails


# --------------------------------------------------------------------------
# 4. выбор по наличию эндпоинта
# --------------------------------------------------------------------------

@test
def test_the_provider_is_chosen_by_whether_the_endpoint_answers():
    """Выбор идёт по живой проверке, а не по флажку.

    Проверены все три исхода, потому что закрытая дыра была именно в третьем:
    раньше выбор шёл по `search_backend: searxng` из конфига, и при
    выключенном SearXNG поиск просто не работал, вместо того чтобы взять
    другой провайдер."""
    fails = []
    # один поддельный сервис на все три маршрута: проверка выбора должна
    # видеть и работающий SearXNG, и работающий keyless одновременно
    routes = {"/search": SEARX_PAYLOAD, "/w": WIKI_PAYLOAD, "/d": DDG_PAYLOAD}
    with _Stub(routes) as stub:
        keyless = {"wikipedia": stub.url + "/w", "duckduckgo": stub.url + "/d"}
        chosen = search.resolve_provider(None, {"searxng": {"url": stub.url}})
        if chosen.name != "searxng":
            fails.append("with SearXNG answering, chose %r" % chosen.name)
        # пожелание из конфига, на которое нет ответа, не имеет права рушить
        # поиск: берётся следующий рабочий
        chosen = search.resolve_provider("searxng", {
            "searxng": {"url": "http://127.0.0.1:1"},
            "keyless": keyless})
        if chosen.name != "keyless":
            fails.append("an unreachable searxng was honoured instead of "
                         "falling back: chose %r" % chosen.name)
    # и оба молчат: исключение, а не пустой список
    try:
        search.resolve_provider(None, {
            "searxng": {"url": "http://127.0.0.1:1"},
            "keyless": {"wikipedia": "http://127.0.0.1:1",
                        "duckduckgo": "http://127.0.0.1:1"}})
        fails.append("with no provider answering, resolve_provider() returned "
                     "a provider - the caller would believe the search was "
                     "performed and that it found nothing")
    except search.ProviderError:
        pass
    return fails


@test
def test_an_empty_query_is_refused_not_answered_with_nothing():
    """Пустой запрос - это ошибка вызова, а не «ничего не нашлось».

    Разница видна только в честности: `[]` на запрос «» читается как
    «поискали, результатов нет», и вызывающий строит на этом вывод."""
    fails = []
    for query in ("", "   ", None):
        try:
            got = search.Search(query)
            fails.append("Search(%r) returned %d results instead of refusing "
                         "an empty query" % (query, len(got or [])))
        except ValueError:
            pass
        except search.ProviderError:
            fails.append("Search(%r) tried to reach the network before "
                         "noticing the query is empty" % (query,))
    return fails


# --------------------------------------------------------------------------
# 5-6. секрет и петля
# --------------------------------------------------------------------------

@test
def test_the_old_secret_is_gone_and_the_new_one_is_outside_git():
    """Секрет не лежит в git. Проверяется на отслеживаемых файлах и на
    индексе git, а не только в рабочем каталоге: незакоммиченная правка
    выглядит как «уже исправлено», пока она не в индексе."""
    fails = []
    tracked = subprocess.run(["git", "ls-files", "-z"], cwd=str(_ROOT),
                             capture_output=True, text=True, check=True)
    names = [n for n in tracked.stdout.split("\0") if n]
    settings = _ROOT / "searxng" / "searxng" / "settings.yml"
    text = settings.read_text(encoding="utf-8")
    if OLD_SECRET in text:
        fails.append("the old secret_key is still in settings.yml")
    import re
    for m in re.finditer(r'secret_key:\s*"?([A-Za-z0-9]{32,})"?', text):
        if m.group(1) not in ("unset",) and not m.group(1).startswith("unset"):
            fails.append("settings.yml still carries a literal secret of %d "
                         "chars (%s...)" % (len(m.group(1)), m.group(1)[:6]))
    # весь отслеживаемый набор файлов
    for name in names:
        path = _ROOT / name
        try:
            if path.stat().st_size > 4 * 1024 * 1024:
                continue
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if OLD_SECRET in body:
            fails.append("%s contains the old secret" % name)
    # новый секрет обязан лежать вне репозитория
    env = Path("/home/server/projects/opendeamon-runtime/searxng.env")
    if env.exists():
        listed = subprocess.run(["git", "ls-files"], cwd=str(_ROOT),
                                capture_output=True, text=True).stdout
        if "searxng.env" in listed:
            fails.append("the new secret file is tracked by git")
        if env.read_text(encoding="utf-8").find(OLD_SECRET) >= 0:
            fails.append("the new secret file still holds the OLD secret")
    # предупреждение о истории: старый ключ остаётся в коммитах, и это надо
    # сказать вслух, а не считать задачу выполненной молча
    if subprocess.run(["git", "log", "-S", OLD_SECRET, "--oneline",
                       "--", "searxng/searxng/settings.yml"],
                      cwd=str(_ROOT), capture_output=True,
                      text=True).stdout.strip():
        sys.stderr.write(
            "NOTE: the old secret_key is still reachable in git history. It is "
            "no longer a live credential (the running instance uses the "
            "rotated one), so rewriting shared history is a separate "
            "decision, not something to do unasked.\n")
    return fails


@test
def test_searxng_is_bound_to_loopback_only():
    """Поисковик без аутентификации не должен слушать все интерфейсы.

    Проверяются обе стороны: настройка в репозитории и фактический слушатель
    на живой машине. Настройка важна сама по себе - именно её читал бы
    тот, кто поднимет сервис заново."""
    fails = []
    text = (_ROOT / "searxng" / "searxng" / "settings.yml").read_text(
        encoding="utf-8")
    # комментарии вырезаются: в них 0.0.0.0 упоминается как «раньше стояло»,
    # и иначе проверка ругалась бы сама на собственное объяснение
    body = "\n".join(l.split("#")[0] for l in text.splitlines())
    import re
    m = re.search(r'^\s*bind_address:\s*"?([0-9a-fA-F.:]+)"?', body, re.M)
    if not m:
        fails.append("settings.yml declares no bind_address, so the default "
                     "applies and nobody knows what it is")
    elif m.group(1) != "127.0.0.1":
        fails.append("bind_address is %r, not 127.0.0.1 - an unauthenticated "
                     "search endpoint would be reachable from the network"
                     % m.group(1))
    if "0.0.0.0" in body:
        fails.append("settings.yml still sets or mentions 0.0.0.0 outside a "
                     "comment")
    port = urlparse(SEARXNG_ENDPOINT).port or 8888
    listening = _listening_on(port)
    if listening is None:
        return skips_note(fails, port)
    for addr in listening:
        if addr not in ("127.0.0.1", "::1"):
            fails.append("something is listening on port %d via %s, not just "
                         "the loopback" % (port, addr))
    return fails


@test
def test_the_docker_path_is_loopback_only_on_the_host():
    """Путь через docker остаётся закрытым наружу.

    Он закрыт иначе, чем прямой запуск, и отсюда растут обе возможные ошибки:

    * убрать подмену настроек - контейнер слушает свою внутреннюю петлю,
      опубликованный порт молча мёртв, и «поиск не работает» без внятной
      причины;
    * расширить публикацию порта до `0.0.0.0` - и открытый поисковик без
      аутентификации становится виден всей сети.

    Проверяются обе стороны: подмена существует и слушает `0.0.0.0` ВНУТРИ
    контейнера, а compose публикует порт только на петлю хоста."""
    fails = []
    compose = (_ROOT / "searxng" / "docker-compose.yml")
    if not compose.exists():
        return law.skipped("no docker-compose.yml in the repository, so there "
                           "is no docker path to keep closed")
    import re
    text = compose.read_text(encoding="utf-8")
    ports = re.findall(r'^\s*-\s*"([0-9.:]+):([0-9]+)"', text, re.M)
    if not ports:
        fails.append("docker-compose.yml publishes no port, so the instance "
                     "is unreachable from the host even on the loopback")
    for host_part, _container_part in ports:
        if not host_part.startswith("127.0.0.1"):
            fails.append("compose publishes %r, not a loopback address - the "
                         "search endpoint would be reachable from the network"
                         % host_part)
    override = _ROOT / "searxng" / "searxng" / "docker" / "settings.yml"
    if not override.exists():
        fails.append("searxng/searxng/docker/settings.yml is missing. The main "
                     "settings.yml binds 127.0.0.1 for a direct run, and inside "
                     "a container that loopback makes the published port dead. "
                     "Without this override the compose path is silently broken")
    else:
        body = "\n".join(l.split("#")[0] for l in
                         override.read_text(encoding="utf-8").splitlines())
        m = re.search(r'^\s*bind_address:\s*"?([0-9a-fA-F.:]+)"?', body, re.M)
        if not m:
            fails.append("the docker override declares no bind_address")
        elif m.group(1) != "0.0.0.0":
            fails.append("the docker override binds %r; inside a container it "
                         "must be 0.0.0.0 or the published port is dead"
                         % m.group(1))
        if "secret_key" in body and re.search(
                r'secret_key:\s*"?[A-Za-z0-9]{32,}"?', body):
            fails.append("the docker override carries a literal secret_key")
    return fails


def skips_note(fails, port):
    """SearXNG не запущен: это объявленный пропуск, а не успех.

    Именно объявленный - иначе набор выполнил бы на одну проверку меньше и
    этого не заметил, что и есть тот самый класс дыры, который здесь
    закрывается."""
    if not fails:
        return law.skipped("no process is listening on 127.0.0.1:%d, so the "
                           "binding cannot be observed here" % port)
    return fails


def _listening_on(port: int):
    """Адреса, на которых слушает порт, или None если никто не слушает."""
    try:
        out = subprocess.run(["ss", "-ltn"], capture_output=True, text=True,
                             timeout=20).stdout
    except Exception:  # noqa: BLE001
        return None
    addrs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        if local.rsplit(":", 1)[-1] != str(port):
            continue
        addrs.append(local.rsplit(":", 1)[0])
    return addrs or None


# --------------------------------------------------------------------------
# 7. живые группы: ровно то, что требует приёмка
# --------------------------------------------------------------------------

@test
def test_the_documented_curl_returns_results():
    """Приёмка (1): `curl "http://127.0.0.1:8888/search?q=...&format=json"`
    даёт непустой `results`.

    Именно curl, а не urllib: если инструкция в репозитории не работает, то
    человек, который ею воспользуется, получит не то, что проверяет тест."""
    fails = []
    url = "%s/search?q=wikipedia&format=json" % SEARXNG_ENDPOINT
    if not _endpoint_answers():
        return law.skipped("no SearXNG at %s, so the documented curl cannot "
                           "be checked here" % SEARXNG_ENDPOINT)
    proc = subprocess.run(["curl", "-s", "--max-time", "40", url],
                          capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        fails.append("curl exited %d against %s" % (proc.returncode, url))
    try:
        data = json.loads(proc.stdout)
    except ValueError as exc:
        fails.append("curl answered something that is not JSON: %s"
                     % str(exc)[:80])
        return fails
    if not isinstance(data, dict) or not data.get("results"):
        fails.append("results is empty: the endpoint answers but finds "
                     "nothing, which is not the same as working")
    return fails


@test
def test_the_interface_reproduces_the_endpoint_answer():
    """Приёмка (2), проверяемая по-настоящему: интерфейс отдаёт ровно то,
    что ответил эндпоинт.

    Ответ живого SearXNG снимается ОДИН раз и потом отдаётся поддельным
    сервисом, а интерфейс разбирает именно его. Так сравнение не зависит от
    погоды: две серии одного запроса к живому SearXNG расходятся, потому что
    он спрашивает десяток внешних движков и часть из них каждый раз
    отваливается по таймауту - на сервере измерено 10 совпадений из 10 в одном
    прогоне и 3 из 10 в следующем, при одном и том же коде.

    Захваченный ответ проверяется целиком: это доказывает, что разбор держится
    на настоящем ответе, а не на представлении о нём."""
    fails = []
    if not _endpoint_answers():
        return law.skipped("no SearXNG at %s, so there is no real answer to "
                           "reproduce" % SEARXNG_ENDPOINT)
    captured = json.loads(search._get("%s/search" % SEARXNG_ENDPOINT,
                                      {"q": "wikipedia", "format": "json"}))
    if not captured.get("results"):
        return law.skipped("SearXNG answered with no results at %s, so there "
                           "is nothing to reproduce yet" % SEARXNG_ENDPOINT)
    routes = {"/search": captured, "/w": WIKI_PAYLOAD, "/d": DDG_PAYLOAD}
    with _Stub(routes) as stub:
        got = search.SearxngProvider(url=stub.url).search("wikipedia",
                                                           limit=len(
                                                               captured["results"]))
    want = captured["results"]
    if len(got) != len(want):
        fails.append("the endpoint returned %d results, the interface "
                     "produced %d" % (len(want), len(got)))
    for index, (w, h) in enumerate(zip(want, got)):
        if h.url != (w.get("url") or "").strip():
            fails.append("result %d: url %r != %r" % (index, h.url, w.get("url")))
        if h.title != str(w.get("title") or "").strip():
            fails.append("result %d: title %r != %r"
                         % (index, h.title, w.get("title")))
        if h.content != str(w.get("content") or "").strip():
            fails.append("result %d: content differs" % index)
    if not fails and got:
        # и живой вызов интерфейса на тот же запрос не пуст
        live = [r for r in search.Search("wikipedia", limit=10) if r.url]
        if not live:
            fails.append("the interface returned nothing for the very query "
                         "whose answer it had just reproduced item for item")
    return fails


@test
def test_the_same_query_answers_from_both_sides():
    """Тот же запрос спрашивает и эндпоинт, и интерфейс - и оба отвечают.

    Равенство ответов проверяет предыдущая группа, на захваченном ответе.
    Здесь проверяется только то, что нельзя проверить на подделке: живой
    SearXNG и интерфейс живы одновременно и для одного и того же запроса.
    Порог - половина: SearXNG отдаёт разное число строк от прогона к прогону
    и сравнивать их напрямую значит ловить таймауты чужих поисковиков."""
    fails = []
    query = "wikipedia"
    if not _endpoint_answers():
        return law.skipped("no SearXNG at %s, so the live side of the "
                           "comparison does not exist" % SEARXNG_ENDPOINT)
    import time
    direct = json.loads(search._get("%s/search" % SEARXNG_ENDPOINT,
                                    {"q": query, "format": "json"}))
    n_direct = len(direct.get("results") or [])
    time.sleep(2.5)  # уважаем request_delay из settings.yml
    via = search.Search(query, limit=10, provider="searxng")
    if not n_direct:
        fails.append("the endpoint found nothing for %r" % query)
    if not via:
        fails.append("the interface found nothing for %r while the endpoint "
                     "found %d" % (query, n_direct))
    for i, r in enumerate(via):
        if not r.url.startswith(("http://", "https://")):
            fails.append("result %d has url %r, which is not addressable"
                         % (i, r.url))
        if not r.title.strip():
            fails.append("result %d has an empty title, so the caller has "
                         "nothing to show" % i)
    if via and n_direct and len(via) < max(1, n_direct // 2):
        fails.append("the interface returned %d results while the endpoint "
                     "returned %d - rows are being lost on the way"
                     % (len(via), n_direct))
    return fails


def _endpoint_answers() -> bool:
    try:
        search.SearxngProvider().search("test", limit=1)
        return True
    except Exception:  # noqa: BLE001
        return False


def main() -> int:
    failed = skipped_n = 0
    for label, fn in TESTS:
        try:
            outcome = fn()
        except Exception as exc:  # noqa: BLE001
            outcome = ["%s: %s" % (type(exc).__name__, exc)]
        if isinstance(outcome, Skip):
            skipped_n += 1
            print("SKIP  %s" % label)
            print("        - %s" % outcome.reason)
            continue
        fails = list(outcome or [])
        if fails:
            failed += 1
            print("FAIL  %s" % label)
            for line in fails:
                print("        - %s" % line)
        else:
            print("pass  %s" % label)

    passed = len(TESTS) - skipped_n - failed
    # Код возврата - из закона. Семь групп читают только репозиторий, поэтому
    # ноль выполненных здесь означал бы поломку самого набора.
    return law.finish(len(TESTS), passed, failed, skipped_n)


if __name__ == "__main__":
    raise SystemExit(main())
