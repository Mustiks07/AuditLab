# Runtime образ политики (для упаковщика)

Контейнер получает **только код политики и стандартные потоки**. Эталон, снимки, оценщик и отчёты в образ не входят.

## Профиль запуска (ISO-01)

Источник истины — `auditlab.runner.isolation.docker_command(image)`; тест `test_docker_profile_has_no_network_volumes_or_caps` фиксирует его:

```
docker run --rm -i --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 128 --cpus 2 --memory 4g \
  --user 65534:65534 --tmpfs /tmp:rw,noexec,nosuid,size=64m  IMAGE [NAME SEED]
```

* нет сети, нет `-v`/`--volume`, нет сокета Docker, нет привилегий;
* корневая ФС только для чтения, непривилегированный пользователь;
* канал: JSON Lines — строка наблюдения в stdin, строка действия в stdout; stderr не читается.

## Как хост использует образ

```python
from auditlab.runner.isolation import SubprocessPolicy, docker_command
policy = SubprocessPolicy(docker_command("auditlab-policy:dev") + ["reconcile", "7"])
run_episode(env, policy, seed, config)   # auditlab.runner.episode
```

`NAME SEED` → `auditlab.policies.make_policy(NAME, SEED)` (random, reconcile, flag_all). Внешний адаптер: `--factory module:fn SEED` → `fn(SEED)`. Объект обязан иметь `act(public_observation)`. Новый процесс на каждый эпизод. Oracle в этот образ не входит: он исполняется доверенным адаптером `auditlab.runner.episode.OraclePolicy`.

Зависание политики до дедлайна эпизода — это тайм-аут по §8 (`truncated_timeout`, пустой ответ), падение или мусор в канале — `policy_failure` (не финансовый результат, в публичный экспорт не попадает).

## Статус

* Dockerfile **не собирался** в среде разработки (Docker недоступен); профиль запуска и протокол проверены без Docker через `SubprocessPolicy` с обычным процессом.
* `auditlab.policies` принадлежит GPT Pro 1: пока модуль не слит в main, `COPY auditlab/policies` не соберётся.
* ISO-01 в части реальных томов и окружения контейнера проверяется только после сборки образа.
