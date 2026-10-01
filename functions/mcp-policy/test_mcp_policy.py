"""Фаза 7: MCP-серверы по политике «ноль credentials, ноль денег».

Сама установка одноразовая, а вот решение «какие серверы вообще можно
ставить» — нет. Плажментный или попросту опасный сервер может вернуться
следующей установкой, и никто этого не заметит: он ведь просто появится в
списке.

Поэтому проверяется не «установлено ли», а две вещи, которые должны быть
верными всегда:

  - запрещённое отсутствует: платные подписки и опасные по смыслу
    (платежи, брокеры, реальные сделки);
  - установленное действительно бесплатно: у каждого сервера нет OAuth и нет
    credentials, потому что закон проекта - $0.

Конкретный набор проверен живым `hermes mcp test`: все восемь соединяются,
kiwi отвечает 11-12 с и проходит после 30-секундного таймаута пробы - сервер
медленный на холодном старте, но рабочий.
"""
from __future__ import annotations

import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
CONFIG = os.path.join(_ROOT, "hermes-home", "config.yaml")

# Плажментный план (группа 3) и опасный по смыслу (группа 4). asana - отдельно:
# требует ручной сборки OAuth-кредов, там нет DCR.
FORBIDDEN = {
    "strava": "paid subscription",
    "comfy-cloud": "generation burns credits",
    "grafana": "Assistant Cloud is billed per connection",
    "stripe": "money movement",
    "paypal": "money movement",
    "square": "money movement",
    "plaid": "banking data",
    "robinhood": "can place real trades per its manifest",
    "asana": "needs hand-built OAuth creds, no DCR",
}
# Явно исключены планом из группы 1.
EXCLUDED_BY_PLAN = {
    "alltrails": "5 tools for ~24k tokens of schema",
    "unreal-engine": "local, requires UE 5.8+",
}
EXPECTED = ["context7", "deepwiki", "aws-knowledge", "microsoft-learn",
            "twilio-docs", "wolfram", "kiwi", "trivago"]

TESTS = []



def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


def servers():
    """Читать список MCP-серверов.

    Ключ в config.yaml - `mcp_servers`, а НЕ `mcp`. План писал «блока `mcp`
    нет», и поиск по `mcp` даёт честный и бессмысленный вывод «серверов нет»,
    пока восемь из них настроены. Имя ключа взято из файла, а не из плана.
    """
    import yaml
    data = yaml.safe_load(open(CONFIG, encoding="utf-8")) or {}
    block = data.get("mcp_servers")
    if block is None:
        block = data.get("mcp")
    if isinstance(block, dict):
        block = block.get("servers", block)
    return block if isinstance(block, dict) else {}


@test
def test_the_zero_cost_group_is_configured():
    have = servers()
    if not have:
        return ["no mcp block in config.yaml"]
    missing = [n for n in EXPECTED if n not in have]
    return ([] if not missing
            else ["group 1 servers missing from config: %s" % missing])


@test
def test_forbidden_servers_are_absent():
    have = servers()
    bad = []
    for name, why in FORBIDDEN.items():
        if name in have:
            bad.append("%s is configured (%s)" % (name, why))
    return bad


@test
def test_servers_excluded_by_the_plan_are_absent():
    have = servers()
    return ["%s is configured but the plan excludes it (%s)"
            % (name, why) for name, why in EXCLUDED_BY_PLAN.items()
            if name in have]


@test
def test_every_configured_server_is_enabled_and_credential_free():
    have = servers()
    fails = []
    for name, cfg in have.items():
        if not isinstance(cfg, dict):
            fails.append("%s is not a mapping" % name)
            continue
        if cfg.get("enabled") is False:
            fails.append("%s is configured but disabled - it is dead weight" % name)
        # Креды в конфиге означали бы, что сервер не из группы 1.
        for key in ("oauth", "client_id", "client_secret", "token",
                    "access_token", "api_key"):
            if cfg.get(key):
                fails.append("%s carries %s; group 1 is credential-free" % (name, key))
    return fails


@test
def test_no_filter_silently_disables_a_server():
    """Офлайн проверяется то, что видно из конфига.

    Пустой `include` отключает сервер полностью - это ловится. Исключение
    «всех» через `exclude` офлайн НЕ ловится: не зная полного списка
    инструментов, нельзя отличить «исключён 1 из 5» от «исключены все».
    `aws-knowledge` исключает `aws___retrieve_skill` и оставляет 4 рабочих -
    это осознанно и проверено живым `hermes mcp configure`.

    Пустой exclude - наоборот, признак испорченной записи: сервер выглядит
    настроенным и молчит."""
    have = servers()
    fails = []
    for name, cfg in have.items():
        if not isinstance(cfg, dict):
            continue
        tools = cfg.get("tools")
        if not isinstance(tools, dict):
            continue
        inc = tools.get("include")
        if isinstance(inc, list) and not inc:
            fails.append("%s has an empty include filter - it is configured "
                         "but exposes nothing" % name)
        if isinstance(inc, list) and inc and "all" in inc:
            fails.append("%s mixes 'all' with an explicit include list" % name)
        exc = tools.get("exclude")
        if isinstance(exc, list) and not exc:
            fails.append("%s has an empty exclude filter, which reads as "
                         "'nothing to show'" % name)
    return fails


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
    if failed:
        print("%d/%d groups failed" % (failed, len(TESTS)))
        return 1
    print("all pass (%d groups)" % len(TESTS))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())