"""Поиск: один интерфейс, два провайдера.

ДЫРА, которую закрывает этот модуль. Поиск был описан в репозитории только
через `searxng/docker-compose.yml`. Это описание работает ровно на одной
машине - на той, где есть docker. На сервере docker нет, поэтому поиск был
не настроен вовсе, а интерфейса, к которому можно обратиться, не
существовало: ни `Search()`, ни чего-либо, что можно вызвать. Что делать
воркеру, когда нужен поиск, определялось на месте, в зависимости от того,
что оказалось под рукой.

Теперь выбор не на месте, а здесь:

    Search("что-нибудь")            # сам выбирает провайдера
    Search("что-нибудь", provider="searxng")   # явно

Провайдеров два, и оба возвращают один и тот же тип `Result`:

  * SearxngProvider - локальный SearXNG на 127.0.0.1:8888. Предпочитается,
    когда он отвечает: один запрос к нему обходит десяток поисковиков сразу.
  * KeylessProvider - внешние API, которым не нужен ключ. Работает, когда
    локального SearXNG нет, и это не запасной аварийный путь, а полноценная
    реализация: википедия отдаёт настоящий поиск по статьям, DuckDuckGo -
    краткий ответ и смежные темы.

ВЫБОР ИДЁТ ПО НАЛИЧИЮ ЭНДПОИНТА, а не по флажку в конфиге. Флажок в конфиге
разъезжается с реальностью: `search_backend: searxng` в конфиге при выключенном
SearXNG означает не «работает», а «упало бы, если бы кто-нибудь спросил».
Провайдер проверяется запросом по-настоящему, и только потом им пользуются.

ЧТО НЕ ДЕЛАЕТ МОДУЛЬ

Ничего секретного. Оба провайдера работают без ключей и без пароля, поэтому
секретов в коде нет вовсе, а конфигурировать их нечем - это выбрано
осознанно: ключ в конфиге рано или поздно оказывается в git, ровно как
секрет SearXNG уже оказался (см. searxng/searxng/settings.yml).

Запуск:
    python functions/search/search.py "hermes opencode"
    python functions/search/search.py --provider keyless "python 3.13"
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, asdict

# Эндпоинты берутся из окружения, а не из кода: адрес локального SearXNG у
# двух машин разный, и молчащий зашитый адрес - это тот же класс дыры, что
# был с secret_key.
SEARXNG_URL = os.environ.get("HERMES_SEARXNG_URL",
                             "http://127.0.0.1:8888").rstrip("/")
WIKIPEDIA_URL = os.environ.get("HERMES_WIKIPEDIA_URL",
                               "https://ru.wikipedia.org/w/api.php")
DUCKDUCKGO_URL = os.environ.get("HERMES_DUCKDUCKGO_URL",
                                "https://api.duckduckgo.com/")

# Пользовательский агент. Некоторые википедии и поисковики отвечают пустотой на
# агент по умолчанию, и «ничего не нашлось» выглядит как правдивый ответ.
UA = os.environ.get("HERMES_SEARCH_UA", "Hermes/1.0 (local search)")

DEFAULT_LIMIT = 10
DEFAULT_TIMEOUT = float(os.environ.get("HERMES_SEARCH_TIMEOUT", "20"))


@dataclass
class Result:
    """Один результат поиска.

    Поля - те, что есть у обоих провайдеров. Внешние API отдают разное, и
    приводить их к общему виду здесь, а не в вызывающем коде, - ровно ради
    этого и нужен интерфейс.
    """
    title: str
    url: str
    content: str = ""
    engine: str = ""
    score: float = 0.0
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return asdict(self)


class ProviderError(RuntimeError):
    """Провайдер ответил не тем, чем должен.

    Отдельный тип, потому что «поиск не дал результатов» и «поиск не смог
    ответить» - разные вещи, и склеивать их нельзя: первое честно возвращает
    пустой список, второе обязано поднимать исключение, иначе вызывающий
    решит, что он ничего не нашёл, и пойдёт дальше строить неверный вывод.
    """


def _get(url: str, params: dict | None = None, timeout: float | None = None):
    """GET с понятной ошибкой вместо исключения urllib.

    Таймаут и сетевой сбой не должны выглядеть как пустой результат
    поиска."""
    if params:
        url = "%s?%s" % (url, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout or DEFAULT_TIMEOUT) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raise ProviderError("%s answered HTTP %s" % (url, exc.code)) from exc
    except Exception as exc:  # noqa: BLE001
        raise ProviderError("%s is unreachable: %s: %s"
                            % (url, type(exc).__name__, exc)) from exc


# --------------------------------------------------------------------------
# провайдер 1: локальный SearXNG
# --------------------------------------------------------------------------

class SearxngProvider:
    """Локальный SearXNG. Один запрос - десяток поисковиков.

    Порт и адрес - из окружения (см. `SEARXNG_URL`). Значение по умолчанию
    заведомо петлевое: 127.0.0.1, не 0.0.0.0, потому что поисковик, доступный
    извне без аутентификации, - это не удобство, а дыра.
    """

    name = "searxng"

    def __init__(self, url: str | None = None, timeout: float | None = None):
        self.url = (url or SEARXNG_URL).rstrip("/")
        self.timeout = timeout

    def available(self) -> bool:
        """Отвечает ли эндпоинт на самом деле.

        Проверка настоящим запросом, а не ping: порт может быть занят кем-то
        другим, а `/search` - отдавать HTML вместо JSON. Именно такая разница
        между «порт открыт» и «поиск работает» стоила бы отказа в отладке."""
        try:
            self.search("test", limit=1)
            return True
        except Exception:  # noqa: BLE001
            return False

    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[Result]:
        raw = _get("%s/search" % self.url,
                   {"q": query, "format": "json"}, self.timeout)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ProviderError(
                "%s did not answer JSON - is the searxng instance serving its "
                "json format?" % self.url) from exc
        if not isinstance(data, dict) or "results" not in data:
            raise ProviderError("%s answered JSON without a 'results' field"
                                % self.url)
        out = []
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            out.append(Result(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                content=str(item.get("content") or "").strip(),
                engine=str(item.get("engine") or item.get("engines") or ""),
                score=float(item.get("score") or 0.0),
                extra={"parsed_url": item.get("parsed_url") or []},
            ))
        return out[:limit]


# --------------------------------------------------------------------------
# провайдер 2: внешние API без ключа
# --------------------------------------------------------------------------

class KeylessProvider:
    """Внешний поиск без ключа и без пароля.

    Два источника, оба без аутентификации:

    * википедия - `list=search`, честный поиск по статьям: заголовок, сниппет,
      адрес. Отвечает почти всегда.
    * DuckDuckGo Instant Answer - краткий ответ и смежные темы. Отвечает не на
      всё и часто пустым `Abstract`, поэтому он дополняет, а не заменяет.

    Источники перебираются по порядку, и результаты сливаются: так «поиск без
    локального SearXNG» остаётся полноценным ответом, а не урезанным.
    """

    name = "keyless"

    def __init__(self, wikipedia: str | None = None,
                 duckduckgo: str | None = None, timeout: float | None = None):
        self.wikipedia = wikipedia or WIKIPEDIA_URL
        self.duckduckgo = duckduckgo or DUCKDUCKGO_URL
        self.timeout = timeout

    def available(self) -> bool:
        try:
            return bool(self._wikipedia("test", 1))
        except Exception:  # noqa: BLE001
            return False

    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[Result]:
        out: list[Result] = []
        errors = []
        for name, fn in (("wikipedia", self._wikipedia),
                         ("duckduckgo", self._duckduckgo)):
            try:
                out.extend(fn(query, limit))
            except Exception as exc:  # noqa: BLE001
                # Один источник молчит - это не причина отказывать: второй
                # ещё может ответить. Отказ целиком будет только если не
                # ответил никто.
                errors.append("%s: %s" % (name, exc))
        if not out and errors:
            raise ProviderError("no keyless source answered (%s)"
                                % "; ".join(errors))
        return out[:limit]

    def _wikipedia(self, query: str, limit: int) -> list[Result]:
        raw = _get(self.wikipedia, {
            "action": "query", "list": "search", "srsearch": query,
            "format": "json", "srlimit": max(1, min(limit, 50)),
            "utf8": "1",
        }, self.timeout)
        data = json.loads(raw)
        rows = ((data or {}).get("query") or {}).get("search") or []
        out = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            if not title:
                continue
            snippet = str(row.get("snippet") or "").replace("<span ", "<")
            out.append(Result(
                title=title,
                url="https://ru.wikipedia.org/wiki/"
                    + urllib.parse.quote(title.replace(" ", "_")),
                content=_strip_tags(snippet),
                engine="wikipedia",
                score=1.0,
                extra={"pageid": row.get("pageid")},
            ))
        return out

    def _duckduckgo(self, query: str, limit: int) -> list[Result]:
        raw = _get(self.duckduckgo, {
            "q": query, "format": "json", "no_html": "1",
            "skip_disambig": "1",
        }, self.timeout)
        data = json.loads(raw)
        out = []
        abstract = str(data.get("AbstractText") or "").strip()
        if abstract and data.get("AbstractURL"):
            out.append(Result(
                title=str(data.get("Heading") or query).strip(),
                url=str(data["AbstractURL"]),
                content=abstract,
                engine="duckduckgo",
                score=2.0,
            ))
        for row in data.get("RelatedTopics") or []:
            if not isinstance(row, dict):
                continue
            # RelatedTopics бывает двух видов: с готовым FirstURL и вложенным
            # списком Topics. Второй пропускаем: разворачивать его - это
            # отдельная работа, а пустые заглушки в выдаче хуже, чем их
            # отсутствие.
            url = row.get("FirstURL")
            text = row.get("Text")
            if not url or not text:
                continue
            parts = str(text).split(" - ", 1)
            out.append(Result(
                title=(parts[0] if parts else str(text)).strip(),
                url=str(url),
                content=parts[1].strip() if len(parts) > 1 else "",
                engine="duckduckgo",
                score=0.5,
            ))
        return out[:limit]


def _strip_tags(text: str) -> str:
    """Убрать html-метки из сниппета википедии."""
    import re
    return re.sub(r"<[^>]+>", "", text or "").strip()


# --------------------------------------------------------------------------
# выбор провайдера и сам интерфейс
# --------------------------------------------------------------------------

PROVIDERS = {
    "searxng": SearxngProvider,
    "keyless": KeylessProvider,
}

# Порядок предпочтения. SearXNG - локальный, он не отправляет запросы
# наружу и не зависит от чужого аптайма; keyless - выход, когда его нет.
PREFERENCE = ("searxng", "keyless")


def probe(name: str, options: dict | None = None):
    """Провайдер, если он действительно работает, иначе None.

    Проверка настоящим поисковым запросом. Это дороже, чем проверка порта,
    и намеренно: «порт открыт» не означает «поиск отвечает», а означает лишь
    «кто-то слушает».

    `options` - настройки ЭТОГО провайдера, а не общие для всех: у SearXNG
    один адрес, у keyless их два. Общий словарь разошёлся бы на первой же
    попытке настроить оба, поэтому каждый получает своё."""
    try:
        provider = PROVIDERS[name](**(options or {}))
    except Exception:  # noqa: BLE001
        return None
    return provider if provider.available() else None


def resolve_provider(preferred: str | None = None, config: dict | None = None):
    """Выбрать провайдера по наличию эндпоинта.

    `preferred` - имя из конфигурации (`search_backend`). Это пожелание, а не
    приказ: если провайдер из конфигурации не отвечает, берётся следующий
    рабочий, и вызывающий узнаёт, кто реально ответил. Конфиг, который
    заставляет падать вместо того, чтобы работать, - это не настройка.

    `config` - адреса провайдеров по именам, для проверки и для вызова:
    `{"searxng": {"url": ...}, "keyless": {"wikipedia": ..., ...}}`. Пусто -
    берутся адреса из окружения."""
    order = list(PREFERENCE)
    if preferred and preferred in PROVIDERS:
        order.remove(preferred)
        order.insert(0, preferred)
    config = config or {}
    tried = []
    for name in order:
        provider = probe(name, config.get(name))
        if provider is not None:
            return provider
        tried.append(name)
    raise ProviderError(
        "no search provider answered (tried: %s) - neither a local SearXNG at "
        "%s nor a keyless API is reachable" % (", ".join(tried), SEARXNG_URL))


def Search(query: str, limit: int = DEFAULT_LIMIT, provider: str | None = None,
           config: dict | None = None) -> list[Result]:
    """Единственная точка входа. `Search(query) -> results`.

    `provider` - имя провайдера из конфигурации (`search_backend`). Если он не
    отвечает, берётся следующий рабочий; если не отвечает никто, поднимается
    `ProviderError`, а не возвращается пустой список: пустой список означал бы
    «искали и не нашли», а здесь искать было некому."""
    if not str(query or "").strip():
        raise ValueError("Search() needs a query; an empty query is not a "
                         "search that found nothing")
    chosen = resolve_provider(provider, config)
    return chosen.search(query, limit=limit)


def _human(results: list[Result], chosen: str) -> str:
    """Человеческий вывод. Счётчик - обязателен.

    Пустой вывод без числа рядом читается как «всё проверили, ничего не
    нашлось», а это разные вещи: ноль ответов от провайдера и ноль попыток
    спросить."""
    lines = ["provider: %s" % chosen,
             "results: %d" % len(results)]
    for i, r in enumerate(results, 1):
        lines.append("  %d. [%s] %s" % (i, r.engine or "-", r.title or "(untitled)"))
        lines.append("     %s" % r.url)
        if r.content:
            lines.append("     %s" % r.content[:160])
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Search through whichever "
                                             "provider answers.")
    ap.add_argument("query")
    ap.add_argument("--provider", choices=sorted(PROVIDERS), default=None,
                    help="a wish, not an order: an unresponsive provider "
                         "gives way to a working one")
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    chosen = resolve_provider(args.provider)
    results = chosen.search(args.query, limit=args.limit)
    if args.json:
        print(json.dumps({"provider": chosen.name,
                          "query": args.query,
                          "results": [r.as_dict() for r in results]},
                         ensure_ascii=False, indent=2))
    else:
        print(_human(results, chosen.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
