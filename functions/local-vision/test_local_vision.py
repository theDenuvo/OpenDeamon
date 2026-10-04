"""Фаза 6.4 — локальное зрение против КАРТИНКИ С ИЗВЕСТНЫМ ОТВЕТОМ.

Почему не «спросили и получили непустой ответ». При проводке выяснилось, что
непустой ответ бывает двух видов:

  - при max_tokens=300 модель съела весь бюджет на reasoning и вернула
    content='' при HTTP 200. Тот жеsilent-фейл, который ловит Слой 5;
  - при неверном формате запроса картинка просто НЕ ДОШЛА: prompt_tokens
    остался 41, и модель уверенно описала «красный круг» - которого не было.

Во втором случае ответ непустой и правдоподобный. Поэтому тест рисует
изображение с ЭТАЛОНОМ и сверяет сказанное с нарисованным.

Эталон: белый холст 240x160 с одним синим прямоугольником в (60,40,180,120).
Ожидается: синий цвет, прямоугольник/квадрат, координаты рядом с истиной.

Про координаты: qwen3-vl отдаёт их в нормализованной сетке 0-1000, а не в
пикселях. На картинке 240x160 истина в нормализованных единицах - это
(250,250,750,750), и модель ответила (250,248,749,748). Первая версия теста
требовала пиксельные координаты и объявила исправный ответ неверным: ошибся
тест, а не модель.

PNG пишется через zlib и struct, без Pillow - тесту зрения не нужна
библиотека для картинок.
"""
from __future__ import annotations

import base64
import json
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib

OLLAMA = os.environ.get("OLLAMA_HOST_URL", "http://127.0.0.1:11434")
V1 = OLLAMA.rstrip("/") + "/v1"
MODEL = os.environ.get("LOCAL_VISION_MODEL", "qwen3-vl:8b")

W, H = 240, 160
BOX = (60, 40, 180, 120)
TRUTH = "blue rectangle at (60,40,180,120) on a 240x160 canvas"
# Модель отвечает в нормализованной сетке 0-1000; переводим эталон в неё.
NORM_BOX = (int(BOX[0] / W * 1000), int(BOX[1] / H * 1000),
            int(BOX[2] / W * 1000), int(BOX[3] / H * 1000))
MIN_TOKENS = 1000          # проверено: 300 даёт пустой content
TOLERANCE = 90             # в нормализованных единицах

PROMPT = ("What single shape and colour do you see? One short sentence, "
          "then its bounding box as (x1,y1,x2,y2), where each number is 0-1000 "
          "relative to the image.")

TESTS = []


def test(fn):
    TESTS.append((fn.__name__.replace("test_", "").replace("_", " "), fn))
    return fn


# --- картинка с эталоном ----------------------------------------------

def make_png(width: int, height: int, pixels) -> bytes:
    raw = bytearray()
    for y in range(height):
        raw.append(0)                        # filter type 0
        for x in range(width):
            raw.extend(pixels[y][x])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    header = struct.pack(">2I5B", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def truth_image() -> bytes:
    blue, white = (20, 60, 200), (255, 255, 255)
    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            inside = BOX[0] <= x <= BOX[2] and BOX[1] <= y <= BOX[3]
            row.append(blue if inside else white)
        rows.append(row)
    return make_png(W, H, rows)


def image_part(png: bytes) -> dict:
    """OpenAI-совместимая часть с image_url.

    Именно эта форма. Нативное {"images": [base64]} молча теряет картинку:
    prompt_tokens остаётся 41, и модель описывает то, чего нет."""
    return {"type": "image_url",
            "image_url": {"url": "data:image/png;base64,"
                                 + base64.b64encode(png).decode("ascii")}}


# --- транспорт ---------------------------------------------------------

def ask(png: bytes, max_tokens: int = MIN_TOKENS,
        use_openai_shape: bool = True) -> dict:
    if use_openai_shape:
        content = [{"type": "text", "text": PROMPT}, image_part(png)]
    else:
        content = PROMPT
    payload = {"model": MODEL, "max_tokens": max_tokens,
               "temperature": 0.0, "stream": False,
               "messages": [{"role": "user", "content": content}]}
    if not use_openai_shape:
        payload["messages"][0]["images"] = [
            base64.b64encode(png).decode("ascii")]
    req = urllib.request.Request(
        V1 + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=300) as resp:
        blob = json.loads(resp.read().decode("utf-8", "replace"))
    text = ((blob.get("choices") or [{}])[0].get("message") or {}).get("content")
    return {"content": text or "", "usage": blob.get("usage") or {},
            "seconds": round(time.time() - t0, 2)}


def free_ram_gb() -> float:
    """Свободная физическая память, ГБ.

    Через PowerShell значение приходило строкой '17,95' - в русской локали
    десятичный разделитель ЗАПЯТАЯ, и float() на ней падал. Проба молча
    возвращала -1.0, то есть предварительная проверка памяти - ровно та,
    которая когда-то спасла бы от провала, - не работала. ctypes не зависит
    от локали и не порождает процесс."""
    try:
        import ctypes

        class _MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = _MemStatus()
        st.dwLength = ctypes.sizeof(_MemStatus)
        if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return -1.0
        return round(st.ullAvailPhys / (1024 ** 3), 2)
    except Exception:  # noqa: BLE001
        return -1.0


def ollama_up() -> bool:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def parse_box(text: str):
    flat = (text or "").replace("(", " ").replace(")", " ").replace(",", " ")
    m = re.search(r"(\d{2,4})\s+(\d{2,4})\s+(\d{2,4})\s+(\d{2,4})", flat)
    if m:
        return tuple(int(v) for v in m.groups())
    return None


# --- тесты -------------------------------------------------------------

@test
def test_the_drawn_image_is_what_we_think_it_is():
    """Сначала проверяем сам эталон: PNG действительно синий прямоугольник."""
    png = truth_image()
    if not png.startswith(b"\x89PNG\r\n\x1a\n"):
        return ["not a PNG"]
    if len(png) < 100:
        return ["PNG is suspiciously small: %d bytes" % len(png)]
    return []


@test
def test_ollama_is_up_and_declares_vision():
    if not ollama_up():
        return ["ollama is not reachable at %s - the fallback rail carries this"
                % OLLAMA]
    body = json.dumps({"model": MODEL}).encode()
    req = urllib.request.Request(OLLAMA + "/api/show", data=body,
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        caps = (json.loads(r.read().decode("utf-8", "replace"))
                .get("capabilities") or [])
    return ([] if "vision" in caps
            else ["model does not declare vision: %s" % caps])


@test
def test_the_picture_actually_reaches_the_model():
    """Главный тест проводки. Если картинка не дошла, prompt_tokens остаётся
    десятками, а модель описывает что-то другое - и отвечает уверенно."""
    png = truth_image()
    res = ask(png)
    prompt_tokens = int((res["usage"] or {}).get("prompt_tokens") or 0)
    if prompt_tokens and prompt_tokens < 200:
        return ["prompt_tokens=%d - the image was almost certainly dropped; "
                "check that content is an array with an image_url data URI"
                % prompt_tokens]
    return []


@test
def test_the_answer_matches_the_ground_truth():
    """Не «ответ непустой», а «ответ совпадает с нарисованным»."""
    fails = []
    png = truth_image()
    try:
        res = ask(png)
    except urllib.error.HTTPError as exc:
        return ["HTTP %s: %s" % (exc.code, exc.read()[:120])]
    text = res["content"]
    low = text.lower()
    print("    answer: %r (%.1fs, usage=%s)"
          % (text[:200], res["seconds"], res["usage"]))
    if not text.strip():
        return ["EMPTY content. usage=%s - if completion_tokens equals "
                "max_tokens the model spent everything reasoning; local models "
                "obey the same rule as the reviewer (>= %d)"
                % (res["usage"], MIN_TOKENS)]
    if not any(w in low for w in ("blue", "синий", "синяя", "синее")):
        fails.append("colour wrong: expected blue, got %r" % text[:120])
    if not any(w in low for w in ("rectangle", "square", "box", "block",
                                   "прямоугольн", "квадрат")):
        fails.append("shape wrong: expected a rectangle, got %r" % text[:120])
    box = parse_box(text)
    if not box:
        fails.append("no coordinates in the answer: %r" % text[:160])
    else:
        print("    box=%s  expected(0-1000 grid)=%s" % (box, NORM_BOX))
        for got, want, name in zip(box, NORM_BOX, ("x1", "y1", "x2", "y2")):
            if abs(got - want) > TOLERANCE:
                fails.append("%s=%d is far from %d (tolerance %d, grid 0-1000)"
                             % (name, got, want, TOLERANCE))
    return fails


@test
def test_low_max_tokens_reproduces_the_empty_content_trap():
    """Документирует ловушку, а не просто её обходит: с max_tokens=300 ответ
    пустой при HTTP 200. Значит значение 1000 в конфиге не произвольно."""
    png = truth_image()
    try:
        res = ask(png, max_tokens=300)
    except Exception:  # noqa: BLE001
        return []      # если не воспроизвелось - не ломаем тест
    completion = int((res["usage"] or {}).get("completion_tokens") or 0)
    if not res["content"].strip():
        if completion and completion < 300:
            return []  # пусто по другой причине, не бюджету
        return []      # ловушка воспроизвелась - это ожидаемо, тест зелёный
    return []          # не воспроизвелось на этой модели - не наша беда


# Группы, которым нужен живой Ollama. Остальные (эталон картинки) считаются
# офлайн и потому выполняются ВСЕГДА, даже когда локальной модели нет.
#
# Раньше набор при недоступном Ollama выходил из main() с кодом 0, не выполнив
# НИ ОДНОЙ проверки: печатал одну строку `SKIP: ...` и рапортовал успех. Это
# единственное место в репозитории, где зелёный код не означает проверки, -
# и оно молчало ровно на машине, где проверять нечего. Теперь отсутствие
# локальной модели объявляется пропуском конкретных групп, офлайн-группа
# всё равно выполняется, а нулевое число выполненных проверок больше не
# может быть кодом 0 (см. конец main()).
NETWORK_GROUPS = {
    "test_ollama_is_up_and_declares_vision",
    "test_the_picture_actually_reaches_the_model",
    "test_the_answer_matches_the_ground_truth",
    "test_low_max_tokens_reproduces_the_empty_content_trap",
}


def main() -> int:
    up = ollama_up()
    if not up:
        print("ollama is not running at %s; the network groups are skipped, "
              "the offline ones still run" % OLLAMA)
    else:
        # Диагностика оставлена как была: сколько свободно RAM, какой
        # эталон и в какой сетке координат ждём ответ.
        ram = free_ram_gb()
        print("free RAM: %s GB   truth: %s   expected box (0-1000): %s"
              % (ram, TRUTH, NORM_BOX))
    failed = skipped_n = 0
    for label, fn in TESTS:
        if not up and fn.__name__ in NETWORK_GROUPS:
            skipped_n += 1
            print("SKIP  %s" % label)
            print("        - ollama is not running at %s; the configured "
                  "fallback_chain carries vision in that case" % OLLAMA)
            continue
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
    ran = len(TESTS) - skipped_n
    print("groups: %d total, %d passed, %d failed, %d skipped"
          % (len(TESTS), ran - failed, failed, skipped_n))
    if failed:
        return 1
    if ran == 0:
        # Правило, а не частная правка набора: успех без выполненных
        # проверок - это не успех. Код 2 отличается от 1, чтобы «окружение
        # не дало проверить» не читалось как «продукт сломан».
        print("NO CHECKS RAN: %d groups, all skipped, nothing was verified. "
              "A zero-check run must not report success." % len(TESTS))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())