# Runtime образ политики (для упаковщика)

Контейнер получает **только код политики и стандартные потоки**. Эталон, снимки, оценщик и отчёты в образ не входят.

## Профиль запуска (ISO-01)

Источник истины — `auditlab.runner.isolation.docker_command(image)`; тест `test_docker_profile_has_no_network_volumes_or_caps` фиксирует его:

```
docker run --rm -i --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --pids-limit 128 --cpus 2 --memory 4g \
  --user 65534:65534 --tmpfs /tmp:rw,noexec,nosuid,size=64m  IMAGE [factory SEED]
```

* нет сети, нет `-v`/`--volume`, нет сокета Docker, нет привилегий;
* корневая ФС только для чтения, непривилегированный пользователь;
* канал: JSON Lines — строка наблюдения в stdin, строка действия в stdout; stderr не читается.

## Как хост использует образ

```python
from auditlab.runner.isolation import SubprocessPolicy, docker_command
policy = SubprocessPolicy(docker_command("auditlab-policy:dev") + ["auditlab.policies:make_policy_by_seed", "7"])
run_episode(env, policy, seed, config)   # auditlab.runner.episode
```

`factory` — `module:callable`, вызывается как `factory(seed)` и возвращает объект с `act(public_observation)`. Новый процесс на каждый эпизод.

## Статус

* Dockerfile **не собирался** в среде разработки (Docker недоступен); профиль запуска и протокол проверены без Docker через `SubprocessPolicy` с обычным процессом.
* `auditlab.policies` принадлежит GPT Pro 1: пока его нет, `COPY auditlab/policies` не соберётся, а имя фабрики по умолчанию в `CMD` — заглушка, упаковщику нужно подставить реальное.
* ISO-01 в части реальных томов и окружения контейнера проверяется только после сборки образа.
