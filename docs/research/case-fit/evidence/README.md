# Журнал воспроизведения

## Проверенные версии

| Объект | Подтверждённая версия |
|---|---|
| Исходный head ветки исследования | `3faef433475e101bf71115ebabe2b7b8d8d16748` |
| Исходный и повторно сверенный `origin/main`, 20.11.2026 | `a301b16b825c5575393d94f92ac1707f42e292cb`. Начальная ветка добавляла только материалы кейса |
| Оригинал PDF | 6 физических страниц, 76 907 B, SHA-256 `f5406bfc1185f174f034cecbf0f01326ba7f2c1304a1440113fa985d7fb47cbd` |
| Обновление runner #28 | `63b3267bdbcf119c15f58de9f4dbbe326eb25818` |
| Исходный и обновлённый #22 | `39bd6fb80d9204c7056e3170ba5d89410a2745bc` → `2d31213d7fdec9f7dbe017324099652777d1cd31`. Новый head включает fix #23 |
| Локальный runtime | Windows, Python 3.12.14, DuckDB 1.1.3, tzdata 2026.6. Linux Docker daemon недоступен |

## Три прогона GPT Pro 1 на исходном коде

Это исторические проверки кода `a301b16b825c5575393d94f92ac1707f42e292cb`,
а не текущего `main`. Скрипт `root-probe.py` импортирует проект относительно своего
расположения. После слияния исправлений запуск из текущей ветки уже не воспроизводит
старый сбой и не должен трактоваться как регрессия. Команды ниже создают отдельный
checkout кода и копируют в него только материалы проверки; код остаётся на исходном SHA.
Ожидаемые сбой #23 и отказ FX относятся именно к нему.

В новом рабочем каталоге Linux/WSL с установленным `uv`:

```sh
git clone https://github.com/Mustiks07/AuditLab.git AuditLab-research
git -C AuditLab-research checkout --detach 7e54e5b3c32ec6ac37a339a3ae40d21ce66e28b1
git -C AuditLab-research worktree add --detach ../AuditLab-baseline a301b16b825c5575393d94f92ac1707f42e292cb
mkdir -p AuditLab-baseline/docs/research/case-fit
cp -R AuditLab-research/docs/research/case-fit/evidence AuditLab-baseline/docs/research/case-fit/
uv venv --python 3.11.14 AuditLab-baseline/.venv
uv pip install --python AuditLab-baseline/.venv/bin/python --require-hashes -r AuditLab-baseline/requirements.lock
cd AuditLab-baseline
export PYTHONTZPATH=''
git rev-parse HEAD
.venv/bin/python docs/research/case-fit/evidence/root-probe.py prepare artifacts/case-fit-root-repeat
# Читать только artifacts/case-fit-root-repeat/auditor и сверить ручной ответ.
sha256sum docs/research/case-fit/evidence/root-public.md
.venv/bin/python docs/research/case-fit/evidence/root-probe.py check artifacts/case-fit-root-repeat
.venv/bin/python -m unittest discover -s tests/integration -p test_control_pair.py -v
```

Исходный автор использовал Windows/Python 3.12.14, как указано выше, и `Get-FileHash`
вместо `sha256sum`. Новый рецепт использует поддерживаемый проектом Python 3.11.14;
это инструкция повторения исторического входа, а не замена условий первоначального
замера. Проверку текущих исправлений нужно проводить отдельно на их точных SHA.

Скрипт создаёт отдельные публичные копии, а на второй стадии проверяет хеш ручного ответа, затем открывает truth чистого FX-контроля. Фиксированный SHA-256 ручного ответа `de5d8a0f358e32c90f607a8ead664a9361adb9ed9adda1a33e45d09147eef518`. Локальная последовательность этапов записана здесь. Хеш даёт проверку неизменности файла, независимой внешней отметки времени для этого прогона нет. [Ручной ответ](root-public.md) · [Файл SHA-256](root-public.sha256) · [Наблюдения](root-observed.json)

| Воспроизведение | Ожидалось | Получено |
|---|---|---|
| 1. Неверная сдача #23 на S02, train и closed | Один терминальный результат и журнал, −1 в train, нейтральный 0 в closed | Неглубокая ошибка завершается по контракту. Глубина 600 в исходном runner даёт `RecursionError`, фазу `done`, `final=null` и пустую траекторию в обоих режимах |
| 2. FX EUR→JPY, seed 0 | Чистый вариант без находки. Запрошенный дефект должен быть виден после округления и случай должен создаваться | Чистый truth пуст. 61 EUR × 1,21 и × 1,22 дают одинаковые 74 JPY после округления. С включённой мутацией `RuntimeError: unintended_issue_counts`, снимок не записан |
| 3. S01/S02 и замена evidence | За границей 20 000 ₸, у правильного ответа полный комплект evidence | `reconcile` через публичные действия выполнил 5 инспекций и `submit` в каждом опыте. S01 0/0/0 и 0,99. S02 1/0/0 и 0,99. Та же сумма с `ledger/R6` вместо `ledger/R5` дала 0/1/1 и −1 |
| Дополнительные тесты границы и индекса | До, ровно на и после границы, независимость от порядка строк и часового смещения | 2/2, включая 36 подслучаев календарного расчёта |

Воспроизведения работают с синтетическими локальными снимками и доверенным контроллером. Они не проверяют Docker sandbox. Полный финансовый вход и дополнительные команды находятся в исходном отчёте агента. [План независимого финансового прогона](finance-plan.md) · [Ручный ответ агента](finance-public-answer.json) · [Проверка простого признака](finance-shortcut-public.json) · [Финансовый отчёт](../agents/finance-generalization.md)

## Точная проверка открытых исправлений

Для каждой строки экспортирован ровно указанный коммит командой `git archive --format=zip --output=PATH.zip SHA`. Архив распакован в отдельный каталог. Команды выполнялись из его корня тем же Python.

| PR и SHA | Команда | Результат |
|---|---|---|
| [#28](https://github.com/Mustiks07/AuditLab/pull/28) `63b3267bdbcf119c15f58de9f4dbbe326eb25818` | `python -m unittest discover -s tests/runner -p test_finalization.py -v` | 11/11. Исходный #23 через прямой API и внешний JSONL в train/closed, ровно один финал, бюджет, timeout, persistence |
| [#22](https://github.com/Mustiks07/AuditLab/pull/22) `2d31213d7fdec9f7dbe017324099652777d1cd31` | `python -m unittest discover -s tests/runner -p test_public_schemas.py -v` | 15/15. Реальный `calculator` с лишним `seed` принят как вход и как часть журнала, среда отвечает `invalid_args`. Закрытые поля в доверенных наблюдениях отвергаются, внешний stdlib-адаптер сходится |
| #22, тот же SHA | `python -m unittest discover -s tests/runner -p test_finalization.py -v` | 11/11, результат на отдельном head схем |

Первоначальный контрпример #22 описан в интеграционном review по `39bd6fb`. Исправленный head разделяет вход политики и доверенные observation/info. Результаты открытых PR хранятся отдельно от замеров `main`. В объединённом итоговом Docker-комплекте эти изменения ещё не проверены. [Предыдущий review](../../../reviews/integration-2026-11-20.md)

Открытые смежные работы на контрольной дате:

- [#13 / PR #26](https://github.com/Mustiks07/AuditLab/pull/26), head `55690578ac79361eef429d598da97cb386a81838`. Автор публикует оригиналы проверки 1.3 и новый public-first отчёт по 1.4. Историю и файлы этого PR здесь не перепроверяли, штатный доступ к полной исходной книге остаётся отдельной проверкой.
- [#15 / PR #27](https://github.com/Mustiks07/AuditLab/pull/27), head `acfe434b45a81c11c36b1932a8bf4d333ae5f9d9`. Автор описывает живое демо с генерируемыми компаниями и CLI crosscheck. Здесь проверяли существующий CLI на `a301b16`. Приёмка финального архива независимым человеком ещё требуется.

## Архивная приёмка

Локально проверялись исходники, архиватор и описанный выше публичный путь. Релизный агент зафиксировал 24 теста image/evidence, два одинаковых исходных архива и отдельный host-only rehearsal. Строгий прогон на Windows остановился на проверках для Linux/Python 3.11 и сбое локального теста таймаута. Историческая CI-приёмка делалась с Linux/Docker. [Полный локальный протокол, команды, hashes и условия CI](../agents/release-reproducibility.md)

| Доказательство | Что измерено |
|---|---|
| [RC2, run 44606305454](https://github.com/Mustiks07/AuditLab/actions/runs/44606305454), `0791995d2d8ff3d15d5043197fc6079be01e6a6f` | 256 тестов и 20/80/20 на classic 28.0.4 и containerd 29.0.1. Payload 314 973 218 B |
| [CI run 44728718824](https://github.com/Mustiks07/AuditLab/actions/runs/44728718824), `8fe202ca74178163b42e5f5da87eab4a61bf1a7a` | 274 теста и 20/80/20 на обоих хранилищах. Payload 315 108 569 B. Internal checker 0, strict verify 2 |
| Архивы отчётов этого CI | [Classic `14624888783`](https://github.com/Mustiks07/AuditLab/actions/runs/44728718824/artifacts/14624888783), SHA-256 `b1961d84b229789f3b9ca661ebbbd41494a3697032fd8a822b4545151e702649`. [Containerd `14626260153`](https://github.com/Mustiks07/AuditLab/actions/runs/44728718824/artifacts/14626260153), SHA-256 `6f7436995f72c1d547495dd26769aa5a4d06ef5b8699f1e63903a6745f67e6fb`. API Actions подтверждает истечение обоих 18.02.2027 в 16:34:34 по времени Астаны |

20/80/20 относится к внутренним снимкам, четырём запускам на каждый и преобразованным парам. Полная таблица различает доказательства прошлых сборок, невыполненный новый холодный запуск, официальный пул и приёмку получателем. [Матрица](../REQUIREMENTS.md)
