# Передача офлайн-комплекта независимому получателю

Документ для человека, который получает архив AuditLab и проверяет его **без помощи авторов**.

**Опубликованный кандидат:** [v0.1.0-rc.3](https://github.com/Mustiks07/AuditLab/releases/tag/v0.1.0-rc.3),
исходный SHA `b7a1857a51bf47bb36e42699988b38b4d897fa1f`.
Архив `AuditLab-v0.1.0-rc.3-offline.zip`: **330 616 243 B**, SHA-256
`a5363412f4509b7ae39aa21aa9eee27154e2af5357347f7d02d7dc31ddb52b81`.
Публикация и хеши проверены через GitHub; повторите сверку файла после скачивания.

RC2 — исторический кандидат. Его результаты и обнаруженные тогда дефекты сохранены без изменения в
[RC2_AGENT_CHECK.md](RC2_AGENT_CHECK.md). Исправления финализации, генератора, сохранения результатов,
отмены запуска и консоли относятся к новому SHA. Старые измерения к нему не переносятся.
Встроенные в `source.tar.gz` документы зафиксированы вместе с кодом; после публикации актуальную
инструкцию получателя берите со страницы релиза или из
[HANDOFF.md в main](https://github.com/Mustiks07/AuditLab/blob/main/docs/demo/HANDOFF.md).

Что нельзя делать во время проверки: устанавливать пакеты проекта (`pip install`), собирать образы, скачивать
образы из registry, подменять комплект исходным checkout. Всё нужное лежит в архиве.

## 1. Требования к машине

| Что | Требование | Как проверить |
|---|---|---|
| ОС | Linux x86_64. Windows и Docker Desktop этим комплектом не заявлены | `uname -srm` |
| Python | **Ровно 3.11**: launcher отказывает другим версиям (`Offline launch failed: launcher requires Python 3.11`). В Ubuntu 24.04 по умолчанию 3.12; Python 3.11 нужно установить заранее, до офлайн-проверки | `python3.11 --version` |
| Docker | Локальный Docker Engine с Linux-контейнерами и сокетом `/var/run/docker.sock`. Удалённые contexts и `DOCKER_HOST` launcher игнорирует. Пользователь должен иметь доступ к сокету | `docker --host unix:///var/run/docker.sock version`, `docker --host unix:///var/run/docker.sock info` |
| cgroup | **v2** для подтверждения лимитов ресурсов. На v1 проверка функциональная, ресурсы остаются «не проверено», коды 2 | `docker --host unix:///var/run/docker.sock info --format '{{.CgroupVersion}}'` |
| CPU | Минимум 2 CPU в affinity процесса (берутся первые два, или явно `--cpu-set 0,1`) | `nproc` |
| Диск | Место для ZIP, распакованного комплекта, загруженных Docker-слоёв и результатов. Размер ZIP берите со страницы RC3, payload — из `manifest.json`; они не равны суммарному месту на диске | `df -h .`, `docker --host unix:///var/run/docker.sock system df` |
| Сеть | Не нужна для проверки. Все контейнеры проверки запускаются с `--network none`. Демо использует bridge только для порта на `127.0.0.1` | |

Контроллер и один одновременно работающий контейнер политики либо verifier ограничены суммарно **3 GiB**.
Это не измерение памяти всей машины: ОС, Docker daemon и файловый кеш хоста требуют отдельной
проверки. Не записывайте «весь хост уложился в 4 GB» по одним контейнерным лимитам.

## 2. Две конфигурации Docker

Комплекты начиная с `v0.1.0-rc.2` работают на обоих хранилищах образов, переключать демон не нужно. Узнайте
своё хранилище и запишите вывод в журнал:

```sh
docker --host unix:///var/run/docker.sock info --format '{{.Driver}} {{.DriverStatus}} cgroup={{.CgroupVersion}}'
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

Скачайте в новый пустой каталог архив и файл контрольных сумм со страницы RC3.
Скачивание выполняется **до** отключения сети; сам запуск не требует интернета.

```sh
TAG=v0.1.0-rc.3
ZIP="AuditLab-${TAG}-offline.zip"
SUMS="AuditLab-${TAG}-SHA256SUMS.txt"
```

Имена скачанных файлов должны совпадать с `$ZIP` и `$SUMS`. В том же терминале:

```sh
test -f "$ZIP" && sha256sum -c "$SUMS" --ignore-missing
```

Ожидается именно `AuditLab-v0.1.0-rc.3-offline.zip: OK`, exit 0. Отсутствие файла, ошибка проверки
или строка о том, что ни один файл не проверен, не являются успехом. Не используйте checksum RC2.

Архив **не содержит общего каталога**: шесть файлов лежат в корне. Распаковывайте в новый пустой каталог:

```sh
mkdir bundle
unzip "$ZIP" -d bundle
cd bundle
```

Ожидаемые файлы: `offline.py`, `images.tar`, `source.tar.gz`, `licenses.json`, `manifest.json`, `SHA256SUMS`.

## 4. Первый офлайн-запуск

Все команды — из каталога `bundle`. Запишите в журнал вывод, код возврата (`echo $?`) и длительность.

| № | Команда | Ожидаемый вывод и код |
|---|---|---|
| 1 | `python3.11 offline.py check` | Проверка payload и каталога образов, **exit 0**, Docker не нужен. Source должен быть `b7a1857a51bf47bb36e42699988b38b4d897fa1f` |
| 2 | `python3.11 offline.py verify --suite quick --output-root ./results` | **347 тестов** в 8 наборах, без пропусков. **Exit 0**, если все исполненные проверки и фактические лимиты контроллера подтверждены; **exit 2**, если ресурсы не подтверждены. Это быстрые проверки, не полная приёмка |
| 3 | `python3.11 offline.py verify --suite rehearsal --output-root ./results` | Те же тесты, 20 снимков, 4 политики × 20 попыток, 20 пар и независимые проверки исходных книг. Ожидаемая строгая приёмка — **exit 2**, пока OPS-01 / OPS-02 остаются «не проверено» |
| 4 | evidence checker (ниже) | При полном внутреннем прогоне и подтверждённых ресурсах: `internal_rehearsal_passed`, `failures: []`, `missing: []`, **exit 0**. Недостающие ресурсные свидетельства — `not_checked`, **exit 2**; обнаруженная ошибка — **exit 1** |
| 5 | `python3.11 offline.py image --role runtime` | Одна строка `sha256:…`, **exit 0**, после загрузки образов шагом 2 или 3 |

Число 347 подтверждено CI исходного SHA; оно не является утверждением, что получатель уже прошёл
проверку. Длительность, image IDs, пиковую память и результат своего запуска записывайте фактически.

Evidence checker использует только стандартную библиотеку хостового Python. Извлеките поставленные
исходники, не клонируя репозиторий и не устанавливая зависимости:

```sh
mkdir -p source
tar -xzf source.tar.gz -C source
```

Задайте `RESULTS` равным точному каталогу `results/verify-…` **завершённого шага 3**:

```sh
RESULTS=./results/verify-REPLACE-WITH-YOUR-RUN
python3.11 -I source/tools/check_rehearsal_evidence.py "$RESULTS" --bundle . --output evidence-summary.json
```

**Различайте три результата.** Быстрые проверки могут завершиться кодом 0 при ещё открытой полной
матрице SPEC. Полная репетиция возвращает 2 из-за непроверенных обязательных пунктов OPS-01 / OPS-02;
проверьте причины в `verify/report.json`, а не принимайте любой код 2 за ожидаемый. Отдельный evidence
checker может вернуть 0, подтвердив именно внутренний прогон, архив и контейнерные ресурсы. Это не
подтверждает внешний адаптер жюри, независимую человеческую приёмку или память всей ОС.

Коды launcher: **0** — выбранные проверки и ресурсы подтверждены; **1** — ошибка; **2** — неполная
приёмка/ресурсные свидетельства; **130** — SIGINT; **143** — SIGTERM. Для обоих сигналов проверьте
поле `cleanup`: прерывание не превращается в завершённый успешный прогон.

Во втором терминале на время шагов 2–3 запишите события Docker:

```sh
docker --host unix:///var/run/docker.sock events --format '{{.Type}} {{.Action}} {{.Actor.Attributes.name}}'
```

Ожидается: ни одного `image pull`; события `network connect` только с сетью `none`.

## 5. Ожидаемые файлы результатов

Каждый вызов `verify` создаёт новый `results/verify-<UTC>-<id>/` и никогда не перезаписывает прежние.

| Файл | Что проверить |
|---|---|
| `launch.json` | `status`, `exit_code`, `source_revision`, `loaded_images` (три роли, ID и OCI digest), `launch_id`, фактические времена, `cleanup.status` и `cleanup.remaining_containers` |
| `controller-resources.json` | Фактические лимиты контроллера; на cgroup v1 `resource_limits_verified` не `true` |
| `verify/report.json` | `status`, `exit_code`, `commit`, `python`, `checks` по наборам; после репетиции — `rehearsal.summary` |
| `verify/report.md` | Человекочитаемая сводка того же отчёта |
| `verify/<набор>.json`, `.log`, `-artifacts/` | ID каждого теста и журналы |
| `verify/rehearsal/` | `attempts/<политика>/internal-NN/final.json` (сдача и оценка), `snapshots/`, `independent/`, `pairs/` |

Сводка нового прогона — внутренний диагностический результат, не конкурсный балл. Для каждой политики
нужно `planned = measured = 20`, `score_status = complete`, `failed = not_checked = 0`.
При недостающих измерениях сводные `micro` и `mean_reward` равны `null`; `measured_only` содержит
только частичную выборку и не заменяет полный результат. Не подставляйте числа RC2 вместо измерений RC3.

```sh
python3.11 - "$RESULTS/verify/report.json" <<'PYCODE'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    report = json.load(stream)
for policy, row in report["rehearsal"]["summary"].items():
    print(policy, "planned/measured:", row["planned"], row["measured"],
          "status:", row["score_status"], "failed:", row["failed"],
          "not_checked:", row["not_checked"], "micro:", row["micro"],
          "mean_reward:", row["mean_reward"])
PYCODE
```

Сравните результат с опубликованным evidence RC3 **для этого source SHA и вашего хранилища**.
Самостоятельные финансовые результаты и показатели ресурса не заменяются статусом CI.

## 5а. Обязательные политики на 10 train и 10 dev

Критерий OPS-02 в SPEC §14 требует приложить результаты обязательных политик на открытых наборах
`datasets/open/train` и `datasets/open/dev` (по 10 компаний). Репетиция §4 их не включает: в ней 20 внутренних
снимков. Наборы лежат внутри runtime-образа. Команда ниже использует только поставленные образы. Каждая
обычная политика запускается в отдельном контейнере без сети, как в `verify`. Выполняйте после шага 2 или 3,
когда образы загружены.

```sh
AUDITLAB_RUNTIME_IMAGE=$(python3.11 offline.py image --role runtime)
AUDITLAB_POLICY_IMAGE=$(python3.11 offline.py image --role policy)
mkdir -p open-results
```

```sh
docker --host unix:///var/run/docker.sock run -i --rm --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --group-add "$(stat -c %g /var/run/docker.sock)" --cpus 2 --memory 1536m \
  --tmpfs /tmp:rw,nosuid,size=1g \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
  --mount "type=bind,src=$(pwd)/open-results,dst=/results" \
  "$AUDITLAB_RUNTIME_IMAGE" python - "$AUDITLAB_POLICY_IMAGE" > open-results/summary.jsonl <<'PYLOOP'
import json, os, subprocess, sys
image = sys.argv[1]
for split in ("train", "dev"):
    for episode in sorted(os.listdir(f"datasets/open/{split}")):
        snapshot = f"datasets/open/{split}/{episode}"
        for policy in ("random", "reconcile", "flag_all", "oracle"):
            command = [sys.executable, "-m", "auditlab.runner"]
            command += (["oracle", "--snapshot", snapshot] if policy == "oracle" else
                        ["policy", "--snapshot", snapshot, "--name", policy, "--image", image])
            command += ["--mode", "closed", "--trusted-dir", f"/results/{split}/{episode}/{policy}"]
            done = subprocess.run(command, capture_output=True, text=True)
            lines = done.stdout.strip().splitlines()
            result = json.loads(lines[-1]) if lines else {"status": "no_output", "exit": done.returncode}
            print(json.dumps({"split": split, "episode": episode, "policy": policy, "result": result}), flush=True)
PYLOOP
```

Сводка (стандартная библиотека Python на хосте):

```sh
python3.11 - open-results/summary.jsonl <<'PYCODE'
import collections, json, sys
total = collections.defaultdict(lambda: [0, 0, 0, 0, 0.0, collections.Counter()])
for line in open(sys.argv[1], encoding="utf-8"):
    row = json.loads(line); result = row["result"]; score = result.get("score") or {}
    t = total[(row["split"], row["policy"])]
    t[0] += 1; t[1] += score.get("tp", 0); t[2] += score.get("fp", 0); t[3] += score.get("fn", 0)
    t[4] += result.get("reward") or 0; t[5][result.get("status")] += 1
for (split, policy), (n, tp, fp, fn, reward, status) in sorted(total.items()):
    print(split, policy, "n:", n, "status:", dict(status), "TP/FP/FN:", tp, fp, fn,
          "mean_reward:", round(reward / n, 4))
PYCODE
```

Ожидается 80 строк, у каждой `status = submitted`. Агентская проверка RC3 получила следующее (TP / FP / FN
и средняя награда; внутренняя диагностика открытых наборов, не конкурсный балл):

| Набор | random | reconcile | flag_all | oracle |
|---|---|---|---|---|
| train | 0 / 10 / 11, −1 | 11 / 0 / 0, 0,982 | 0 / 300 / 11, −1 | 11 / 0 / 0, 1 |
| dev | 0 / 10 / 11, −1 | 11 / 0 / 0, 0,982 | 0 / 300 / 11, −1 | 11 / 0 / 0, 1 |

В каждом наборе две чистые компании; reconcile не дал на них ложных обвинений. Около 1,5 минуты на 2 CPU.
Сравните со своим запуском, а не переписывайте эти числа. Подкоманда runner называется `policy`; в
docstring модуля RC3 по ошибке написано `public`.

## 6. Консоль и сценарий

Запуск демо, адрес и сценарий на 3 минуты — [DEFENSE.md](DEFENSE.md). Кратко:

```sh
AUDITLAB_RUNTIME_IMAGE=$(python3.11 offline.py image --role runtime)
mkdir -p demo-results
docker --host unix:///var/run/docker.sock run --rm --pull never --user "$(id -u):$(id -g)" --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 2 --memory 2g \
  --tmpfs /tmp:rw,nosuid,size=1g -p 127.0.0.1:8765:8765 \
  --mount "type=bind,src=$(pwd)/demo-results,dst=/results" \
  "$AUDITLAB_RUNTIME_IMAGE" python run.py demo --host 0.0.0.0 --root /results/demo
```

Откройте `http://127.0.0.1:8765/`; адрес `0.0.0.0` в браузере не открывайте. `curl -s http://127.0.0.1:8765/demo/health`
отвечает `{"ok":true,"runs":6,"static":true}` после создания шести учебных экспортов в новом каталоге.
После собственных опытов число запусков растёт. Сверка консоли с экспортами в контейнере без сети:

```sh
docker --host unix:///var/run/docker.sock run --rm --pull never --network none --user "$(id -u):$(id -g)" --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 2 --memory 1536m --memory-swap 1536m --pids-limit 256 \
  --tmpfs /tmp:rw,size=64m \
  --mount "type=bind,src=$(pwd)/demo-results,dst=/results" "$AUDITLAB_RUNTIME_IMAGE" \
  python -m auditlab.demo --root /results/demo crosscheck --live
```

Успех `crosscheck --live` требует непустых экспортов и реально выполненного пересчёта оценки.
Пустой набор — `not_checked`, код 2. Повреждение экспорта, недоступный эталон пересчёта или расхождение
не дают `ok: true`. Для резервного `replay --check` используйте ID из списка запусков, а не `S01`/`S02`.

## 7. Диагностика ошибок

Сообщения и их смысл соответствуют интерфейсу launcher; исторические воспроизведения RC2 находятся
в отдельном отчёте. Сохраните точный текст ошибки нового запуска.

| Симптом | Причина и что делать |
|---|---|
| `Offline launch failed: launcher requires Python 3.11`, exit 1 | Запущен другой Python. Используйте `python3.11` |
| `Offline launch failed: checksum or size mismatch: licenses.json; no images were loaded`, exit 1 | Файл комплекта повреждён или изменён. Образы не загружались, каталог результатов не создан. Распакуйте архив заново после проверки хеша (раздел 3) |
| `Offline launch failed: saved Docker catalog lost the tagged runtime identity` (или `saved OCI index …`) | Архив без тегированных записей образов, например RC1 на новом launcher. Возьмите архив `v0.1.0-rc.2` или новее |
| `Resource limits are not supported by this daemon; functional checks continue…` | cgroup v1 или демон без делегирования контроллеров. Функциональные проверки идут, итоговые коды 2. Для ресурсной приёмки нужна машина с cgroup v2 |
| `permission denied … /var/run/docker.sock` или `Cannot connect to the Docker daemon` | Демон не запущен или у пользователя нет доступа к сокету. `docker --host unix:///var/run/docker.sock info` должен работать от того же пользователя |
| Порт `8765` занят | Остановите прежний контейнер демо (`docker ps`, `docker stop <name>`) или пробросьте другой локальный порт `-p 127.0.0.1:8766:8765` |
| В консоли `"runs":0` | Каталог `demo-results` недоступен на запись пользователю контейнера. Создайте его от своего пользователя до `docker --host unix:///var/run/docker.sock run` |
| Оценка или наблюдения разошлись в `replay --check` / `crosscheck` | Дефект: сохраните вывод и каталог `demo-results` и сообщите |

## 8. Прерывание и повтор

- **Ctrl+C / SIGINT:** код **130**. **SIGTERM:** код **143**. При существующем `launch.json` статус
  `interrupted`, поле `signal` содержит номер сигнала. Дождитесь выхода launcher: он завершает очистку.
- Начиная с RC3 это относится и к сигналу **только процессу launcher** (`kill -INT <PID>` или
  `kill -TERM <PID>` из другого терминала). Контроллер, политики и verifier этого запуска получают
  случайную метку `org.auditlab.launch=<launch_id>`; launcher удаляет только эти контейнеры.
  Другие запуски и чужие контейнеры сохраняются. После старта контроллера ожидается
  `cleanup.status = passed`, `cleanup.remaining_containers = 0`.
- Проверьте отсутствие контейнеров конкретного запуска; **не** требуйте пустого общего `docker ps`:

```sh
AUDITLAB_LAUNCH_ID=$(python3.11 -c 'import json,sys; print(json.load(open(sys.argv[1]))["launch_id"])' "$RESULTS/launch.json")
docker --host unix:///var/run/docker.sock ps --all \
  --filter "label=org.auditlab.launch=$AUDITLAB_LAUNCH_ID" --format '{{.ID}} {{.Names}}'
```

Ожидается пустой вывод и код 0. Для проверки сигнала дождитесь начала контейнерной проверки и сохраните
PID launcher. Если прервали раньше создания контроллера, `launch_id`/`cleanup` могут отсутствовать:
это не проверка удаления работающих контейнеров. Если Docker недоступен во время очистки,
`cleanup.status = failed`; сохраните ошибку и проверьте эту метку после восстановления демона.
Не удаляйте контейнеры других пользователей и не подменяйте фактический вывод Docker.

- Каталог прерванного запуска содержит частичные результаты; не используйте его как свидетельство
  успешной проверки. Повтор той же команды создаёт **новый** каталог `results/verify-…`, прежний сохраняется.
  Образы снова проверяются и загружаются из локального архива, без registry pulls.
- SIGKILL и выключение машины не позволяют выполнить `finally`; автоматическая очистка при них не
  заявлена. После восстановления проверьте метку прерванного запуска вручную.
- После перезагрузки загруженные Docker-образы сохраняются; повторите `verify` или `image`.

## 9. Что вернуть авторам

Заполните [форму журнала](../reviews/receiver-acceptance-rc3/JOURNAL_FORM.md); короткая версия всего
порядка — [пакет получателя](../reviews/receiver-acceptance-rc3/RECEIVER_PACKAGE.md). Агентская проверка RC3 —
[отчёт](../reviews/receiver-acceptance-rc3/REPORT.md); она не заменяет ваш журнал.


Журнал по разделам 1–8: версии ОС, Python и Docker, хранилище и cgroup, SHA архива и исходников, три image ID
из `launch.json`, команды, коды возврата и длительности, сводку репетиции, вывод evidence checker и
`docker --host unix:///var/run/docker.sock events`. Пройдите чек-лист [ACCEPTANCE_CHECKLIST.md](ACCEPTANCE_CHECKLIST.md) и добавьте снимки экрана
консоли. Каждое затруднение с инструкцией записывайте дословно. Вывод формулируйте как «независимая
человеческая проверка комплекта … на машине …» без утверждений о конкурсных баллах. До такого отчёта
пункт OPS-02 / UI-01 (#15) остаётся открытым.
