"""Рендер матрицы «что чем проверяется» в Markdown.

Матрица живёт в `matrix.json`, этот файл её превращает в читаемую таблицу, а
`test_verification_matrix.py` проверяет, что таблица не разошлась с данными и
с репозиторием. Смысл такой: матрица, которую не проверяют, через месяц
становится ровно той памятью планировщика, ради которой её делали.
"""
from __future__ import annotations

import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
MATRIX = os.path.join(HERE, "matrix.json")
OUT = os.path.join(HERE, "VERIFICATION.md")

MACHINE_RU = {
    "server": "сервер",
    "windows": "Windows",
    "gpu": "GPU + веса",
    "live-provider": "живой провайдер",
    "owner": "владелец",
}


STATUS_RU = {
    "verified": "проверено",
    "open": "НЕ РЕАЛИЗОВАНО",
    "owner": "решает владелец",
}


def load() -> dict:
    with open(MATRIX, encoding="utf-8") as f:
        return json.load(f)


def render(matrix: dict | None = None) -> str:
    m = matrix or load()
    out = []
    add = out.append

    add("# VERIFICATION — что чем проверяется: сервер против Windows")
    add("")
    add("Сгенерировано из `matrix.json` через `render.py`. Правьте данные, не")
    add("этот файл: `test_verification_matrix.py` требует, чтобы таблица была")
    add("побайтно тем, что рендерит рендерер.")
    add("")
    add("Половина листа годами копила пункты «нужен Windows», и каждый раз это")
    add("выяснялось заново. Ниже - весь проект, разложенный по машинам.")
    add("")

    # --- наборы ---------------------------------------------------------
    add("## Наборы: что доказывает каждый")
    add("")
    add("| набор | машина | что доказывает | чем закрыт |")
    add("|---|---|---|---|")
    for s in m["suites"]:
        add("| `%s` | %s | %s | `%s` |"
            % (s["suite"], MACHINE_RU.get(s["machine"], s["machine"]),
               s["proves"], s["proof"]))
    add("")

    gpu = [s for s in m["suites"] if s["machine"] in ("gpu", "windows")]
    if gpu:
        add("### Что физически нельзя проверить на сервере")
        add("")
        for s in gpu:
            note = s.get("note", "")
            add("- `%s` — %s%s"
                % (s["suite"], MACHINE_RU.get(s["machine"], s["machine"]),
                   (". " + note) if note else ""))
        add("")

    # --- команды --------------------------------------------------------
    add("## Команды: что проверяется только вживую")
    add("")
    add("| команда | машина | что доказывает | почему не набор |")
    add("|---|---|---|---|")
    for c in m["commands"]:
        add("| `%s` | %s | %s | %s |"
            % (c["command"], MACHINE_RU.get(c["machine"], c["machine"]),
               c["proves"], c.get("why_not_a_suite", "")))
    add("")

    # --- пункты листа ---------------------------------------------------
    add("## Пункты листа: чем закрыт и на какой машине")
    add("")
    add("| пункт | состояние | машина | чем закрыт |")
    add("|---|---|---|---|")
    for it in m["items"]:
        machine = MACHINE_RU.get(it["machine"], it["machine"])
        status = it.get("status") or ("owner" if it.get("owner_required")
                                      else "verified")
        closed = it.get("proof")
        if status == "owner":
            closed = "**требует владельца** — " + str(it.get("reason") or "")
        elif status == "open":
            closed = "**чем не закрыт** — " + str(it.get("why_not") or "")
        elif not closed:
            # Дыра рендерится, а не роняет рендерер: показать пробел должен
            # тот, кто читает таблицу, иначе о нём узнают только из стектрейса.
            closed = "**ЧЕМ НЕ ЗАКРЯТ** — дыра в матрице"
        else:
            closed = "`%s`" % closed
            if it.get("note"):
                closed += " (" + it["note"] + ")"
        add("| %s | %s | %s | %s |"
            % (it["item"], STATUS_RU.get(status, status), machine, closed))
    add("")

    owner = [it for it in m["items"] if it.get("status") == "owner"
             or it.get("owner_required")]
    open_items = [it for it in m["items"] if it.get("status") == "open"]
    add("### Что не закрыто и чем это закрывать нечем")
    add("")
    add("Расхождение «пункт есть, а чем закрыть нечем» допускается ровно в")
    add("этой форме: пункт назван и объяснён, а не спрятан. Таких строк")
    add("%d, и они разведены на два разных случая." % (len(owner)
                                                         + len(open_items)))
    add("")
    add("**Решает владелец** — закрыть кодом или прогоном нельзя:")
    add("")
    for it in (owner or [None]):
        if it is None:
            add("- _пусто_")
            continue
        add("- **%s** — %s" % (it["item"], it.get("reason") or it.get("why_not")))
    add("")
    add("**Не реализовано** — задача поставлена, но кода нет, проверять нечем:")
    add("")
    for it in (open_items or [None]):
        if it is None:
            add("- _пусто_")
            continue
        add("- **%s** — %s" % (it["item"], it["why_not"]))
    add("")

    # --- сводка ---------------------------------------------------------
    counts = {}
    for s in m["suites"]:
        counts[s["machine"]] = counts.get(s["machine"], 0) + 1
    add("## Сводка по машинам")
    add("")
    add("| машина | наборов |")
    add("|---|---|")
    for key in ("server", "windows", "gpu", "live-provider"):
        if counts.get(key):
            add("| %s | %d |" % (MACHINE_RU.get(key, key), counts[key]))
    add("")
    verified = [i for i in m["items"] if i.get("status") == "verified"]
    add("Пунктов листа: %d: проверено %d, не реализовано %d, решает владелец %d."
        % (len(m["items"]), len(verified), len(open_items), len(owner)))
    add("")
    add("Разница между «не реализовано» и «решает владелец» существенна: первое")
    add("закрывается кодом и прогоном, второе в принципе не проверяется тестом.")
    add("")
    add("Живая приёмка (настоящие ходы модели, сеть и ключи) в CI не гоняется:")
    add("`python functions/opencode-adapter/acceptance_live.py`,")
    add("`python functions/free-rank/rank.py`, `hermes doctor`, `hermes mcp test`.")
    add("")
    return "\n".join(out)


def main() -> int:
    text = render()
    if "--check" in os.sys.argv:
        current = ""
        if os.path.exists(OUT):
            with open(OUT, encoding="utf-8") as f:
                current = f.read()
        if current != text:
            print("VERIFICATION.md is out of sync with matrix.json. "
                  "Run: python functions/verification-matrix/render.py")
            return 1
        print("VERIFICATION.md matches matrix.json")
        return 0
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(text)
    print("wrote %s (%d bytes)" % (OUT, len(text)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())