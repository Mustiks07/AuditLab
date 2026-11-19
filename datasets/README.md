# Открытые наборы AuditLab 1.0

В `open/train` и `open/dev` по 10 синтетических эпизодов. Каждый набор содержит две чистые компании, четыре случая с одной ошибкой каждого типа, оба взаимодействия и два варианта смешанного пограничного случая. У каждой компании 28 строк проводок и 9 первичных документов. Имена каталогов совпадают с непрозрачными ID из публичного манифеста.

В `train` пограничная пара сохраняет независимый дубликат счёта. Последний зачёт проходит за минуту до границы или за минуту после неё. В `dev` пара сохраняет независимую ошибку периода и сравнивает зачёт за минуту до границы с зачётом ровно на ней. Каждая пара целиком входит в свой набор. Оба набора содержат чистую компанию с валютной оценкой. В `dev` используются JPY и KWD с разрядностью 0 и 3.

## Воспроизведение

Из корня репозитория с Python 3.11 или новее выполните команду с новым каталогом назначения.

```sh
python -m datasets.build build/open-datasets
python -m unittest discover -s tests/foundation -v
python -m unittest discover -s tests/simulator -v
```

`recipe.json` фиксирует версии, семейства, 128-битные seeds и конфигурации. Повторная сборка в другом каталоге создаёт те же относительные пути и байты. Доверенный код использует `datasets.build.build_datasets(output_dir)` для подготовки серии. Обычному аудитору передаётся `public/` конкретного эпизода. Организатор использует `private/truth.json` для оценки и `private/provenance.json` для воспроизведения.

## Пара с зачётом 50 000 ₸

```sh
python -c "from pathlib import Path; from auditlab.generator import generate_episode; generate_episode(817, {'settlement_timing': 'before', 'clean_distractors': []}, Path('build/pair/a'))"
python -c "from pathlib import Path; from auditlab.generator import generate_episode; generate_episode(817, {'settlement_timing': 'after', 'clean_distractors': []}, Path('build/pair/b'))"
python -m unittest discover -s tests/simulator -p 'test_generated_pair.py' -v
```

Тест переносит только `public/` и через `open_session` находит платёж, счёт, правило и полный перечень движений. Остаток равен 0 ₸ при `before` и 20 000 ₸ при `at` и `after`. Значения в документах даны в тиынах. [Публичные инструменты и ключи](../docs/INTERFACES.md)

## Публичная сессия

`open_session(public_dir).inspect(action)` принимает `action` и `args` по контракту. `list_records` возвращает `records` и `next_cursor`. Передайте `cursor=null` для первой страницы и используйте полученный курсор для следующих. Допустимый размер страницы составляет от 1 до 50 записей. Доступны таблицы `ledger`, `invoices`, `payments`, `fx`, `policy`.

Для `get_record` используйте `row_id` в `ledger`, `doc_id` в `invoices` и `payments`, `YYYY-MM-DD/SRC/DST` в `fx` и имя правила в `policy`. Страницы `policy` содержат объекты `id` и `value`. `get_document` принимает `doc_id` счёта или платежа. В доказательствах добавляйте название таблицы, например `ledger/R72`. Правая граница `period_end` в манифесте исключается.

`calculator` возвращает десятичную строку. Допустимы десятичные числа, скобки и ASCII-операторы `+`, `-`, `*`, `/`. Длина выражения ограничена 256 символами, вложенность составляет до 24 уровней. Расчёт использует 64 значащие цифры и `ROUND_HALF_UP`. `remaining_steps` и `submit` обслуживает runner.

## Конфигурация генератора

`generate_episode(seed, config, output_dir)` принимает целочисленный seed и создаёт новый каталог снимка. Параметры можно комбинировать.

| Поле | Допустимые значения |
|---|---|
| `config_version` | `1.0` |
| `target_period`, `timezone` | Месяц `YYYY-MM` и локальная зона, по умолчанию `2026-11` и `Asia/Almaty`. Для других зон, кроме `UTC`, используется системная база IANA. |
| `base_currency`, `currencies` | KZT, USD, EUR, JPY, KWD. Список уникален и включает базовую валюту. Для FX нужна иностранная валюта. |
| `issue_counts` | От 0 до 5 для `duplicate_invoice`, `payment_allocation`, `fx_conversion`, `period_cutoff`. Отсутствующее значение равно 0. |
| `interaction` | `allocation_fx_same_payment` или `cutoff_fx_same_invoice`. Счётчики выбирают активные дефекты. При нулевых счётчиках получается чистое состояние. |
| `settlement_timing` | `before`, `at`, `after` для пограничного зачёта. Для `at` и `after` нужен один дефект распределения. Если счётчик опущен, генератор задаёт его по времени зачёта. |
| `clean_distractors` | `split_payment`, `equal_amount_different_supplier`, `refund`, `archive_copy` |
| `company_size` | `ledger_rows_target` от 0 до 2 000 и `documents_target` от 0 до 600 задают желаемые минимумы. Целые проводки и документы могут повысить итоговый размер. |
| `snapshot_nonce` | Необязательное непрозрачное значение для различения независимых публикаций одного снимка. |
