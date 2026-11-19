# Автономная поставка AuditLab

Сборка и исполнение — разные этапы. Сборщик один раз получает закреплённые базовые
образы и wheels. Получатель готового комплекта не устанавливает Python-пакеты,
не собирает образы и не обращается к registry. Нужны Python 3.11 и работающий
локальный Docker Engine с Linux-контейнерами и сокетом `/var/run/docker.sock`.
Сборщик поддерживает Linux x86_64/Python 3.11. Проверка на Windows/Docker Desktop
этим документом не заявляется.

## Сборка из зафиксированной версии

Все изменения, включая лицензию, lock и файлы упаковки, сначала должны попасть
в чистый commit. Рабочее дерево с изменениями или непроиндексированными файлами
сборщик отвергает. Каталог назначения должен быть новым:

```sh
python tools/build_release.py --output build/release-candidate
```

Сборщик берёт дерево из `git archive HEAD`, исключая каталоги результатов,
виртуальные среды, `.env` и файлы ключей. Непроиндексированные и игнорируемые
файлы рабочего каталога никогда не становятся Docker build context. Symlink и
hardlink в дереве исходников отвергаются. Генераторные `private/truth.json` в
открытых train/dev и ручных fixtures — часть воспроизводимых синтетических
материалов, они сохраняются; закрытых эпизодов организатора в исходниках нет.

На стороне сборки выполняется `pip download --require-hashes --only-binary=:all:`
по общему lock. Wheels помещаются во временный Docker context. Docker RUN-этапы
идут с `--network none`; закреплённые Dockerfile устанавливают wheels с
`--no-index`. Получение базовых образов и wheels относится к подготовке сборки,
а не к официальной автономной проверке.

Комплект содержит:

| Файл | Содержание |
|---|---|
| `offline.py` | Самостоятельный launcher на стандартной библиотеке Python 3.11. |
| `images.tar` | Один `docker save` для runtime, verifier и policy; общие слои сохранены один раз. |
| `source.tar.gz` | Отфильтрованное дерево исходного commit, включая LICENSE. |
| `licenses.json` | Версии Python-пакетов, метаданные лицензий, доступные тексты LICENSE/NOTICE и Debian copyright из образов; лицензия исходников. |
| `manifest.json` | SHA исходников, точные image IDs, ОС/архитектура, размеры образов, версии, SHA-256 и размеры всех четырёх payload-файлов. |
| `SHA256SUMS` | Контрольные суммы payload-файлов и самого manifest. |

`payload_size_bytes` — суммарный размер четырёх payload-файлов, включая архив
образов. Размер каждого распакованного образа указан отдельно; его нельзя
складывать с остальными образами для оценки общего хранилища без учёта общих
слоёв. Размер готового каталога и памяти реально измеряются перед выпуском.
В исходники, runtime и licenses.json включены 113 файлов LICENSE/NOTICE/COPYING
из исходников Docker CLI v29.0.1 и его vendor-зависимостей; содержимое проверено
по Git blob SHA из дерева upstream. Происхождение записано в sources.json.

## Проверка полученного комплекта

### Перед запуском: хранилище образов и cgroup

Сборщик сохраняет все три образа **по тегам**, проверяет соответствие тегов image IDs
в Docker-каталоге и полноту OCI-индекса. CI испытывает один и тот же комплект на
двух свежих хостах: классическое хранилище и Docker 29.0.1 с containerd image store.
Артефакты `offline-evidence-classic-SHA` и `offline-evidence-containerd-SHA` разделены;
успех одного хранилища не подтверждает другое. Во время исполнения pulls запрещены,
каждый контейнер запускается по проверенной неизменяемой идентичности образа.
Манифест хранит исходный config ID и OCI manifest digest: классическое хранилище
использует первый, containerd — второй. Launcher принимает только эти проверенные
идентификаторы, не mutable tag; выбранные значения записываются в `launch.json.loaded_images`.

**Ограничение старого `v0.1.0-rc.1`:** он проверен на классическом хранилище Docker (`overlay2`).
Новые установки Docker Engine 29 по умолчанию используют containerd image store. На нём RC1 `docker load` из
`images.tar` загружает **один безымянный образ из трёх** (в OCI `index.json` комплекта
`v0.1.0-rc.1` один манифест, в `manifest.json` три образа без `RepoTags`), и `verify` завершается
`Offline launch failed: ... docker image inspect sha256:9cad68... returned non-zero exit status 1`.
Проверьте до запуска:

```sh
docker info --format '{{.Driver}} {{.DriverStatus}} cgroup={{.CgroupVersion}}'
```

Для повторения именно RC1 `overlay2 ... cgroup=2` — подходящая конфигурация. Если в выводе есть
`io.containerd.snapshotter.v1`, временный обход для RC1 — на отдельной проверочной машине
переключить демон на классическое хранилище и перезапустить его. Уже
загруженные образы при этом не видны:

```json
{"features": {"containerd-snapshotter": false}}
```

Это настройка `features` в `/etc/docker/daemon.json` с сохранением остальных параметров,
затем перезапуск Docker; не меняйте рабочий демон с чужими контейнерами ради проверки RC1.
Для нового комплекта проверяйте свидетельства своей версии на нужном хранилище.
`cgroup=1` даёт только функциональную
проверку: launcher пишет `Resource limits are not supported by this daemon`, ресурсные строки
остаются `not_checked`, а `verify` и evidence checker законно возвращают 2.

### Команды

Перед передачей отдельно публикуется checksum manifest/комплекта. Внутренние
SHA-256 выявляют повреждение файлов, но не удостоверяют источник изменённого
manifest. Из каталога комплекта:

```sh
python offline.py check
python offline.py verify --output-root ./results
python offline.py verify --suite rehearsal --output-root ./results
```

Внутреннее доказательство репетиции проверяет checker из приложенного `source.tar.gz` (та же
команда, что в CI); `RESULTS` — каталог `results/verify-…` последнего `--suite rehearsal`:

```sh
mkdir -p source && tar -xzf source.tar.gz -C source
python3.11 -I source/tools/check_rehearsal_evidence.py RESULTS --bundle . --output evidence-summary.json
```

Ожидаемо `"status": "internal_rehearsal_passed"`, exit 0, `failures: []` при полных 20/80/20 и
подтверждённых лимитах; без cgroup v2 — `not_checked`, exit 2, `failures` пуст, а `missing`
перечисляет ресурсные строки.

`check` проверяет весь комплект без Docker. `verify` сначала проверяет размеры
и хеши **всех** payload-файлов и только затем вызывает `docker load`. После
загрузки сверяются image IDs. Все контейнеры запускаются по `sha256:...` с
`--pull never`. Launcher игнорирует удалённые Docker contexts/DOCKER_HOST и
использует локальный Unix socket.

Обычный `verify` передаёт policy image через `AUDITLAB_POLICY_IMAGE`, чтобы
проверки реальной границы контейнера могли использовать поставляемый образ.
`--suite rehearsal` дополнительно передаёт точные `--policy-image`,
`--verifier-image`, `--cpu-set` общему проверяющему запуску. По умолчанию выбираются
первые два CPU из affinity процесса; можно явно указать две доступные CPU:

```sh
python offline.py verify --suite rehearsal --cpu-set 0,1
```

Один и тот же набор CPU передаётся controller и harness дочерних контейнеров.
Controller использует UID/GID пользователя launcher и дополнительную группу
локального Docker socket; каталог результатов не требует world-writable прав.
Controller работает с `--network none --cpus 2 --memory 2g --memory-swap 2g`, read-only rootfs,
`cap-drop ALL`, `no-new-privileges`, временным `/tmp` до 1 ГБ. Только доверенному
controller доступны Docker socket и каталог результатов. Обычная политика не
получает ни socket, ни host bind mounts. Verifier получает публичные файлы через
JSON/stdin; ему не требуется том каталога controller.

Каждый вызов создаёт новый `results/verify-<UTC>-<случайный-id>/`. `launch.json`
содержит версию, ресурсы, image IDs, статус, длительность загрузки образов и
общее время. `controller-resources.json` записывается после окончания дочернего
проверяющего процесса и содержит фактические `memory.peak`, `cpu.stat`, лимиты
и эффективный cpuset controller. Политика и verifier — sibling-контейнеры,
их отдельные измерения берутся из harness, а не из памяти controller.
Вложенный `verify/` содержит отчёты, журналы, результаты испытаний и
артефакты общего запуска. Обрывы не перезаписывают предыдущие результаты.
Коды возвращаются из общего запуска: 0 — доступные проверки прошли, 1 — ошибка,
2 — неполная обязательная приёмка; прерывание launcher — 130. Код 0 быстрого
запуска сам по себе не сертифицирует полный релиз: смотрите статусы в отчёте.

## Локальная демонстрация и повтор

Демо запускается отдельно от официальной network-none проверки. После успешного
`offline.py verify` образы уже загружены. Для проброса порта используется bridge,
а порт доступен только с localhost; интерфейс и ресурсы находятся в образе.

```sh
AUDITLAB_RUNTIME_IMAGE=$(python offline.py image --role runtime)
mkdir -p demo-results
docker run --rm --pull never --user "$(id -u):$(id -g)" --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 2 --memory 2g \
  --tmpfs /tmp:rw,nosuid,size=1g -p 127.0.0.1:8765:8765 \
  --mount "type=bind,src=$(pwd)/demo-results,dst=/results" \
  "$AUDITLAB_RUNTIME_IMAGE" python run.py demo --host 0.0.0.0 --root /results/demo
```

Команда `image` проверяет хеши комплекта и возвращает только неизменяемый ID уже
загруженного локального образа. В старом RC1 этой команды нет: для него на
классическом хранилище берите `images.runtime.id` из `manifest.json`, как в инструкции
внутри RC1. Новый launcher не изменяет архив RC1 задним числом.

Откройте `http://127.0.0.1:8765/`: сервер в контейнере слушает `0.0.0.0`; образ `v0.1.0-rc.1` печатает
адрес `http://0.0.0.0:8765/`, его в браузере не открывайте. Сценарий защиты на 3 минуты:
[docs/demo/DEFENSE.md](../demo/DEFENSE.md). Этот запуск не получает Docker socket. Для
повтора выбранного открытого run ID после остановки сервера:

```sh
docker run --rm --pull never --user "$(id -u):$(id -g)" --network none --read-only \
  --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,size=1g \
  --mount "type=bind,src=$(pwd)/demo-results,dst=/results" \
  "$AUDITLAB_RUNTIME_IMAGE" python run.py replay --root /results/demo --run RUN_ID --check
```

## Статус измерений

Наличие этих скриптов не является результатом испытаний. Для конкретного
release-candidate необходимо приложить его manifest/hash, успешную автономную
проверку из чистого каталога, фактические 80 результатов и 20 пар, cold-load/start
время, пиковую память и размер. До измерения соответствующая строка остаётся
«не проверено». Исходная авторская оценка и результаты отдельного ноутбука не
подменяют проверку именно передаваемого комплекта.


### Проверка реальных ограничений ядра

Параметры Docker и `HostConfig` сами по себе не доказывают применение лимитов.
Некоторые rootless-окружения имеют `CgroupDriver=none`: тогда ограничения CPU,
памяти и процессов не действуют, а видимые cgroup-счётчики могут принадлежать
всей машине. Такой прогон подтверждает только функциональность. Его пиковая
память контейнера и соответствие целевым ресурсам остаются «не проверено».
Rootless Docker с корректным делегированием cgroup допустим; проверяется
фактическая поддержка контроллеров, а затем их значения внутри контейнеров.

Job `Internal offline rehearsal` в GitHub Actions собирает комплект из чистого
commit, запускает 80 опытов и 20 пар, проверяет фактические лимиты и публикует
bundle вместе с отчётами. Успех этого job не означает прохождение всех 24 групп
SPEC, независимую проверку новым пользователем или результат скрытых тестов жюри.
