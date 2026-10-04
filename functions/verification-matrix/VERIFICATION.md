# VERIFICATION — что чем проверяется: сервер против Windows

Сгенерировано из `matrix.json` через `render.py`. Правьте данные, не
этот файл: `test_verification_matrix.py` требует, чтобы таблица была
побайтно тем, что рендерит рендерер.

Половина листа годами копила пункты «нужен Windows», и каждый раз это
выяснялось заново. Ниже - весь проект, разложенный по машинам.

## Наборы: что доказывает каждый

| набор | машина | что доказывает | чем закрыт |
|---|---|---|---|
| `functions/specgate/test_spec_gate.py` | сервер | слой 0: потолок VERIFIED на авто-методах и запрет ручного достижения | `functions/specgate/test_spec_gate.py::auto methods reach the VERIFIED ceiling` |
| `functions/baseline/test_baseline.py` | сервер | слой 2: барьер до воркера, отказ на грязном дереве, хэши LEDGER | `functions/baseline/test_baseline.py::barrier is mandatory before the worker (invariant 7)` |
| `functions/workerstate/test_worker_state.py` | сервер | слой 3: таблица переходов, карантин, откат, RECOVERY_BLOCKED, метка WORKER_CLAIM | `functions/workerstate/test_worker_state.py::transition table matches the scheme` |
| `functions/mechanical/test_mechanical.py` | сервер | слой 1: каждая разновидность отказа имеет предписанную классификацию | `functions/mechanical/test_mechanical.py::every failure kind has the prescribed action` |
| `functions/reviewer/test_reviewer.py` | сервер | слой 5: транспорт опрашивается по-настоящему, рецензент читает коммит, а не живое дерево | `functions/reviewer/test_reviewer.py::empty content is refused even with http 200` |
| `functions/verdict/test_verdict.py` | сервер | слой 6: FULL требует diff, PARTIAL не хуже, вердикт не выдаётся без доказательств | `functions/verdict/test_verdict.py::every result carries an action` |
| `functions/verdict/test_seam.py` | сервер | швы между слоями: mechanical и reviewer отдают валидный json | `functions/verdict/test_seam.py::mechanical cli emits valid json` |
| `functions/delegate-first/test_delegate_first.py` | сервер | слой 0: кодовые тулзы заблокированы намеренно, хук fail-open | `functions/delegate-first/test_delegate_first.py::policy cases` |
| `functions/quota-watch/test_quota_watch.py` | сервер | разбор схемы квот OpenRouter и защита от дрейфа формы | `functions/quota-watch/test_quota_watch.py::parse live /api/v1/key schema` |
| `functions/quota-watch/test_moa_free_rail.py` | сервер | интерлок $0 в MoA: ни один платный слот не достижим, пресет выключен | `functions/quota-watch/test_moa_free_rail.py::the preset is disabled so moa cannot fan out` |
| `functions/free-rank/test_vision_gate.py` | сервер | роль зрения: доказанно мёртвые на картинке исключены, eligibility чистая функция | `functions/free-rank/test_vision_gate.py::denied models excluded from vision` |
| `functions/mcp-policy/test_mcp_policy.py` | сервер | MCP-серверы бесплатны и без credentials | `functions/mcp-policy/test_mcp_policy.py::forbidden servers are absent` |
| `functions/routing-map/test_routing_map.py` | сервер | карта маршрутов совпадает с конфигом, ни один платный id не достижим | `functions/routing-map/test_routing_map.py::conversation goes to the free core and nothing paid` |
| `functions/hygiene/test_hygiene.py` | сервер | текст проекта валиден UTF-8, замена символов отсутствует, файлы планировщика не правятся молча | `functions/hygiene/test_hygiene.py::no replacement characters anywhere in the project text` |
| `functions/reserve/test_reserve_not_activated.py` | сервер | аварийный платный резерв документирован и НЕ активирован | `functions/reserve/test_reserve_not_activated.py::no paid model id on any active config line` |
| `functions/meta/test_coverage_manifest.py` | сервер | покрытие не сокращается молча и ни один набор не зеленеет без проверок | `functions/meta/test_coverage_manifest.py::a zero check run must be declared not assumed` |
| `functions/opencode-adapter/test_opencode_adapter.py` | сервер | адаптер кодинга: идемпотентность, отказ без авторизации, рельс $0, порог диска, запрет второго экземпляра | `functions/opencode-adapter/test_opencode_adapter.py::repeat does not create a second session` |
| `functions/startup/test_startup.py` | Windows | ярлык Desktop в автозагрузке ведёт на существующий бинарник, мост hands остаётся | `functions/startup/test_startup.py::startup has a working hermes entry` |
| `functions/local-vision/test_local_vision.py` | GPU + веса | картинка доходит до модели и ответ совпадает с эталоном | `functions/local-vision/test_local_vision.py::the answer matches the ground truth` |
| `functions/verification-matrix/test_verification_matrix.py` | сервер | сама матрица не врёт: ссылки существуют, наборы не осиротели, класс машины честен, непроверяемое названо | `functions/verification-matrix/test_verification_matrix.py::every sheet item is mapped` |

### Что физически нельзя проверить на сервере

- `functions/startup/test_startup.py` — Windows. на сервере объявляется SKIP: .lnk, WScript.Shell и диск A:\ не существуют. Четыре группы проверяются только на Windows
- `functions/local-vision/test_local_vision.py` — GPU + веса. нужен живой Ollama на 127.0.0.1:11434 с qwen3-vl; офлайн-группа с эталоном PNG выполняется всегда

## Команды: что проверяется только вживую

| команда | машина | что доказывает | почему не набор |
|---|---|---|---|
| `python functions/free-rank/rank.py` | живой провайдер | живой каталог :free и собственные пробы моделей | нужен живой OpenRouter и около 40 вызовов; в CI не гоняется |
| `python functions/quota-watch/quota_watch.py` | живой провайдер | живой остаток суточной квоты и spend | нужен OPENROUTER_API_KEY |
| `python functions/opencode-adapter/acceptance_live.py` | живой провайдер | приёмка адаптера кодинга целиком: diff, идемпотентность, автоподъём, уборка, $0 | делает настоящие ходы модели |
| `hermes doctor` | живой провайдер | установленный Hermes видит credentials и не раздувает число issues | нужен установленный Hermes |
| `hermes mcp test` | живой провайдер | восемь MCP-серверов соединяются (kiwi отвечает 11-12 с, нужен таймаут 30 с) | живое соединение с внешними серверами |
| `python functions/imggen/comfy_gen.py` | GPU + веса | локальная генерация Qwen-Image-2.1 fp8 через ComfyUI | нужна GPU и локальные веса |
| `hermes computer-use doctor` | Windows | cua-driver 0.28.2, сессия MCP жива, D3D11 доступен | Windows и UIAutomation |
| `fsutil hardlink list` | Windows | файлы в cache/hf и в ComfyUI — одни и те же, дублирования места нет | инструмент Windows |

## Пункты листа: чем закрыт и на какой машине

| пункт | машина | чем закрыт |
|---|---|---|
| А2-bis. Адаптер OpenCode — не только для тестов, а как 24/7-эндпоинт | живой провайдер | `python functions/opencode-adapter/acceptance_live.py` |
| А5. Правило «ноль проверок — не успех» как закон репозитория | сервер | `functions/meta/test_coverage_manifest.py::a zero check run must be declared not assumed` |
| А4. Контракты переносимости: EOL, аргументы, кодировка | сервер | `functions/workerstate/test_worker_state.py::rollback restores the baseline, re-measured` |
| НЕ ЗАКРЫТО — группа, которая не может упасть (`test_local_vision`) | сервер | `functions/meta/test_coverage_manifest.py::no group can pass without asserting anything` (обход снят и ловер добавлен; сама группа переписать нельзя без живого Ollama, поэтому она объявлена в манифесте как non_asserting) |
| 6.5 Уборка | GPU + веса | `python functions/imggen/comfy_gen.py` (уборка касается локальных весов ComfyUI и qwen3-vl) |
| ФАЗА 6. Локальное зрение — Ollama уже стоит | GPU + веса | `functions/local-vision/test_local_vision.py::the answer matches the ground truth` |
| ФАЗА 3. Один вход — Desktop | Windows | `functions/startup/test_startup.py::the real desktop binary exists and is not a lnk` |
| 3.2 Раскладка панелей | Windows | `hermes computer-use doctor` |
| ФАЗА 8. Саморасширение | Windows | `hermes skill install` (живая установка скилла из хаба нужна на машине, где стоит Hermes) |
| 8.2 Включить гейты | Windows | `hermes doctor` (approval-гейты видны в работающем Hermes, не в репозитории) |
| ФАЗА 10. Уборка и гигиена | сервер | `functions/hygiene/test_hygiene.py::project text files are valid utf8` (часть пункта про диски C:/A:/B: вынесена отдельной строкой - она не серверная) |
| P1 — потеря данных, рассинхрон, риск для соседних систем | владелец | **требует владельца** — пункт объединяет несколько независимых вещей, часть из которых не проверяется кодом вовсе: решения владельца по дискам и по ключам. Строки ниже разбирают его по частям |
| 6. C: — 3.4 ГБ свободно из 110 ГБ | владелец | **требует владельца** — только Windows-диски C:/A:, правило владельца «C: не трогать». Настоящая причина - A:\OpenDeamon\cache\hf = 32.95 ГБ; дублирования нет (fsutil hardlink list). Закрывается решением владельца о переносе, а не тестом |
| ⚠️ П1-п.6: диск. Настоящая причина найдена, но не устранена | владелец | **требует владельца** — то же, что и в 6. C:, плюс предложение владельцу: порог «загрузка >5 ГБ начинается с проверки свободного места». Порог - политика, а не проверка |
| ⚠️ ТРЕБУЕТ РЕШЕНИЯ — не закрывать фазу, пока не решено | владелец | **требует владельца** — по определению требует решения владельца; сервер не может закрыть пункт, который ждёт выбора |
| 2-bis.7 Подключение (не сделано) | владелец | **требует владельца** — нужны ключи Groq/NVIDIA в allow-list и решение владельца, какой рельс основной |
| 2-bis.3 Groq: квоты и жёсткое ограничение | живой провайдер | `hermes doctor` (живой замер квот Groq; сам лимит зафиксирован в config.yaml, его проверяет routing-map) |
| Окружение на сервере: что нужно поставить (задачи воркеру) | владелец | **требует владельца** — установка пакетов на сервер - решение владельца по политике хоста, а не проверка проекта |
| АРХИТЕКТУРА: задачи на реализацию | сервер | `functions/verification-matrix/test_verification_matrix.py::every sheet item is mapped` (umbrella для А1…А8; сам факт «каждый пункт чем-то закрыт» проверяется набором) |

### Пункты без автоматической проверки — их решал владелец

Расхождение «пункт есть, а чем закрыть нечем» допускается ровно
в этой форме: пункт назван и объяснён, а не спрятан.

- **P1 — потеря данных, рассинхрон, риск для соседних систем** — пункт объединяет несколько независимых вещей, часть из которых не проверяется кодом вовсе: решения владельца по дискам и по ключам. Строки ниже разбирают его по частям
- **6. C: — 3.4 ГБ свободно из 110 ГБ** — только Windows-диски C:/A:, правило владельца «C: не трогать». Настоящая причина - A:\OpenDeamon\cache\hf = 32.95 ГБ; дублирования нет (fsutil hardlink list). Закрывается решением владельца о переносе, а не тестом
- **⚠️ П1-п.6: диск. Настоящая причина найдена, но не устранена** — то же, что и в 6. C:, плюс предложение владельцу: порог «загрузка >5 ГБ начинается с проверки свободного места». Порог - политика, а не проверка
- **⚠️ ТРЕБУЕТ РЕШЕНИЯ — не закрывать фазу, пока не решено** — по определению требует решения владельца; сервер не может закрыть пункт, который ждёт выбора
- **2-bis.7 Подключение (не сделано)** — нужны ключи Groq/NVIDIA в allow-list и решение владельца, какой рельс основной
- **Окружение на сервере: что нужно поставить (задачи воркеру)** — установка пакетов на сервер - решение владельца по политике хоста, а не проверка проекта

## Сводка по машинам

| машина | наборов |
|---|---|
| сервер | 18 |
| Windows | 1 |
| GPU + веса | 1 |

Пунктов листа: 19, из них требуют владельца: 6, закрыто проверкой: 13.

Живая приёмка (настоящие ходы модели, сеть и ключи) в CI не гоняется:
`python functions/opencode-adapter/acceptance_live.py`,
`python functions/free-rank/rank.py`, `hermes doctor`, `hermes mcp test`.
