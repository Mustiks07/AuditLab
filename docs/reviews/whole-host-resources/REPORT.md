# OPS-01: память всей машины для опубликованного RC3

**Вывод: подтверждено для всей гостевой Linux-системы WSL2 VM.**
- Параметры VM: 2 vCPU; выделено 3800 MiB = 3 984 588 800 B; MemTotal гостя 3 803 185 152 B; swap 0; обычный Docker Engine с хранилищем containerd.
- Полная проверка опубликованного RC3 по [HANDOFF](../../demo/HANDOFF.md) прошла с документированными кодами возврата.
- За всё время записи — 0 OOM kill, swap не использовался, давление памяти 0.
- Сэмплированный пик занятой памяти всей гостевой ОС — **848 486 400 B** (22,3 % MemTotal).

Решение интегратора по среде ([PR #32](https://github.com/Mustiks07/AuditLab/pull/32)): SPEC §14/§15 требует чистую машину и целевые ресурсы, но не запрещает Linux-гостя WSL2, поэтому этот прогон принимается как результат на Linux WSL2 VM. Отдельная VM вне Windows обязательным условием не является.

**Границы:**
- Этим запуском **не измерены**: Windows-хост и его `vmmem`, Linux без виртуализации (bare-metal), другие конфигурации Docker (например, классическое хранилище `overlay2`).
- Это **не** новая офлайн-проверка генератора 1.6 и **не** официальная приёмка организаторов.

| Что | Значение |
|---|---|
| Кандидат | [v0.1.0-rc.3](https://github.com/Mustiks07/AuditLab/releases/tag/v0.1.0-rc.3), тег → `b7a1857a51bf47bb36e42699988b38b4d897fa1f` |
| Архив | `AuditLab-v0.1.0-rc.3-offline.zip`, скачан с релиза. `sha256sum` = `a5363412f4509b7ae39aa21aa9eee27154e2af5357347f7d02d7dc31ddb52b81`, `sha256sum -c` → OK |
| База ветки | `main` `daf1fc64df37651e14801e0286b9741b30820d35`, ветка `review/whole-host-resources` |
| Прогон | 2026-11-20, 15:39–15:55 UTC; [logs/2026-11-20-wsl2-rc3/](logs/2026-11-20-wsl2-rc3/) |
| Код RC3 | не менялся; результаты относятся именно к архиву RC3 |

## 1. Машина

Хост — Windows 11 (10.0.26200.9974), 16 407 719 936 B RAM, 8 логических CPU, WSL 3.0.5.0.
VM задана в `%USERPROFILE%\.wslconfig`: `processors=2`, `memory=3800MB`, `swap=0`. До проверки файла не было.

Единица `3800MB` в WSL — MiB: гость видит MemTotal 3 803 185 152 B, это больше 3 800 000 000. Значит, выделено 3800 × 1 048 576 = **3 984 588 800 B ≤ 4 000 000 000 B**. Это не 4 GiB (4 294 967 296 B).

| Свойство | Факт ([environment/](logs/2026-11-20-wsl2-rc3/environment/), [qualification.json](logs/2026-11-20-wsl2-rc3/qualification.json)) |
|---|---|
| ОС | Ubuntu 26.04.1 LTS, ядро `6.18.58.1-microsoft-standard-WSL2`, x86_64, `systemd-detect-virt` = `wsl` |
| CPU | 2 (affinity `0,1`; Docker NCPU 2) |
| Память | MemTotal 3 803 185 152 B; SwapTotal 0 |
| Python | 3.11.18 (`/usr/bin/python3.11`); внутри образа RC3 — 3.11.14 |
| Docker | Engine 29.1.3 (`docker.io` Ubuntu), хранилище **containerd** (`overlayfs`, `io.containerd.snapshotter.v1`), cgroup v2, драйвер systemd |
| Квалификация | 7 из 7 условий |
| Образы проекта до загрузки | отсутствовали по config ID, OCI digest и тегу всех трёх ролей ([preload-images.json](logs/2026-11-20-wsl2-rc3/preload-images.json)); чужих образов и контейнеров не было |

Prerequisites поставлены до проверки скриптом [`prepare_wsl2.sh`](prepare_wsl2.sh): Docker Engine, Python 3.11, `unzip`, `curl`. Пакеты проекта не устанавливались, образы не собирались, pull не было.

## 2. Шаги HANDOFF и коды

| Шаг | Команда | Код | Длительность, с | Ожидание |
|---|---|---:|---:|---|
| Простой | запись памяти без нагрузки | 0 | 30,0 | базовая линия |
| 1 | `python3.11 offline.py check` | **0** | 0,5 | 0; «Bundle hashes verified; source b7a1857…» |
| 3 | `python3.11 offline.py verify --suite rehearsal --output-root ./results` | **2** | 560,3 | 2 — строгая приёмка неполная |
| 5 | `python3.11 offline.py image --role runtime` | **0** | 0,8 | 0; `sha256:bc52a5ec…` |
| 4 | `python3.11 -I source/tools/check_rehearsal_evidence.py RESULTS --bundle . --output …` | **0** | 0,3 | 0, `internal_rehearsal_passed`, `failures: []`, `missing: []` |
| §8 | `docker ps --all --filter label=org.auditlab.launch=<launch_id>` | 0, пусто | 0,2 | контейнеров запуска не осталось |
| §6 | demo: `run.py demo` в контейнере, `curl /demo/health` | **0** | 6,1 | `{"ok":true,"runs":6,"static":true}` |
| §6 | `python -m auditlab.demo … crosscheck --live` | **0** | 1,1 | «Проверено запусков: 6; расхождений: 0» |
| §6 | `python -m auditlab.demo … replay --run <id> --check` | **0** | 1,0 | наблюдения и оценка совпали |

**Код 2 репетиции разобран, а не принят на веру.**
- `verify/report.json`: `status=incomplete`, commit `b7a1857…`, Python 3.11.14.
- Наборы: 3 + 87 + 50 + 32 + 96 + 34 + 4 + 41 = **347 тестов**, все `passed`.
- Матрица: **22 passed, 2 not_checked** — ровно OPS-01 и OPS-02.
- `launch.json`: `status=incomplete`, `exit_code=2`, `cleanup.status=passed`, `remaining_containers=0`.
- `controller-resources.json`: `status=measured`, лимит 1 610 612 736 B, пик контроллера 227 999 744 B.

Сводка репетиции совпадает с опубликованной для RC3:

| Политика | planned / measured | TP / FP / FN | Средняя награда |
|---|---|---:|---:|
| reconcile | 20 / 20, complete | 38 / 0 / 0 | 0,9512 |
| random | 20 / 20, complete | 0 / 0 / 38 | −0,5035 |
| flag_all | 20 / 20, complete | 0 / 600 / 38 | −1 |
| oracle (доверенный) | 20 / 20, complete | 38 / 0 / 0 | 1 |

**`docker events`** ([docker-events.log](logs/2026-11-20-wsl2-rc3/docker-events.log)): 1023 события.
- `image load` — 3, **`image pull` — 0**.
- 110 контейнеров создано и уничтожено.
- `network connect` — 109 к `none` и 1 к `bridge` (контейнер demo с портом на `127.0.0.1`), как в HANDOFF.
- Событий `oom` нет.

## 3. Память всей гостевой ОС

Метод: [`collect_host_memory.py`](collect_host_memory.py) внутри VM.
- Интервал 0,5 с, **1215 сэмплов за 607,0 с**: от простоя до остановки demo, весь прогон, кроме скачивания и распаковки.
- Максимальный разрыв между сэмплами — 0,505 с.
- Источники: `/proc/meminfo`, `/proc/vmstat`, `/proc/pressure/memory`, cgroup v2 и `VmRSS`/`VmHWM` демонов.
- Сводка: [memory/summary.json](logs/2026-11-20-wsl2-rc3/memory/summary.json), [memory/table.md](logs/2026-11-20-wsl2-rc3/memory/table.md); сырые данные: [memory/samples.jsonl](logs/2026-11-20-wsl2-rc3/memory/samples.jsonl).

| Показатель всей гостевой ОС | Байт | Вид |
|---|---:|---|
| MemTotal | 3 803 185 152 | — |
| Пик used = MemTotal − MemAvailable | **848 486 400** | sampled, t = 271 с (репетиция) |
| Минимум MemAvailable | **2 954 698 752** | sampled, там же |
| Пик used вместе с кешем = MemTotal − MemFree | 3 068 125 184 | sampled; рост в основном за счёт кеша страниц образов и данных, ядро освобождает его по требованию |
| Пик swap used | 0 | SwapTotal 0 |
| PSI memory some / full avg10 | 0,00 / 0,00 | sampled максимум |
| OOM kill: `/proc/vmstat` | +0 | счётчик за всю запись |
| OOM kill: `system.slice`, `user.slice`, `init.scope`, `docker.service`, `containerd.service`, все 110 контейнеров | +0 | `memory.events` |

| Шаг | Пик used, sampled | Пик с кешем, sampled |
|---|---:|---:|
| Простой (30 с) | 501 436 416 | 2 015 608 832 |
| Репетиция (560 с) | 848 486 400 | 3 068 125 184 |
| demo, crosscheck, replay | до 725 995 520 | до 3 047 211 008 |

**Демоны и cgroup — непрерывные пики, каждый только своего объекта:**

| Объект | Байт | Примечание |
|---|---:|---|
| `system.slice` memory.peak | 1 475 297 280 | включает кеш страниц, отнесённый к этой cgroup |
| `containerd.service` memory.peak | 808 677 376 | в том числе распаковка образов и её кеш |
| `docker.service` memory.peak | 173 580 288 | |
| `dockerd` VmHWM | 142 909 440 | один процесс |
| `containerd` VmHWM | 72 925 184 | один процесс |
| `containerd-shim*` VmHWM | 12 091 392 | максимум одного процесса из до 4; сумма RSS (sampled) 43 380 736 |

**Контейнеры — отдельно, не складываются в память хоста:**
- 110 контейнеров. Последний прочитанный `memory.peak`: контроллер — 227 999 744 B, demo — 157 409 280 B, verifier — до 128 090 112 B, медиана — 20 549 632 B.
- Лимиты:
  - 103 контейнера — 1 610 612 736 B;
  - 5 тестовых контейнеров проверки изоляции — 64 MiB;
  - demo — 2 GiB;
  - один мой контейнер `crosscheck`/`replay`, запущенный с явным `--memory 1536m`, попал лишь в один сэмпл, до записи лимита.
- OOM kill — 0.

**Границы вывода:**
1. Пик памяти всей ОС **сэмплированный** (0,5 с). Это не непрерывный максимум: всплеск короче интервала мог быть пропущен. Запас — минимум MemAvailable 2,95 GB.
2. `memory.peak` и `VmHWM` — непрерывные пики отдельных cgroup и процессов. Разные объекты достигают пика в разное время, поэтому их сумма **не** равна памяти машины и не приводится.
3. `memory.peak` контейнера пропадает вместе с его cgroup. Сохранено последнее прочитанное значение; у короткоживущих контейнеров оно может быть ниже финального пика.
4. Измерена память **гостевой ОС** VM. Память, которую Windows-хост тратит на саму VM (`vmmem`), и сам хост не измерялись.
5. Сборщик (Python, ≈ 15–20 MB) работал внутри VM, и его память входит в показатели всей ОС.
6. Скачивание и распаковка архива шли до начала записи.

## 4. Найденные дефекты

**В RC3 дефектов не обнаружено.**

В инструментах проверки найдены и исправлены три недочёта, все с регрессиями в [`tests/runner/test_whole_host_tools.py`](../../../tests/runner/test_whole_host_tools.py).

**1. Судья допускал ложный `confirmed`** (замечание интегратора в PR #32, head `a25e1bd`). На копиях этого каталога четыре отрицательных контроля проходили как подтверждённые:
- обнулённые измерения в `memory/summary.json`;
- MemTotal 16 000 000 000 B при пике 8 000 000 000 B;
- матрица приёмки из одной группы OPS-02;
- evidence `{"status":"not_checked"}`.

Теперь `judge_review.py`:
- **квалификация** — заново выводит её из фактов `qualification.json`. Флаг `qualified` без подтверждающих фактов — провал;
- **сводка** — пересчитывает её из `memory/samples.jsonl` и сравнивает с `summary.json`. Расхождение — провал, отсутствие сырых сэмплов — «не проверено»;
- **числа памяти** — требует MemTotal, пик, минимум MemAvailable и счётчики OOM. Сверяет MemTotal с лимитом 4 000 000 000 B и с квалификацией; swap или превышение — провал;
- **покрытие** — требует, чтобы каждый шаг HANDOFF был окружён сэмплами без разрыва больше max(2 с, 4 интервала). Иначе «не проверено» — например, если запись остановилась до `rehearsal`;
- **матрица приёмки** — требует ровно 24 группы SPEC §14: 22 passed, OPS-01 и OPS-02 not_checked, commit `b7a1857…`. Недостающие группы — «не проверено», проваленные, неизвестные или лишние непроверенные — провал;
- **evidence** — требует `status=internal_rehearsal_passed`, `exit_code=0`, пустые `failures` и `missing`, `source_revision=b7a1857…` и счётчики 20/80/20/60/40. Отсутствие полей — «не проверено», `failures` или чужой SHA — провал.

Регрессии покрывают все четыре контроля, запись только до `rehearsal`, разрыв внутри шага, OOM в сырых данных, недостоверный флаг квалификации, чужой SHA, `image pull`, проваленную или лишнюю непроверенную группу, неверный код и пропущенный шаг. Со старым судьёй (`a25e1bd`) эти тесты дают 7 провалов и 1 ошибку; с новым проходят все 17. Исходный прогон по-прежнему `confirmed`.

**2. Многострочные события.** `docker events` в текстовом формате переносит многострочные команды `exec` (замер ресурсов RC3) на следующие строки. Поиск `image pull` по строкам мог дать ложный провал, но не ложный успех. Теперь сценарий пишет `{{json .}}`, а судья разбирает и JSON, и старый формат по признаку «строка события начинается с отметки времени».
**3. Лимит из первого сэмпла.** Сводка брала лимит контейнера из первого сэмпла. Сборщик успевал прочитать новую cgroup до записи `memory.max`: внутри был только `runc init`, около 3 МБ. Поэтому 9 контейнеров выглядели безлимитными, хотя у восьми со следующего сэмпла читался лимит 1 610 612 736 B, а девятый (`crosscheck`/`replay`) запускался с явным `--memory 1536m`. Теперь лимит — последнее наблюдённое значение, а число чтений без лимита выводится отдельно. Затронута только подпись в сводке; вердикт и показатели памяти не менялись.

Сводка и вердикт пересчитаны исправленными инструментами по тем же сырым данным. Исходные файлы сохранены рядом (`*.original.json`). Вердикт в обоих случаях — `confirmed`.

## 5. Инструменты

| Файл | Назначение |
|---|---|
| [`collect_host_memory.py`](collect_host_memory.py) | Сэмплер всей гостевой ОС; stdlib, без root |
| [`summarize_host_memory.py`](summarize_host_memory.py) | Сводка с явными границами вывода |
| [`judge_review.py`](judge_review.py) | Вердикт `confirmed` / `failed` / `not_checked` (коды 0 / 1 / 2) по сохранённым файлам |
| [`run_review.sh`](run_review.sh) | Квалификация машины, хеш, образы до загрузки, шаги HANDOFF, `docker events`, запись памяти, вердикт |
| [`prepare_wsl2.sh`](prepare_wsl2.sh) | Prerequisites в WSL2 (Docker Engine, Python 3.11); выполняется до проверки |

Вердикт `confirmed` требует одновременно:
- профиль машины, заново выведенный из фактов, совпадает с MemTotal в сэмплах; образы проекта отсутствовали до загрузки; хеш архива совпал;
- `check` = 0 с SHA `b7a1857…`;
- `rehearsal` = 2 при точной матрице из 24 групп: 22 passed, OPS-01/OPS-02 not_checked;
- `image` = 0;
- evidence `internal_rehearsal_passed` со всеми полями, тем же SHA и счётчиками 20/80/20/60/40;
- контейнеров запуска не осталось;
- demo `ok:true`, `crosscheck --live` = 0, `replay --check` = 0;
- нет `image pull` и OOM kill, нет swap;
- сводка памяти совпадает с пересчётом из сырых сэмплов, и каждый шаг покрыт сэмплами.

Отсутствие данных даёт «не проверено», доказанное нарушение — «провалено».

Повторить на любой подходящей машине (2 vCPU, RAM ≤ 4 000 000 000 B, swap off, Python 3.11, Docker Engine, cgroup v2):

```sh
git clone https://github.com/Mustiks07/AuditLab && cd AuditLab && git switch review/whole-host-resources
bash docs/reviews/whole-host-resources/run_review.sh --work ~/ops01 --download
echo "exit=$?"   # 0 confirmed, 1 failed, 2 not_checked, 3 machine does not qualify, 4 project images already present
```

Каталог [logs/2026-11-20-wsl2-unqualified/](logs/2026-11-20-wsl2-unqualified/) хранит первую попытку на той же машине до настройки VM (8 CPU, 7,9 GB, swap, без Docker и Python 3.11): `run_review.sh` вышел с кодом 3 до скачивания архива.

## 6. Что остаётся

- **Не измерено этим запуском:** память Windows-хоста и `vmmem`, Linux без виртуализации, другие конфигурации Docker. Сценарий для них готов (`run_review.sh`).
- **OPS-02** (независимый человек по HANDOFF), **официальный адаптер организаторов** и **новая офлайн-проверка генератора 1.6** этим отчётом не подтверждаются.
