# TODO — диагностика OpenDeamon (2026-09-30)

Формат: **Ошибка** (что наблюдается, с доказательством) → **Предложение**
(что сделать). Код не трогал, только диагностика.

Контекст состояния: квота $0 соблюдена (лимит $1/день, потрачено $0.00;
free-tier 1000/день, использовано 0). Всё ниже — про надёжность, а не про
расходы.

---

## P0 — сломанное enforcement и зависший процесс

### 1. `quota-watch` не срабатывает: `matcher: "*"` — невалидный regex

**Ошибка.** В `hermes-home/config.yaml:78` для quota-watch указан
`matcher: "*"`. Hermes компилирует matcher как regex
(`shell_hooks.py:110`); `"*"` не компилируется, срабатывает fallback
`treating as literal equality` (`shell_hooks.py:113`). Литeral `"*"` не
равен ни одному имени инструмента → хук зарегистрирован и не вызывается
никогда. Подтверждение в `hermes-home/logs/errors.log`:

```
2026-09-30 15:03:26,774 WARNING agent.shell_hooks: shell hook matcher '*'
is invalid (nothing to repeat at position 0) — treating as literal equality
```

**Предложение.** Убрать поле `matcher` у quota-watch: в `_TOOL_EVENTS`
matcher опционален, а отсутствие поля по коду (`shell_hooks.py:118`)
означает «срабатывать на все инструменты» — это и нужно. Если поле
требуется форматом, заменить на валидный regex, покрывающий весь
алфавит (например `terminal|write_file|patch|edit|code_execution|execute_code|delegate_task|web_search|...`),
и обязательно перепроверить логом, что WARNING исчез. Пункт 4 (парсер) —
независимый баг, его тоже надо закрыть, иначе фикс matcher ничего не
даст.

---

### 2. `quota-watch` парсит устаревшую схему `/credits` → все поля `null`

**Ошибка.** `functions/quota-watch/quota_watch.py:100-114` ждёт от
`GET /api/v1/credits` вложенную структуру `data.usage.limit.daily_free_models`.
Живой ответ сейчас плоский:

```json
{"data":{"total_credits":10,"total_usage":0}}
```

Из него `usage = d.get("usage") or {}` → `{}`, поэтому `total_usage`,
`limit_usd`, `free_used`, `free_limit` все `None`. `verdict()`
(`quota_watch.py:140`) при `used is None` возвращает `allow`, то есть даже
после фикса matcher хук пропустит всё. Косвенное подтверждение:
`hermes-home/cache/quota-watch.json` содержит ровно `{"known": true,
"total_usage": null, ...}` — то есть схема не совпала при живом вызове.
Тесты этого не ловят: `test_quota_watch.py` мокает старую форму и зелёный.

Реальные лимиты лежат на другом эндпоинте (проверено живым вызовом):
`GET /api/v1/key` → `limit: 1`, `limit_reset: daily`,
`free_model_daily_requests: {used: 0, limit: 1000, remaining: 1000}`,
`usage_daily/weekly/monthly: 0`.

**Предложение.** Перевести `fetch_credits`/`measure` на `/api/v1/key`
(эндпоинт отдаёт и дневной бакет, и траты — оба порога нужны), удалить
ветку с `daily_free_models`. Тесты переписать на реальную форму ответа
(`/api/v1/key`), добавить кейс «плоская схема не должна молча давать
allow». Стоит зафиксировать в docstring, что оба эндпоинта наблюдались
живьём и схема меняется.

---

### 3. `comfy_gen.py` не забирает готовый результат: два бага, 15 минут впустую

**Ошибка.** Два независимых дефекта в `functions/imggen/comfy_gen.py`:

*a) Строка 193:* `q = f"/view?filename={images[0]}}&subfolder={'subfolder'}"`
— литерал `'subfolder'` вместо значения из ответа. Проверено живьём против
локального ComfyUI:

```
/view?filename=opendeamon_00001_.png&subfolder=           -> 200, 1 833 320 байт
/view?filename=opendeamon_00001_.png&subfolder=subfolder  -> 404
```

*b) Строка 178:* `hist = api(f"/history/{client_id}")` — ComfyUI ключирует
историю по `prompt_id`, а не по `client_id`. Клиент опрашивает
несуществующий ключ 900 секунд и падает с `TimeoutError`.

Симптом, наблюдаемый прямо сейчас: pid 25068 (`comfy_gen.py ... --size
1024x1024 --json`) висит с 18:02:51, CPU 0.5 с; очередь ComfyUI пуста
(`/queue` → `queue_running: []`); картинка `A:\OpenDeamon\generated\
opendeamon_00001_.png` (1.8 МБ) **уже записана диском в 18:05:03** — то
есть генерация отработала успешно, а клиент её не увидел. Тот же
`TimeoutError: no image after 900s` уже зафиксирован в `_setup_tmp/qc.json`.

**Предложение.** Сохранять `prompt_id` из ответа `POST /prompt` (поле
`prompt_id` в JSON-ответе) и опрашивать `/history/{prompt_id}`. Для
`subfolder` брать `img.get("subfolder", "")` и `img.get("type", "output")`
из той же записи истории. После правки — прогнать на 1024×1024 и
убедиться, что клиент завершился за ~2 мин (131 с по логу ComfyUI) и
`qc.json` содержит `"ok": true`. Параллельно имеет смысл добавить в
`generate()` проверку: если очередь пуста, а deadline близко — вернуть
диагностику, а не молчаливый TimeoutError.

---

### 4. Висящий ComfyUI держит 16.9 ГБ RAM и 13.8/16 ГБ VRAM без работы

**Ошибка.** pid 24632 (ComfyUI, старт 18:00:33) занимает 16 951 МБ
WorkingSet при 6 ГБ свободной RAM из 32; GPU 13 789 / 16 311 MiB при
utilization 12%. Клиент от него отвалился (пункт 3), очередь пуста — то
есть сервер держит веса в памяти без единого запроса. Процесс пережил
завершившуюся генерацию и не завершился.

**Предложение.** После фикса пункта 3 — снять pid 25068 и 24632
(`Stop-Process`), проверить, что VRAM вернулся к ~0. Долгосрочно:
ComfyUI запускать лениво (по требованию `comfy_gen.py` при уже
запущенном сервере — либо поднимать и гасить по таймауту простоя), иначе
каждый зависший клиент стоит 17 ГБ RAM.

---

## P1 — потеря данных, рассинхрон, риск для соседних систем

### 5. Git: 206 МБ бинарников в индексе, незакоммиченная логика, три пакета вне репозитория

**Ошибка.** Репозиторий `A:\OpenDeamon` (2 коммита, remote пустой,
`.git` = 101.7 МБ, `in-pack: 0` — всё разжидиком). В индексе:

| файл | размер |
|---|---|
| `tools/ffmpeg/ffmpeg.exe` | 100.5 МБ |
| `tools/ffmpeg/ffprobe.exe` | 100.5 МБ |
| `hermes-home/skills/.hub/index-cache/hermes-index.json` | 47 МБ |
| 4 × `pets/*/spritesheet.webp` | ~7.9 МБ |

Незакоммичено: `hermes-home/SOUL.md` (+61 строка — capability resolution,
recon-before-integration, verify-results и др.), `hermes-home/config.yaml`
(+4 строки — сам quota-watch хук). Вне индекса целиком: `functions/
capability-matrix/`, `functions/imggen/`, `functions/quota-watch/` — то
есть три новых пакета ни разу не были под контролем версий.

**Предложение.** Расширить `.gitignore`: `tools/ffmpeg/`,
`hermes-home/skills/.hub/index-cache/`, `hermes-home/pets/*/spritesheet.webp`,
`hermes-home/cron/executions.db` (runtime, сейчас в индексе). Затем
`git rm --cached` для этих файлов, коммит правок SOUL/config и трёх
пакетов, `git gc --aggressive` (с 404 файлами и 101 МБ упакуется
мгновенно). Отдельно решить, нужен ли remote: сейчас бэкапа нет нигде.

---

### 6. C: — 3.4 ГБ свободно из 110 ГБ

**Ошибка.** На момент диагностики `C:` = 3.38 ГБ free. История проекта
уже содержит один такой инцидент: `functions/imggen/SKILL.md:51` — «Диск
A: кончился (`No space left on device`) — модели Qwen заняли 45 ГБ», и
причина вскрылась только в середине скачивания. C: при этом не входит
в зону ответственности проекта (`bootstrap.ps1` переносит кэши на A:),
но Hermes/opencode/node живут именно на C:. Отдельно: `cache/hf` на A: —
33 ГБ, из них 29 ГБ Qwen-Image-2.1-Uncensored-GGUF; проверил хардлинками
(`fsutil hardlink list`) — файлы те же, что в `A:\ComfyUI\models`,
дублирования дискового пространства нет.

**Предложение.** Провести разбор C: до того, как упрётся ( Temp,
кэши npm/pip, старые логи). Как страховку — внести в SOUL правило уже
есть (`Check the ground truth before you act`); предложить добавить в
`capability-matrix` порог: любая загрузка >5 ГБ обязана начинаться с
проверки свободного места на целевом диске, а не после факта. Плюс
рассмотреть перенос HF-кэша на B: (74 ГБ свободно) — A: при 20.7 ГБ
свободных и растущем ComfyUI.

---

### 7. `hermes-home/memories/MEMORY.md` — битая кодировка

**Ошибка.** Файл 181 байт, одна строка. После `verified reasoning` идёт
`e2 86 92` (валидный UTF-8 `→`), дальше CP1251-мусор вместо текста
(вывод в консоли: `reasoning??'native tools`). Похоже на двойную
перекодировку при записи — файл чинится, но причина (писатель с
неверным кодированием) не устранена, поэтому следующая запись снова
сломается.

**Предложение.** Перезаписать файл в UTF-8 и проверить, каким кодом его
пишет Hermes (`state.db` / механизм memory). Если запись идёт из
Python без `encoding="utf-8"` — задать кодировку явно на стороне
писателя, иначе правка файла будет перезатёрта.

---

### 8. `capability-matrix` обещает несуществующий whisper

**Ошибка.** `functions/capability-matrix/MATRIX.md:25`: «Транскрипт аудио
— **Локально:** `functions/whisper` (faster-whisper-large-v3 в кэше)».
Проверил: в `venvs/img/Lib/site-packages` нет `faster_whisper` (есть
`diffusers 0.40.0`, `torch 2.11.0+cu128`, `transformers 5.17.0`); в
`A:\OpenDeamon\cache\hf\hub` нет ни одного whisper-репозитория. Пакет
`functions/whisper/` — это скачанный из хаба `SKILL.md` (автор Orchestra
Research) + `references/languages.md`, без кода и без модели. Строка
вводит ядро в заблуждение ровно так, как сам MATRIX.md предостерегает в
верхнем разделе («не изобретало то, что уже есть» — здесь наоборот, вера в
то, чего нет).

**Предложение.** Либо развернуть faster-whisper (модель ~3 ГБ на
`venvs/img`, GPU есть — по правилу «креативное/медиа → локально
выигрывает»), либо до тех пор заменить строку в MATRIX.md на честную
(«не развёрнуто: нужен faster-whisper + веса ~3 ГБ; сейчас — хаб-скилл
без реализации») и перенести из секции «Локально» в «Чего в таблице нет».
Выбор за владельцем проекта; сам MATRIX.md в разделе «Обновление»
требует, чтобы таблица и SKILL.md говорили одно и то же.

---

## P2 — рассинхрон конфигурации (не сломан, но вводит в заблуждение)

### 9. `free-rank/ranking.json` помечает vision-победителем модель, которой в конфиге нет

**Ошибка.** `functions/free-rank/ranking.json` (29.09 17:29) выдаёт
`winners.vision = nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free`.
Живой `hermes-home/config.yaml:14-21` идёт `dots-3-note-preview:free` →
`qwen3.8-27b:free` → `gemma-4-31b-it:free`. Это задокументированный
сценарий (DEPLOYMENT_REPORT §34: nano-omni даёт 404 на реальном vision-пути,
исключён через `VISION_DENY` в `rank.py:36`, действующий конфиг —
ручная правка пользователя, сохранённая гистерезисом), то есть **не баг**.

**Предложение.** Либо синхронизировать артефакт (записать в ranking.json
текущий эффективный vision-цепочки), либо явно пометить поле
`winners` как «победитель по score», а не «применённая конфигурация», и
добавить рядом `applied: {...}` из фактического config.yaml. Сейчас файл
читается как «так настроено», а настроено иначе.

---

### 10. `imggen` — два конкурирующих пути, документация устарела

**Ошибка.** В `functions/imggen/` два генератора: `imggen.py` (diffusers,
SD 1.5, `MODEL_CHAINS = {sd15, sd14}`) и `comfy_gen.py` (ComfyUI +
Qwen-Image-2.1 fp8). `SKILL.md` документирует только первый — и с
формулировкой «ComfyUI-обвязка нужна, если качество станет критичным»,
хотя ComfyUI уже стоит (`A:\ComfyUI`, v0.3.38, запущен) и именно он
даёт приемлемое качество. Пользователь забраковал SD 1.5 как «пластилин»
(SKILL.md:27), и вывод SKILL.md:47 «на 16 ГБ Qwen не поднять» уже неверен
— поднят, через ComfyUI. Метаданные `comfy_gen.py:13-14` тоже говорят
про GGUF Q5_K_M, тогда как фактически грузится `qwen-image-2.1-fp8.safetensors`
(ComfyUI-модель, хардлинк на HF-файл `qwen-image-2.1-UC-fp8.safetensors`).

**Предложение.** Обновить `functions/imggen/SKILL.md`: основной путь —
`comfy_gen.py` + ComfyUI (с указанием, что сервер надо поднимать), SD 1.5
через `imggen.py` — как запасной. Поправить модель в описании
`comfy_gen.py` на фактическую. Это правка документации, не кода.

---

## Проверено и признано исправным (чтобы не переделывать)

- `functions/delegate-first/pre_tool_budget.py` — живой хук, `matcher`
  валидный, регистрация в логе подтверждена; прогнал
  `test_delegate_first.py`: 14 policy-кейсов + per-turn budget + fail-open
  проходят. (Баг `NameError: name 're' is not defined` из лога 23:08
  **уже исправлен** — импорт `re` на месте, строка 13.)
- `functions/quota-watch/test_quota_watch.py` — 7 порогов + fail-open
  проходят. Проходят на моках старой схемы, поэтому не ловят пункт 2.
- Автостарт: `OpenDeamonHands.lnk` в Startup + guard в `bootstrap.ps1:39-50`
  (поднимает `hands.py` скрыто, если :9131 закрыт). Bridge жив (pid 21120,
  слушает 127.0.0.1:9131).
- `free-rank` в Task Scheduler: `OpenDeamonFreeRank`, Пн 04:00,
  `rank.py --weekly --apply`, состояние Ready.
- Ключи не размножаются: `bootstrap.ps1` читает
  `A:\AI\daemon\config\secrets.local.toml` в процесс, на диск не пишет;
  `hermes-home/.env` содержит только `SEARXNG_URL`; `.gitignore` закрывает
  `*.env`, `secrets/`, `hermes-home/auth.json`. (При чтении ключей для
  диагностики пользовался переменной, в отчёт значения не попали.)
- LiteLLM / OpenWebUI / Postgres остановлены осознанно (DEPLOYMENT_REPORT
  §32), конфиги оставлены для отката — docker daemon сейчас не запущен,
  это ожидаемо.
- Квота: $1/день, потрачено $0.00; free-tier 1000/день, использовано 0.