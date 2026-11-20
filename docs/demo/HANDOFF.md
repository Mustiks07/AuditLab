# Передача офлайн-комплекта независимому получателю

Документ для человека, который получает архив AuditLab и проверяет его **без помощи авторов**. Команды
написаны для архива `AuditLab-<версия>-offline.zip`. Подставьте версию, которую вам передали. Фактические
результаты агентской проверки опубликованного `v0.1.0-rc.2` — в [RC2_AGENT_CHECK.md](RC2_AGENT_CHECK.md).

> **Какой архив проверять.** `v0.1.0-rc.2` — опубликованный исторический кандидат. Блокер
> [#23](https://github.com/Mustiks07/AuditLab/issues/23) (финализация runner при глубоко вложенном JSON)
> в нём не исправлен. Это не исправленный финальный кандидат. Финальную приёмку выполняйте на новом архиве,
> который выдаст интегратор после исправления #23; порядок команд тот же.

Что нельзя делать во время проверки: устанавливать пакеты проекта (`pip install`), собирать образы, скачивать
образы из registry, подменять комплект исходным checkout. Всё нужное лежит в архиве.

## 1. Требования к машине

| Что | Требование | Как проверить |
|---|---|---|
| ОС | Linux x86_64. Windows и Docker Desktop этим комплектом не заявлены | `uname -srm` |
| Python | **Ровно 3.11**: launcher отказывает другим версиям (`Offline launch failed: launcher requires Python 3.11`). В Ubuntu 24.04 по умолчанию 3.12; Python 3.11 нужно установить заранее, до офлайн-проверки | `python3.11 --version` |
| Docker | Локальный Docker Engine с Linux-контейнерами и сокетом `/var/run/docker.sock`. Удалённые contexts и `DOCKER_HOST` launcher игнорирует. Пользователь должен иметь доступ к сокету | `docker version`, `docker info` |
| cgroup | **v2** для подтверждения лимитов ресурсов. На v1 проверка функциональная, ресурсы остаются «не проверено», коды 2 | `docker info --format '{{.CgroupVersion}}'` |
| CPU | Минимум 2 CPU в affinity процесса (берутся первые два, или явно `--cpu-set 0,1`) | `nproc` |
| Диск | Около 0,9 ГБ: архив 315 МБ, распакованный комплект 315 МБ, загруженные образы, результаты (быстрый запуск около 1,6 МБ, репетиция около 50 МБ) | `df -h .` |
| Сеть | Не нужна для проверки. Все контейнеры проверки запускаются с `--network none`. Демо использует bridge только для порта на `127.0.0.1` | |

## 2. Две конфигурации Docker

Комплекты начиная с `v0.1.0-rc.2` работают на обоих хранилищах образов, переключать демон не нужно. Узнайте
своё хранилище и запишите вывод в журнал:

```sh
docker info --format '{{.Driver}} {{.DriverStatus}} cgroup={{.CgroupVersion}}'
```

| Вывод содержит | Хранилище | Как адресуется образ |
|---|---|---|
| `overlay2` | классическое | config ID из `manifest.json` (`images.<роль>.id`) |
| `io.containerd.snapshotter.v1` (Driver `overlayfs`) | containerd image store, по умолчанию в **новых** установках Docker Engine 29 | OCI manifest digest; launcher выбирает проверенный ID сам |

На машине, обновлённой со старого Docker, с уже существующими образами классического хранилища, Docker 29
может остаться на `overlay2`. Хранилище включается явно настройкой
`{"features": {"containerd-snapshotter": true}}` (или `false`) в `/etc/docker/daemon.json` с перезапуском
демона. Образы другого хранилища после переключения не видны. Не меняйте рабочий демон с чужими
контейнерами только ради проверки.

Для `demo` и `replay` всегда берите ID командой `python3.11 offline.py image --role runtime`, а не из
`manifest.json`: на containerd это другой идентификатор.

## 3. Получение и проверка хеша архива

Скачайте в новый пустой каталог архив и файл контрольных сумм. Для `v0.1.0-rc.2`:
`AuditLab-v0.1.0-rc.2-offline.zip` и `AuditLab-v0.1.0-rc.2-SHA256SUMS.txt` со страницы релиза.

```sh
sha256sum -c AuditLab-v0.1.0-rc.2-SHA256SUMS.txt --ignore-missing
```

Ожидается `AuditLab-v0.1.0-rc.2-offline.zip: OK`. Для RC2 SHA-256 архива —
`54850f508b65d48be8e7b2aa6b1404d4a35cc0bdbc02f83c767afa06c16b62c9`. Любой другой результат — остановитесь:
архив повреждён или подменён.

Архив **не содержит общего каталога**: шесть файлов лежат в корне. Распаковывайте в новый пустой каталог:

```sh
mkdir bundle && cd bundle && unzip ../AuditLab-v0.1.0-rc.2-offline.zip
```

Ожидаемые файлы: `offline.py`, `images.tar`, `source.tar.gz`, `licenses.json`, `manifest.json`, `SHA256SUMS`.

## 4. Первый офлайн-запуск

Все команды — из каталога `bundle`. Запишите в журнал вывод, код возврата (`echo $?`) и длительность.

| № | Команда | Ожидаемый вывод и код |
|---|---|---|
| 1 | `python3.11 offline.py check` | `Bundle hashes verified; source <SHA>`, **exit 0**, меньше 1 с. Docker не нужен. Запишите SHA; у RC2 это `0791995d2d8ff3d15d5043197fc6079be01e6a6f` |
| 2 | `python3.11 offline.py verify --output-root ./results` | Три строки `Loaded image: auditlab-…:<sha12>`, затем 8 наборов «пройдено». У RC2 тестов 256: 3 + 56 + 31 + 32 + 68 + 32 + 4 + 30. Код **2** (`status=incomplete`): полная приёмка SPEC открыта. Около 2 минут |
| 3 | `python3.11 offline.py verify --suite rehearsal --output-root ./results` | Те же наборы и репетиция: 20 снимков, 80 попыток, 20 пар. Код **2**. Около 3,5 минуты на 2 CPU |
| 4 | evidence checker (ниже) | cgroup v2: `internal_rehearsal_passed`, `failures: []`, `missing: []`, **exit 0**. cgroup v1: `not_checked`, `failures: []`, 102 строки в `missing`, **exit 2** |
| 5 | `python3.11 offline.py image --role runtime` | Одна строка `sha256:…`, **exit 0** |

Evidence checker, где `RESULTS` — каталог `results/verify-…` шага 3:

```sh
mkdir -p source && tar -xzf source.tar.gz -C source
```

```sh
python3.11 -I source/tools/check_rehearsal_evidence.py RESULTS --bundle . --output evidence-summary.json
```

**Внимание к двум кодам.** В конце шагов 2 и 3 внутренний отчёт печатает
`Report: /results/verify/report.json; status=incomplete; exit=0`, а сам `offline.py` возвращает 2. Это не
противоречие. Внутренний `exit=0` значит «все исполненные проверки прошли». Launcher добавляет «полная
приёмка и ресурсы не подтверждены» и возвращает 2. Коды launcher: 0 — доступные проверки прошли и ресурсы
подтверждены; 1 — ошибка (комплект, Docker, тест); 2 — неполная обязательная приёмка; 130 — прерывание.

Во втором терминале на время шагов 2–3 запишите события Docker:

```sh
docker events --format '{{.Type}} {{.Action}} {{.Actor.Attributes.name}}'
```

Ожидается: ни одного `image pull`; события `network connect` только с сетью `none`.

## 5. Ожидаемые файлы результатов

Каждый вызов `verify` создаёт новый `results/verify-<UTC>-<id>/` и никогда не перезаписывает прежние.

| Файл | Что проверить |
|---|---|
| `launch.json` | `status` (`incomplete` / `completed` / `failed` / `interrupted`), `exit_code`, `loaded_images` (три роли, ID и OCI digest), время загрузки образов и общее |
| `controller-resources.json` | Фактические лимиты контроллера; на cgroup v1 `resource_limits_verified` не `true` |
| `verify/report.json` | `status`, `exit_code`, `commit`, `python`, `checks` по наборам; после репетиции — `rehearsal.summary` |
| `verify/report.md` | Человекочитаемая сводка того же отчёта |
| `verify/<набор>.json`, `.log`, `-artifacts/` | ID каждого теста и журналы |
| `verify/rehearsal/` | `attempts/<политика>/internal-NN/final.json` (сдача и оценка), `snapshots/`, `independent/`, `pairs/` |

Ожидаемая сводка репетиции (внутренний набор, не конкурсный балл):

| Политика | TP / FP / FN | Средняя награда |
|---|---:|---:|
| random | 0 / 0 / 38 | −0,5035 |
| reconcile | 38 / 0 / 0 | 0,9512 |
| flag_all | 0 / 600 / 38 | −1 |
| oracle (доверенный) | 38 / 0 / 0 | 1 |

```sh
python3.11 -c "import json,sys; r=json.load(open(sys.argv[1])); [print(p, v['micro']['tp'], v['micro']['fp'], v['micro']['fn'], round(v['mean_reward'],4)) for p,v in r['rehearsal']['summary'].items()]" RESULTS/verify/report.json
```

## 6. Консоль и сценарий

Запуск демо, адрес и сценарий на 3 минуты — [DEFENSE.md](DEFENSE.md). Кратко:

```sh
AUDITLAB_RUNTIME_IMAGE=$(python3.11 offline.py image --role runtime)
mkdir -p demo-results
docker run --rm --pull never --user "$(id -u):$(id -g)" --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 2 --memory 2g \
  --tmpfs /tmp:rw,nosuid,size=1g -p 127.0.0.1:8765:8765 \
  --mount "type=bind,src=$(pwd)/demo-results,dst=/results" \
  "$AUDITLAB_RUNTIME_IMAGE" python run.py demo --host 0.0.0.0 --root /results/demo
```

Откройте `http://127.0.0.1:8765/`; адрес `0.0.0.0` в браузере не открывайте. `curl -s http://127.0.0.1:8765/demo/health`
отвечает `{"ok":true,"runs":N,"static":true}`: в RC2 `N = 3`, в консоли после этого PR `N = 6`. Сверка консоли
с экспортами в контейнере без сети (в кандидате, собранном после этого PR):

```sh
docker run --rm --pull never --network none --read-only --cap-drop ALL --tmpfs /tmp:rw,size=64m \
  --mount "type=bind,src=$(pwd)/demo-results,dst=/results" "$AUDITLAB_RUNTIME_IMAGE" \
  python -m auditlab.demo --root /results/demo crosscheck --live
```

## 7. Диагностика ошибок

Сообщения ниже получены фактически на `v0.1.0-rc.2`.

| Симптом | Причина и что делать |
|---|---|
| `Offline launch failed: launcher requires Python 3.11`, exit 1 | Запущен другой Python. Используйте `python3.11` |
| `Offline launch failed: checksum or size mismatch: licenses.json; no images were loaded`, exit 1 | Файл комплекта повреждён или изменён. Образы не загружались, каталог результатов не создан. Распакуйте архив заново после проверки хеша (раздел 3) |
| `Offline launch failed: saved Docker catalog lost the tagged runtime identity` (или `saved OCI index …`) | Архив без тегированных записей образов, например RC1 на новом launcher. Возьмите архив `v0.1.0-rc.2` или новее |
| `Resource limits are not supported by this daemon; functional checks continue…` | cgroup v1 или демон без делегирования контроллеров. Функциональные проверки идут, итоговые коды 2. Для ресурсной приёмки нужна машина с cgroup v2 |
| `permission denied … /var/run/docker.sock` или `Cannot connect to the Docker daemon` | Демон не запущен или у пользователя нет доступа к сокету. `docker info` должен работать от того же пользователя |
| Порт `8765` занят | Остановите прежний контейнер демо (`docker ps`, `docker stop <name>`) или пробросьте другой локальный порт `-p 127.0.0.1:8766:8765` |
| В консоли `"runs":0` | Каталог `demo-results` недоступен на запись пользователю контейнера. Создайте его от своего пользователя до `docker run` |
| Оценка или наблюдения разошлись в `replay --check` / `crosscheck` | Дефект: сохраните вывод и каталог `demo-results` и сообщите |

## 8. Прерывание и повтор

- **Ctrl+C** в терминале с `verify`: launcher возвращает **130**, `launch.json` получает
  `"status": "interrupted"`, контейнер проверки останавливается вместе с ним. Каталог прерванного запуска
  содержит частичные результаты; не используйте его как доказательство.
- **Повтор:** запустите ту же команду `verify`. Создаётся новый `results/verify-…`, прежние каталоги не
  меняются. Образы уже загружены, загрузка повторяется с проверкой ID и занимает меньше 3 с. На RC2 повтор
  после прерывания вернул 2 за 108 с.
- **Если сигнал получил только процесс launcher** (например, `kill -INT <pid>` из другого терминала, а не
  Ctrl+C), на RC2 launcher сразу пишет `interrupted` и выходит с 130, но контейнер проверки **продолжает
  работать** до конца (около 2 минут). Он дописывает `verify/report.json` в каталог с пометкой `interrupted`.
  Перед повтором проверьте `docker ps` и дождитесь окончания или остановите контейнер
  (`docker stop <name>`). Такой каталог не используйте. Дефект передан владельцу launcher.
- После перезагрузки машины или демона загруженные образы сохраняются. Достаточно снова выполнить `verify`
  или `image`.

## 9. Что вернуть авторам

Журнал по разделам 1–8: версии ОС, Python и Docker, хранилище и cgroup, SHA архива и исходников, три image ID
из `launch.json`, команды, коды возврата и длительности, сводку репетиции, вывод evidence checker и
`docker events`. Пройдите чек-лист [ACCEPTANCE_CHECKLIST.md](ACCEPTANCE_CHECKLIST.md) и добавьте снимки экрана
консоли. Каждое затруднение с инструкцией записывайте дословно. Вывод формулируйте как «независимая
человеческая проверка комплекта … на машине …» без утверждений о конкурсных баллах. До такого отчёта
пункт OPS-02 / UI-01 (#15) остаётся открытым.
