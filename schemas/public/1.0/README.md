# Публичные схемы AuditLab 1.0

Исполняемое описание действующего wire format: `reset`/`step`, действия, наблюдения, `info`, ошибки, сдача, строки траектории и `run.json` экспорта. Схемы описывают формат **как он есть** у runner 1.1 и среды (профили генератора 1.0 и 1.1). Формат провода этим файлом не меняется.

| Файл | Назначение |
|---|---|
| `auditlab-public.schema.json` | Единственное определение: JSON Schema Draft 2020-12, все типы в `$defs` |
| `examples/valid.json` | 425 настоящих сообщений среды с указателем схемы, которой каждое обязано соответствовать |
| `examples/invalid.json` | 48 тех же сообщений с одним намеренным дефектом; каждое обязано отвергаться |
| `../../validate.py` | Независимый валидатор на стандартной библиотеке Python, без импорта AuditLab |

## Какое сообщение какой схеме соответствует

| Сообщение | Указатель |
|---|---|
| `reset(seed, config)` → `[observation, info]` | `#/$defs/reset` (`initialObservation`, `resetInfo`) |
| `step(action)` в режиме train | `#/$defs/stepTrain` |
| `step(action)` в закрытом режиме | `#/$defs/stepClosed` |
| Корректное действие политики | `#/$defs/action` |
| Что раннер принимает как транспортно целое действие | `#/$defs/actionEnvelope` |
| Тело `submit` | `#/$defs/submission` |
| Наблюдение после действия (без контекста) | `#/$defs/stepObservation` |
| Ответ на конкретное действие | `replyListLedger`, `replyListInvoices`, `replyListPayments`, `replyListFx`, `replyListPolicy`, `replyGetLedger`, `replyGetInvoice`, `replyGetPayment`, `replyGetFx`, `replyGetPolicy`, `replyGetDocument`, `replyCalculator`, `replySubmitAccepted`, `errorObservation`, `protocolObservation` |
| Строка `trajectories.jsonl` | `trajectoryLineTrain` / `trajectoryLineClosed` |
| `run.json`, `score.json` экспорта | `exportRun`, `score` |
| Записи таблиц | `ledgerRow`, `invoice`, `payment`, `fxRow`, `policyRow`, `manifest`, `policyDocument` |

`step` описан как массив из пяти элементов `[observation, reward, terminated, truncated, info]` с четырьмя допустимыми формами:

| Форма | reward | terminated / truncated | info |
|---|---|---|---|
| Осмотр (шаги 1–99) | 0 | false / false | `{"inspection_steps": n}` |
| Принятый или структурно ошибочный `submit` | train: [−1, 1]; closed: **0** | true / false | `{"status"}` (+ `score` только в train) |
| Сотый осмотр, тайм-аут | train: [−1, 1]; closed: **0** | false / true | то же |
| Протокол: `malformed_action`, `no_active_episode`, `episode_finished` | 0 | как у финала / false | `{"protocol_error": code}` |

## Ошибки

Ошибка — фиксированный код и **фиксированное** сообщение; схема требует точную пару, поэтому через ошибку нельзя передать путь, стек или закрытое значение.

| Код | Источник | Когда | Шаг |
|---|---|---|---|
| `invalid_args` | среда | лишний/отсутствующий аргумент, неверный тип, `limit` вне 1–50 | тратится |
| `unknown_table` | среда | таблица не из `ledger, invoices, payments, fx, policy` | тратится |
| `not_found` | среда | неизвестный ID | тратится |
| `invalid_cursor` | среда | чужой, повреждённый или устаревший курсор | тратится |
| `invalid_expression`, `division_by_zero`, `arithmetic_limit` | среда | калькулятор | тратится |
| `invalid_request` | раннер | у инструмента нет объекта `args` | тратится |
| `unknown_action` | раннер | неизвестное имя действия | тратится |
| `internal_error` | раннер | сбой сессии или оценщика (детали только в доверенном журнале) | тратится |
| `invalid_submission` | раннер | распознанный `submit` не прошёл схему; итог −1 | финал |
| `time_limit` | раннер | дедлайн наступил до принятия действия | финал |
| `aborted` | раннер | сбой политики (только в траектории; политика его не получает) | финал |
| `malformed_action` | раннер | не объект, нет строки `action` или лишние ключи конверта | **не тратится** |
| `no_active_episode`, `episode_finished` | раннер | до `reset` или после финала; без `remaining_steps` | не тратится |
| `invalid_action` | среда | только при прямом вызове `PublicSession.inspect`; через раннер недостижим | — |

## Закрытый режим

`stepClosed` требует нулевую награду и `info` без `score`. Записи таблиц и манифест закрыты (`additionalProperties: false`), а свободные значения (правила политики, результат `get_record(policy, …)`) проверяются рекурсивно: ключи закрытых данных (`truth`, `issue_id`, `evidence_sets`, `expected_amount_minor`, `seed`, `true_reward`, `score` и др.) запрещены на любой глубине.

## Семантика проверки

`validate.py` реализует перечисленное в его docstring подмножество Draft 2020-12 и отвергает любые другие ключевые слова — схема не может молча ослабнуть. Отличия от универсальных валидаторов:

- `integer` — число JSON без дробной части и экспоненты. Стандартные валидаторы принимают `2.0` как целое, а оценщик такую сумму отвергает (`invalid_submission`, −1). Это единственное расхождение, найденное сверкой с `jsonschema` 4.23.0 на всех 473 примерах.
- `false` не равно `0`; NaN/Infinity и повторяющиеся ключи отвергаются `load_json`.
- Входной JSON глубже 64 уровней признаётся невалидным без рекурсии.

```sh
python schemas/validate.py schemas/public/1.0/auditlab-public.schema.json action action.json
python schemas/validate.py schemas/public/1.0/auditlab-public.schema.json trajectoryLineTrain run/trajectories.jsonl --jsonl
```

## Эталонные примеры и внешний адаптер

`valid.json` строится прогоном настоящих `make_env`, `open_session`, оценщика в отдельном процессе и опубликованных снимков; тест сравнивает файл с живым прогоном, поэтому любое изменение формата видно сразу.

```sh
python tests/runner/schema_golden.py --check   # 0, если примеры совпадают с текущими компонентами
python tests/runner/schema_golden.py           # пересобрать после согласованного изменения формата
```

`tests/runner/schema_adapter.py` — внешний адаптер политики: запускается как `python -I`, импортирует только стандартную библиотеку, загружает этот файл и валидатор по пути, сам читает `examples/*.json`, проверяет каждое полученное наблюдение и каждое своё действие и выполняет все пять действий через канал JSON Lines.

## Версии и изменения

Идентификатор `urn:auditlab:public-schema:1.0`; `manifest.schema_version` остаётся `"1.0"`. Любое изменение формата провода требует новой версии схемы, обновлённых примеров и проверки консоли и политик. Новые необязательные поля профиля генератора (например, `period_end_exclusive`, `policyRow.version` профиля 1.1) добавляются в схему явно — неизвестное поле в записи считается ошибкой.
