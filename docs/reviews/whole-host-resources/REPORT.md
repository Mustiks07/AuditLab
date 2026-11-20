# OPS-01: память всей машины для опубликованного RC3

**Статус: не проверено.** Подходящей машины (Linux x86_64, 2 CPU, не более 4 000 000 000 байт RAM, без swap, Python 3.11, локальный Docker Engine) в распоряжении проверяющего не было. Проверка не подменялась контейнером с memory limit на большом хосте. Подготовлены и испытаны сборщик измерений, сценарий проверки по HANDOFF и правило вердикта. Ниже — фактическое состояние доступной машины и точная инструкция для подходящей.

| Что | Значение |
|---|---|
| Проверяемый кандидат | [v0.1.0-rc.3](https://github.com/Mustiks07/AuditLab/releases/tag/v0.1.0-rc.3), тег → `b7a1857a51bf47bb36e42699988b38b4d897fa1f` (сверено `git ls-remote`) |
| Архив | `AuditLab-v0.1.0-rc.3-offline.zip`, 330 616 243 B, SHA-256 `a5363412f4509b7ae39aa21aa9eee27154e2af5357347f7d02d7dc31ddb52b81`. Тот же digest показывает страница ассетов релиза. Архив **не скачивался**: машина не прошла квалификацию до шага загрузки |
| База ветки | `main` `daf1fc64df37651e14801e0286b9741b30820d35`, ветка `review/whole-host-resources` |
| Изменения кода RC3 | нет: добавлены только инструменты проверки, журналы и тесты инструментов |

## 1. Фактически доступная машина

Журнал: [logs/2026-11-20-wsl2-unqualified/](logs/2026-11-20-wsl2-unqualified/). `run_review.sh` записал окружение и квалификацию и завершился с **кодом 3** («машина не соответствует профилю») до скачивания архива.

| Требование | Факт | Итог |
|---|---|---|
| Linux x86_64 | WSL2 на Windows 11, Ubuntu 26.04.1 LTS, ядро `6.18.58.1-microsoft-standard-WSL2`, `systemd-detect-virt` = `wsl` | VM есть |
| 2 CPU | 8 (affinity `0-7`) | **нет** |
| ≤ 4 000 000 000 B RAM | MemTotal 7 945 748 480 B | **нет** |
| swap отключён | SwapTotal 2 147 483 648 B | **нет** |
| Python 3.11 | 3.14.4 (3.11 отсутствует) | **нет** |
| Docker Engine, cgroup v2 | cgroup2fs есть, Docker не установлен | **нет** |
| Установка пакетов | sudo требует пароль пользователя | вне полномочий проверяющего |

Хост Windows: 16 407 719 936 B, 8 логических CPU; Docker Desktop нет. Изменять глобальную конфигурацию WSL пользователя и вводить пароль sudo проверяющий не стал.

## 2. Что и как измеряется

Все скрипты используют только стандартную библиотеку Python 3.11 и не требуют root.

| Файл | Назначение |
|---|---|
| [`collect_host_memory.py`](collect_host_memory.py) | Сэмплер внутри гостевой ОС: одна JSON-строка каждые `--interval` с (по умолчанию 0,5) |
| [`summarize_host_memory.py`](summarize_host_memory.py) | Сводка: `summary.json` и `table.md`, пики по всей записи и по каждому шагу |
| [`judge_review.py`](judge_review.py) | Вердикт `confirmed` / `failed` / `not_checked` только по сохранённым файлам; коды 0 / 1 / 2 |
| [`run_review.sh`](run_review.sh) | Полная проверка по HANDOFF §§1–8 с квалификацией машины, хешем, журналами и вердиктом |

Источники и их смысл:

| Показатель | Источник | Что означает |
|---|---|---|
| Память всей гостевой ОС | `/proc/meminfo` (MemTotal, MemFree, MemAvailable, Cached, Slab, Shmem, Swap…) | Ядро, кеш страниц, демоны, контейнеры. `used = MemTotal − MemAvailable`; отдельно `used с кешем = MemTotal − MemFree`. **Только сэмплы** |
| Docker daemon и containerd | cgroup v2 `system.slice/docker.service`, `containerd.service`: `memory.current`, `memory.peak`; процессы `dockerd`, `containerd`, `containerd-shim*`: `VmRSS`, `VmHWM` | `memory.peak` и `VmHWM` — **непрерывные** пики ядра, но каждый только своей cgroup или процесса |
| Контейнеры (отдельно) | `system.slice/docker-<id>.scope` или `/sys/fs/cgroup/docker/<id>`: `memory.current`, `memory.peak`, `memory.max`, `memory.events` | Пики контейнеров **не складываются** в память хоста |
| OOM | `/proc/vmstat oom_kill` и `memory.events oom_kill` каждой cgroup | Любой прирост счётчика — OOM-событие |
| Давление памяти | `/proc/pressure/memory` (PSI some/full avg10) | Признак нехватки до OOM |
| Привязка к шагам | `phases.jsonl`: начало и конец каждого шага в `CLOCK_MONOTONIC`, общий для всех процессов | Пики по шагам: базовый простой 30 с, check, rehearsal, image, evidence checker, demo, crosscheck, replay |

**Границы вывода** (они же записываются в каждую сводку):

1. Пик памяти всей ОС **сэмплированный**: всплеск короче интервала может быть пропущен. Это не непрерывный максимум. Сводка указывает фактический интервал и максимальный разрыв между сэмплами; разрыв больше 10 интервалов переводит вердикт в «не проверено».
2. `memory.peak` cgroup и `VmHWM` процесса — непрерывные максимумы, но разные cgroup достигают пика в разное время. Их **сумма не равна** памяти всего хоста и в отчёте не приводится.
3. `memory.peak` контейнера пропадает вместе с его cgroup. Сохраняется последнее прочитанное значение, оно может быть ниже финального пика.
4. MemTotal — взгляд гостевого ядра, он меньше RAM, выделенной гипервизором. Выделенный объём записывается из конфигурации гипервизора отдельно.
5. Контейнерные лимиты RC3 (контроллер и sibling по 1536 MiB, суммарно 3 GiB) — ограничения, а не измерение машины.

**Правило вердикта** `confirmed` требует одновременно:
- машина прошла квалификацию (`qualification.json`);
- образы проекта отсутствовали до загрузки;
- хеш архива совпал;
- `check` = 0, `rehearsal` = 2 и при этом единственные непроверенные группы — OPS-01 и OPS-02, без проваленных;
- `image` = 0, evidence checker = 0 без `failures` и `missing`;
- после запуска не осталось контейнеров с меткой этого запуска;
- demo health `ok:true`, `crosscheck --live` = 0, `replay --check` = 0;
- в событиях Docker нет `image pull`;
- нет ни одного OOM kill, swap выключен, сэмплирование непрерывно.

Любое отклонение кода — «провалено»; отсутствие шага или данных — «не проверено». Правило покрыто тестами `tests/runner/test_whole_host_tools.py`.

## 3. Как выполнить проверку на подходящей машине

**Машина:** отдельная VM или физический хост Linux x86_64.
- 2 vCPU.
- RAM не более 4 000 000 000 байт: это 3814 MiB или меньше, а не 4 GiB (4 294 967 296 B). Например, 3800 MiB = 3 984 588 800 B. Запишите выделенный объём из настроек гипервизора.
- swap отключён: `sudo swapoff -a` и закомментированный swap в `/etc/fstab`; затем `swapon --show` должен быть пуст.
- cgroup v2, Python 3.11, Docker Engine с доступом пользователя к `/var/run/docker.sock`.

Всё это устанавливается **до** проверки, не во время неё. Чужие образы и контейнеры не удаляются.

```sh
git clone https://github.com/Mustiks07/AuditLab && cd AuditLab && git switch review/whole-host-resources
bash docs/reviews/whole-host-resources/run_review.sh --work ~/ops01 --download
echo "exit=$?"     # 0 confirmed, 1 failed, 2 not_checked, 3 unqualified machine, 4 project images already present
```

Скрипт:
1. записывает окружение и квалифицирует машину;
2. скачивает ZIP и `SHA256SUMS`, проверяет оба хеша и распаковывает;
3. проверяет отсутствие образов проекта по config ID, OCI digest и тегу;
4. запускает сборщик и `docker events`;
5. выполняет 30 с простоя, затем `check`, `verify --suite rehearsal`, `image --role runtime`, evidence checker, проверку контейнеров запуска, demo (`/demo/health`, `/demo/runs`), `crosscheck --live` и `replay --check`;
6. останавливает только свой контейнер demo и выносит вердикт.

Результаты: `~/ops01/review-<UTC>/`, в том числе `result.json`, `memory/summary.json`, `memory/table.md`, `steps/*.out`, `phases.jsonl`, `docker-events.log`. Каталоги верните в эту ветку как `logs/<дата>-<машина>/`, кроме архива и `bundle/images.tar`.

**Вариант WSL2 на этой же машине** — допущение, которое должен принять интегратор:
- В `%USERPROFILE%\.wslconfig` задаётся `[wsl2] processors=2, memory=3800MB, swap=0`, затем `wsl --shutdown`. Это меняет все дистрибутивы WSL пользователя.
- Внутри Ubuntu нужно поставить Docker Engine и Python 3.11; для этого требуется пароль sudo.
- WSL2 — VM Hyper-V со своим ядром, и лимит RAM применяется ко всей гостевой ОС. Но HANDOFF не заявляет Windows-хосты, а память WSL2 динамически возвращается Windows. Результат допустимо подписывать только как «WSL2 VM на Windows 11, 2 vCPU, N байт», не как «отдельный Linux-хост».

## 4. Фактические результаты

| Проверка | Результат |
|---|---|
| Квалификация доступной машины | 1 из 7 условий (x86_64); `run_review.sh` exit 3 |
| Скачивание и хеш архива RC3 | не выполнялись (после неуспешной квалификации) |
| check / rehearsal / evidence checker / demo / replay | не выполнялись |
| Память всей гостевой ОС на RC3 | **не измерена** |
| OOM-события на RC3 | **не измерены** |
| Сборщик на реальном Linux (WSL2, без нагрузки RC3) | 14 сэмплов за 3,003 с при интервале 0,25 с, максимальный разрыв 0,251 с; MemTotal, пики cgroup `system.slice`/`user.slice` и счётчики OOM прочитаны; запись по шагу работает ([collector-smoke/](logs/2026-11-20-wsl2-unqualified/collector-smoke/)). Это проверка инструмента, не RC3 |
| Тесты инструментов | `tests/runner/test_whole_host_tools.py`: 8/8 в Linux (WSL2, Python 3.14.4, включая живой сборщик); 7/7 + 1 «не проверено» на Windows |

Дефектов RC3 не обнаружено, потому что RC3 не исполнялся. Утверждение «весь хост уложился в 4 GB» не делается.

## 5. Воспроизведение проверки инструментов

```sh
python3 -m unittest discover -s tests/runner -p test_whole_host_tools.py -v
python3 docs/reviews/whole-host-resources/collect_host_memory.py --out /tmp/m --interval 0.25 --max-seconds 3
python3 docs/reviews/whole-host-resources/summarize_host_memory.py /tmp/m --markdown /tmp/m/table.md
bash docs/reviews/whole-host-resources/run_review.sh --work /tmp/ops01-gate   # на неподходящей машине: exit 3
```
