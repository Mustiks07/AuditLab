# Runtime обычной политики

Политика получает только код и JSON Lines в stdin/stdout. Снимки, seed компании,
эталон, оценщик и отчёты остаются в доверенном контуре. Seed политики — отдельный
публичный параметр; он не должен совпадать с секретным seed генератора по умолчанию.

## Сборка и офлайн-загрузка

Python базового образа закреплён на 3.11.14 / Debian bookworm и digest в Dockerfile.
Выпускающий фиксирует SHA-256 экспортированного архива; Dockerfile сам по
себе не является автономной поставкой. Образ политики не содержит внешних Python
зависимостей. Сборка из корня (на подготовительной машине с локальной базой):

```sh
docker build --network=none -f docker/runtime/Dockerfile -t auditlab-policy:release .
docker save -o auditlab-policy.tar auditlab-policy:release
```

На офлайн-хосте после проверки контрольной суммы:

```sh
docker load -i auditlab-policy.tar
python -m auditlab.runner policy --snapshot fixtures/foundation/S02 --name reconcile --mode closed --image auditlab-policy:release --trusted-dir runs/trusted
AUDITLAB_POLICY_IMAGE=auditlab-policy:release python -m unittest discover -s tests/runner -p test_docker_boundary.py -v
```

В PowerShell переменная задаётся отдельно:
`$env:AUDITLAB_POLICY_IMAGE = "auditlab-policy:release"`.
Для открытого экспорта добавьте `--mode train --export-dir runs/new-run`.
Существующий каталог экспорта не перезаписывается. Финансовые итоги CLI предназначены
доверенному запускающему; они не возвращаются в stdin политики.

## Программный интерфейс

```python
from auditlab.runner import make_env
from auditlab.runner.episode import run_episode
from auditlab.runner.isolation import DockerPolicy

env = make_env("closed", trusted_dir="runs/trusted")
policy = DockerPolicy("auditlab-policy:release", name="reconcile", seed=7)
try:
    run_episode(env, policy, company_seed, {"snapshot_dir": snapshot,
                "policy_name": "reconcile", "time_limit_s": 60})
finally:
    env.close()
```

`run_episode` закрывает политику также при отказе reset. `DockerPolicy` сначала
создаёт контейнер с уникальным именем, затем присоединяет stdio; cleanup явно
удаляет этот контейнер. Отсутствующий локальный образ — ошибка, загрузка из реестра
и переход к незащищённому subprocess запрещены. Для внешней политики образ должен
содержать её модуль; `DockerPolicy(..., factory="module:fn", seed=7)` вызывает
`fn(7)`, возвращающий объект с `act(public_observation)`.

Обычный профиль: `--pull=never --network none --read-only --cap-drop ALL
--security-opt no-new-privileges --pids-limit 128 --cpus 2 --memory 4g
--memory-swap 4g --user 65534:65534 --tmpfs /tmp:rw,noexec,nosuid,size=64m
--log-driver none`, без GPU, host mounts и сокета Docker. `docker_command()` —
источник параметров; реальные права дополнительно проверяет `test_docker_boundary`.

## Защита канала

Максимум 256 KiB на JSON-строку действия, 1 MiB на строку observation, один ответ
на запрос и максимум один буферизованный ответ. Незатребованный/повторный ответ,
слишком длинная строка, неверный UTF-8/JSON — отказ политики. Общий deadline
охватывает запись observation и чтение action; блокировка stdin не отключает таймер.
Убийство subprocess и удаление контейнера не подменяются ожиданием EOF.

`SubprocessPolicy` сам по себе **не является sandbox**: файл/сеть доступны с правами
пользователя хоста. CLI позволяет его только явно:
`--mode train --unsafe-dev-subprocess`. Такой запуск не доказывает изоляцию.
Oracle остаётся доверенным адаптером и не попадает в образ обычной политики.

## Статус проверки

Транспортные регрессии и фактическая контейнерная граница — разные проверки.
Без `AUDITLAB_POLICY_IMAGE` Docker-тест честно пропущен. Итоговый релиз должен
приложить успешный прогон именно поставленного образа, а также отдельные замеры
CPU, памяти, размера архива и полного офлайн-цикла. Лимиты этого контейнера
ограничивают политику, не суммарные ресурсы хостового оценщика/генератора.
Для общего профиля 2 CPU / 4 GB доверенный runtime и политика могут получить по
2 GB памяти и один и тот же набор двух ядер (`DockerPolicy(..., memory="2g", cpuset="0,1")`).
Подходящие номера ядер выбираются на хосте; они не передаются из недоверенного ввода.
