# Консоль исследования агентов

Раздел «Исследование» в принятом интерфейсе C3 проводит пользователя по всему циклу из
[пакета итерации](https://github.com/Mustiks07/AuditLab/tree/planning/agent-improvement/docs/plans/agent-improvement):

**эксперимент → найденная слабость → документы и действия агента → сокращённый случай и семейство →
учебный набор и обучение → сравнение версий на новых компаниях.**

Каждый экран показывает результат этапа и ссылку на следующий. Линия «Путь исследования» вверху ведёт ко
всем связанным результатам. Если результата нет, этап виден как недоступный, с причиной.

## Запуск

```sh
python -m pip install --require-hashes -r requirements.lock
```

```sh
python -m auditlab.demo serve --research КАТАЛОГ_РЕЗУЛЬТАТОВ
```

Пока у интегратора нет общего контроллера и схем, экраны можно проверить на фикстурах разработки:

```sh
python -m auditlab.demo serve --research-fixtures
```

Фикстуры строятся в `runs/demo/research-fixtures` (или `ROOT/research-fixtures` при `--root`) командой
`python -m auditlab.demo research-fixtures КАТАЛОГ`. Каталог результатов также задаётся переменной
`AUDITLAB_RESEARCH_ROOT`; несколько каталогов разделяются `:`.

## Что такое фикстура разработки

Фикстура не является измерением, и интерфейс показывает это на каждом экране меткой
«Фикстура разработки · не измерение». Её можно проверять, но нельзя выдавать за ответ модели.

- **Компании и документы** — настоящие снимки открытого набора `datasets/open/train`. Исключение —
  вариант компании B с новыми номерами документов: он построен генератором с тем же seed.
- **Действия агента** задаёт скрипт `auditlab/demo/research_fixtures.py`, а не модель. Вариант «без
  памяти» не запрашивает проводки и заявляет всю сумму платежа; вариант «с заметками» читает проводки.
  Скрипт исполняет настоящий раннер `make_env("train")`: наблюдения — это настоящие ответы среды,
  оценка — настоящий оценщик.
- **Независимая проверка** — SQL-сверка AuditLab по публичным файлам.
- **Сокращённый случай** получен фильтром фикстуры, а не алгоритмом поиска GPT Pro 1. SQL-сверка
  подтверждает в нём ту же ошибку 20 000 ₸.
- **Частоты, интервалы, задания обучения и метрики сравнения** — иллюстративные значения для
  интерфейса.

Truth фикстуры лежит в `trusted/` и сервером консоли не читается.

## Формат результатов (временный, до схем интегратора)

Сервер читает JSON с общими полями из CONTRACTS: `schema_version` (поддерживается 1.x), `artifact_id`,
`source_sha`, `protocol_id` и дополнительно `provenance.kind`: `measured`, `dev_fixture` или
`test_double`. Тип артефакта определяется полем `artifact_kind`, а если его нет — именем файла
(`experiment.json`, `attempt-*.json` …) или родительского каталога (`attempts/`). Связи:

| Артефакт | Ссылки, которые использует консоль |
|---|---|
| experiment | `agents[]` (variant_id, label, kind, model), `mode`, `progress`, `budget`, `timing`, `cost`, `status`, `stop_reason`, `attempt_ids` |
| attempt | `experiment_id`, `variant_id`, `repetition`, `episode.company_label`, `snapshot_ref` (каталог `public/` внутри корня), `steps[]`, `submission`, `evaluation.visibility`, `status`, `error`, `usage` |
| failure | `experiment_id`, `attempt_ids`, `validation_id`, `reduction_id`, `finding_type`, `criterion`, `frequency`, `interval`, `alternatives` |
| validation | `snapshot_ref`, `independent.findings` |
| reduction | `failure_id`, `original` и `reduced` (`snapshot_ref`, `validation_id`), `repeats`, `families[]` (`relation`, `expected`, `actual[].attempt_id`) |
| curriculum | `experiment_id`, `failure_ids`, `reduction_ids`, `splits`, `items`, `filtered` |
| training | `curriculum_id`, `kind` (weights / prompt / few_shot), `state`, `result`, `error`, `budget` |
| comparison | `experiment_id`, `sample`, `variants[].metrics` (знаменатели), `paired[]` (интервалы), `verification`, `missing` |

Когда интегратор опубликует JSON Schema 1.0, нужно будет сверить с ней только загрузчик `research.py`.

## Граница данных

- **Защита от сбойных файлов.** Несовместимый или повреждённый файл не роняет экран. Это неверная
  версия схемы, NaN или Infinity, отрицательный или дробный счётчик, не JSON, неизвестный тип,
  неверный или повторный id. Такой файл попадает в список «Несовместимые результаты» с причиной.
- **Приватные поля** (`truth`, `seed`, `config`, `api_key`, `secret` и подобные) рекурсивно удаляются
  из всех ответов.
- **Снимки** открываются только как `public/` внутри корня результатов. Пути с `..`, абсолютные,
  `private`, `trusted` отклоняются. Документ открывается по точному ключу записи.
- **Скрытая оценка** (`evaluation.visibility` не `open`) и ответ попытки во время контроля в браузер
  не передаются.
- **Финансовые расчёты.** Сервер берёт расчёт ситуации из существующей деривации консоли (поступило /
  распределено до границы / остаток). Браузер не считает финансовых итогов и альтернативных средних.
  Метрики сравнения показываются как опубликованы.

## Запуск и остановка

`POST /research/experiments/{id}/start|stop` вызывает `auditlab.research.controller` интегратора
(`status()`, `start(id)`, `stop(id)`), если модуль подключён:

| Состояние | Ответ сервера |
|---|---|
| Контроллера нет | 503, `controller_not_connected`; кнопка запуска неактивна с пояснением |
| Нет бюджета или доступа к модели | 409, `budget_not_configured` или `api_key_missing`; контроллер не вызывается |
| Ошибка контроллера | 503 или 409 с кодом из исключения (например, `model_unavailable`) |
| Ответ контроллера | Возвращается браузеру без приватных полей; ключ API наружу не передаётся |

«Живой запуск» показывается, только когда контроллер подключён и эксперимент выполняется. В остальных
случаях — «Запись эксперимента: состояние на момент сохранения». Траектории всегда помечены «Сохранённая
траектория».

## Проверки

```sh
python -m unittest discover -s tests/demo -v
```

```sh
npm --prefix web test
```

```sh
npm --prefix web run build
```

```sh
python -m auditlab.demo --root artifacts/research-browser serve --port 8765 --research-fixtures
```

```sh
node web/tests/research.browser.mjs
```

Результаты браузерной приёмки и снимки — в [docs/design/c3/research-evidence](../design/c3/research-evidence/README.md).
Связанные финансовые ситуации, досье ошибки и карта результатов — [FINANCIAL_LAB.md](FINANCIAL_LAB.md).
Сценарий видео — [RESEARCH_VIDEO.md](RESEARCH_VIDEO.md).
