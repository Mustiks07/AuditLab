# Адаптер жюри Genesis (`judge_adapter.py`)

Источник: [определения проверок FinTech](https://genesis-exe.com/fintech-checks.html), опубликованы 20.11.2026, прочитаны 21.11.2026. Контрольная сумма загруженной страницы — в `sources.json` пакета интегратора `docs/reviews/official-checks-2026-11-20`.

Адаптер — тонкая доверенная обёртка над генератором, runner и evaluator AuditLab. Он только переводит конверт действий, наблюдения и результаты. Действия за политику он не выбирает и ничего в них не меняет. Бюджет, срок и оценку по-прежнему ведут `AuditEnvironment` и отдельный процесс оценщика.

Версии: адаптер `1.0`, протокол `genesis-fintech-checks-2026-11-20`, правила matcher `genesis-2026-11-20`, runner `1.2`, evaluator `1.2`, публичная схема AuditLab `1.0` не меняется.

## Запуск

Рабочая директория — корень репозитория, в образе runtime — `/app` (`PYTHONPATH=/app`, Python 3.11.14, закреплённый `tzdata`). Сеть не нужна.

```sh
# 1. Самопроверка адаптера внутри образа runtime (из корня /app)
docker run --rm --network none auditlab-runtime:<rev> \
  python -m unittest tests/runner/test_genesis_adapter.py tests/evaluator/test_genesis_rules.py

# 2. Точки входа жюри: каталог жюри монтируется в контейнер
docker run --rm --network none -v "$PWD/judge:/judge" auditlab-runtime:<rev> python -c '
import judge_adapter
judge_adapter.generate_episode(1, {"config_version": "1.1"}, "/judge/ep1")
env = judge_adapter.make_env()
observation, info = env.reset(1, {"mode": "closed", "episode_dir": "/judge/ep1", "team_config": {}})
print(env.step({"name": "list_records", "args": {"table": "ledger", "cursor": None, "limit": 50}})[0]["remaining_steps"])
print(judge_adapter.score_episode("/judge/ep1", {"episode_id": info["episode_id"], "findings": []}, 1))'
```

Из чистого checkout без Docker: Python 3.11, `python -m pip install --require-hashes -r requirements.lock`, затем те же команды из корня. Без закреплённого `tzdata` генератор отказывает (`timezone_data_required`). Тесты, которым нужен генератор, тогда пропускаются, остальные выполняются.

## Точки входа

| Функция | Поведение AuditLab |
|---|---|
| `generate_episode(seed, config, output_dir)` | Вызывает генератор AuditLab. `config` — конфигурация генератора, напрямую или как `{"team_config": {...}}`. `output_dir` новый или пустой; существующий эпизод не перезаписывается. Одинаковые seed и config дают побайтно одинаковые файлы. |
| `make_env()` | Объект с `reset(seed, config)` и `step(action)`. Каждое окружение оценивается правилами `genesis-2026-11-20`. Оценщик — отдельный процесс `python -m auditlab.evaluator`. |
| `reset(seed, {"mode", "episode_dir", "team_config"})` | `mode` — `closed` (по умолчанию) или `train`. С `episode_dir` загружает именно этот каталог, без генерации и смены ID; повторный reset начинает эпизод заново. Без `episode_dir` генерирует эпизод из `seed` и `team_config`. Срок 60 с отсчитывается от входа в reset, до загрузки. |
| `step({"name", "args"})` | Переводит в `{"action", "args"}` runner. Возвращает `(observation, reward, terminated, truncated, info)`. |
| `score_episode(episode_dir, findings, inspection_steps)` | Закрытый вызов оценщика. Корректная сдача: `{tp, fp, fn, precision, recall, f1, return}`. Невалидная: `return = -1`, остальные поля `null`. |

Только для жюри, политике не передаются: `env.trusted_result()` (статус, шаги, официальный результат, диагностика сопоставления), `env.expire()` (остановка по сроку с пустыми findings), `env.seconds_left()`, `env.versions()`.

Журнал оценщика: логгер `auditlab.judge_adapter`. Если задан `AUDITLAB_JUDGE_LOG=<файл>`, записи дополнительно пишутся туда построчно в JSONL: reset (mode, seed, episode_dir, team_config, версии) и каждый `score_episode` (статус, причина структурной ошибки, сопоставления).

## Каталог эпизода

`generate_episode` создаёт:

```
<episode_dir>/public/   manifest.json ledger.csv invoices.jsonl payments.jsonl fx.csv policy.json
<episode_dir>/private/  truth.json provenance.json (+ source_book/, source_transition.json в профиле 1.1)
```

`reset` и `score_episode` требуют `public/manifest.json` и `private/truth.json`. Политика получает только наблюдения. Пути, seed, config, truth и provenance ей не передаются.

Для парных проверок жюри изменяет копию: переставляет строки и согласованно меняет ID в `public/` и в `private/truth.json`. Адаптер загружает копию как есть. Порядок `list_records` определяется ключом поиска (`row_id`, `doc_id`, `date/SOURCE/TARGET`, имя правила), а не порядком строк в файле. Пример преобразования — `auditlab/release/transforms.py`.

## Перевод протокола

| Официально | AuditLab runner |
|---|---|
| `{"name": N, "args": A}` | `{"action": N, "args": A}` |
| `{"name": "submit", "args": {"findings": F}}` | `{"action": "submit", "args": F}`, где F = findings.json `{episode_id, findings}` |
| первое наблюдение `{task, manifest, policy, remaining_steps}` | внутреннее первое наблюдение без поля `tools` |
| `{"result": value, "error": null, "remaining_steps": n}` | `{"ok": true, "result": value, "remaining_steps": n}` |
| `{"result": null, "error": {code, message}, "remaining_steps": n}` | `{"ok": false, "error": {code, message}, "remaining_steps": n}` |
| submit принят: `{"result": {"accepted": true}, "error": null}` | `{"ok": true, "result": {"status": "submitted"}}` |

Каждый вызов step — действие бюджета, в том числе ошибочный. Конверт, отличный от `{"name": строка, "args": ...}`, получает ошибку `malformed_action` и тратит шаг. Значение, которое не переносится JSON (например, NaN), тоже тратит шаг: для инструмента это ошибка аргументов, для submit — невалидная сдача. Тело submit, отличное от `{"findings": объект}`, тоже считается невалидной сдачей (`return = -1`).

Результаты инструментов: `list_records` возвращает `{"records": [...], "next_cursor": null | строка}`, первый курсор `null`, лимит 1–50; `get_record` и `get_document` возвращают объект; `calculator` возвращает десятичную строку (`0.1+0.2` → `"0.3"`). Коды ошибок публичны: `not_found`, `unknown_table`, `unknown_action`, `invalid_args`, `invalid_request`, `invalid_cursor`, `invalid_expression`, `division_by_zero`, `arithmetic_limit`, `malformed_action`, `invalid_submission`, `time_limit`, `episode_finished`. Путей, seed и закрытых данных в них нет.

`info`: `status`, `inspection_steps`, `protocol_error`. В `train` после завершения добавляется `score`. В `closed` награда политике всегда `0.0`.

## Таблицы, поля, типы ошибок

| Таблица | ID для `get_record` | Поля §03 | Дополнительные документированные поля |
|---|---|---|---|
| `ledger` | `row_id` | row_id, timestamp, account, debit_minor, credit_minor, currency, entity_id, invoice_id, payment_id | `journal_id` |
| `invoices` | `doc_id` | doc_id, invoice_id, entity_id, issued_at, due_at, currency, total_minor, line_items | — |
| `payments` | `doc_id` | doc_id, payment_id, timestamp, currency, amount_minor, invoice_refs | `settlement_index`, `allocation_instructions` (если есть) |
| `fx` | `DATE/SOURCE/TARGET` | date, source_currency, target_currency, rate (десятичная строка) | — |
| `policy` (доп. таблица) | имя правила | выбор даты курса, граница периода, округление | `rule_versions` в профиле 1.1 |

Деньги — целые минимальные единицы, время ISO-8601 со смещением, отсутствующие ссылки — `null`. Документы доступны и через `get_document(doc_id)`.

| Тип ошибки из кейса | `type` в findings |
|---|---|
| дубль счёта | `duplicate_invoice` |
| пропущенное или частичное распределение платежа | `payment_allocation` |
| неверная дата или курс конвертации | `fx_conversion` |
| ошибка отнесения к отчётному периоду | `period_cutoff` |

Сценарии взаимодействия (`interaction`): `allocation_fx_same_payment`, `cutoff_fx_same_invoice`. Чистые отвлекающие случаи (`clean_distractors`): `split_payment`, `equal_amount_different_supplier`, `refund`, `archive_copy`, в профиле 1.1 также `next_day_settlement`.

Ключи конфигурации генератора: `config_version` (`"1.0"` по умолчанию, рекомендуется `"1.1"`), `company_size` (`ledger_rows_target` 0–2000, `documents_target`), `base_currency`, `currencies`, `timezone`, `target_period` (`YYYY-MM`), `issue_counts` (каждый тип 0–5, в профиле 1.1 всего до 12), `interaction`, `clean_distractors`, `settlement_timing` (`before`/`at`/`after`), `snapshot_nonce`. Чистая компания: все `issue_counts` равны 0.

Формы ссылок:
- `affected_ids` — `invoice/<invoice_id>` и `payment/<payment_id>`;
- `evidence_ids` — `ledger/<row_id>`, `invoices/<doc_id>`, `payments/<doc_id>`, `fx/<date>/<SRC>/<TGT>`, `policy/<rule>`.

## Matcher, правила `genesis-2026-11-20`

Код: `auditlab/evaluator/schema.py`, `matcher.py`, `evidence.py`, `service.py`.

- **Структура.** Невалидными считаются: не объект; ключи сдачи не `{episode_id, findings}`; чужой `episode_id`; ключи вывода не равны шести полям §05; `type`, `currency` или `explanation` не строка; списки не из строк; сумма не JSON-целое (bool, строка, дробь). Также лимиты: больше 30 выводов, больше 10 ID доказательств до удаления повторов, `explanation` длиннее 1000 code points. Итог — `return = -1`.
- **Семантика.** Неизвестная строка `type` и отрицательная целая сумма структурно валидны и дают FP.
- **Сравнение.** ID, `type` и `currency` сравниваются как точные строки. `affected_ids` — точное множество. `evidence_ids` должны покрывать хотя бы один допустимый набор truth.
- **Доступность.** Каждый указанный ключ доказательства должен читаться через `get_record` этого эпизода. Одна недоступная ссылка делает вывод несопоставимым (FP), даже если минимальный набор присутствует.
- **Допуск τ.** По умолчанию 0 для всех типов. Фикстура может задать его публично в `policy.json` ключом `matching_tolerance_minor`: одно целое ≥ 0 или `{тип: целое}`. Условие: `|claimed − expected| ≤ τ`. Генератор этот ключ не пишет.
- **Неоднозначность.** Строится максимальное паросочетание. Среди равных по мощности выбирается то, у которого лексикографически наименьшая отсортированная последовательность пар `(issue_id, индекс вывода)`; `issue_id` сравниваются по code points. Повторные выводы остаются отдельными FP.
- **Эталон.** Допускаются две ошибки с одинаковыми `(type, affected_ids)`: так устроены фикстуры M10/M11. Уникальным должен быть только `issue_id`.

Внутренние правила `auditlab-1.1` (по умолчанию для runner, rehearsal и публичной схемы 1.0) сохраняют прежнюю структуру. Неизвестный `type` и отрицательная сумма остаются невалидными, одинаковая пара `(type, affected_ids)` в эталоне отвергается. Общими для обоих профилей стали проверка доступности доказательств (исправление дефекта 2 из разбора интегратора) и правило выбора при неоднозначности. TP/FP/FN от порядка не зависят.

## Бюджет, срок, изоляция

- **Бюджет.** Всего 100 действий. Submit входит в лимит, но не в `inspection_steps`. 100 исследовательских действий без submit дают `truncated` и пустые findings.
- **Срок.** 60 с от входа в reset, включая загрузку эпизода и время политики. После срока ближайший step или `env.expire()` даёт `truncated` и пустые findings.
- **Изоляция.** Адаптер работает на стороне жюри. Политика жюри работает в своём процессе и получает только наблюдения. Собственные базовые политики AuditLab по-прежнему запускаются в Docker-песочнице `auditlab-policy` (`python -m auditlab.runner policy ...`) без доступа к evaluator, truth и каталогу эпизода.

## Что проверено и что нет

Проверено тестами `tests/runner/test_genesis_adapter.py` и `tests/evaluator/test_genesis_rules.py` в образе runtime RC3 (Linux, Python 3.11.14, без сети):

- **C01–C20** — локальными аналогами на фикстурах C (S01), E (S02), L (сгенерированный эпизод, больше 100 строк) и U (ID, выбранный после чтения данных);
- **matcher** — 20 векторов награды и M01–M12, с ожиданиями, выведенными рациональной арифметикой;
- **пары** — загрузка преобразованной копии без изменения исходника.

Это внутренняя проверка команды, а не официальный балл. Не проверено:

- политики организаторов и их процессная изоляция;
- скрытые эпизоды D01–D20 и матрица P01–P20 в полном объёме;
- время на машине жюри.
