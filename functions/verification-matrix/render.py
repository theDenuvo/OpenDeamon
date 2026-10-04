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
    add("| пункт | машина | чем закрыт |")
    add("|---|---|---|")
    for it in m["items"]:
        machine = MACHINE_RU.get(it["machine"], it["machine"])
        proof = it.get("proof")
        if it.get("owner_required"):
            closed = "**требует владельца** — " + it["reason"]
        elif not proof:
            # Дыра рендерится, а не роняет рендерер: показать пробел должен
            # тот, кто читает таблицу, иначе о нём узнают только из стектрейса.
            closed = "**ЧЕМ НЕ ЗАКРЯТ** — дыра в матрице"
        else:
            closed = "`%s`" % proof
            if it.get("note"):
                closed += " (" + it["note"] + ")"
        add("| %s | %s | %s |" % (it["item"], machine, closed))
    add("")

    owner = [it for it in m["items"] if it.get("owner_required")]
    add("### Пункты без автоматической проверки — их решал владелец")
    add("")
    if owner:
        add("Расхождение «пункт есть, а чем закрыть нечем» допускается ровно")
        add("в этой форме: пункт назван и объяснён, а не спрятан.")
        add("")
        for it in owner:
            add("- **%s** — %s" % (it["item"], it["reason"]))
    else:
        add("Пусто: все пункты закрыты проверкой.")
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
    add("Пунктов листа: %d, из них требуют владельца: %d, закрыто проверкой: %d."
        % (len(m["items"]), len(owner), len(m["items"]) - len(owner)))
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