# Late refresh run log — 22.11.2026

Окно запусков: приблизительно 15:50–16:22 UTC. Базовый образ:
`python@sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535`
(Python 3.11.18).

## Изоляция

Каждый запуск использовал локальную копию зафиксированного SHA и Docker с сетью `none`, UID/GID
65534, read-only rootfs, `--cap-drop ALL`, `--security-opt no-new-privileges`, `--pids-limit 256`,
2 CPU и ограничением памяти 1–2 GiB. Записываемыми были только отдельный tmpfs и каталог результата.
Зависимости из сети во время измерений не устанавливались.

Шаблон зонда (пути сокращены, флаги изоляции одинаковы):

```sh
docker run --rm --network none --read-only --user 65534:65534 \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 256 \
  --cpus 2 --memory 1g --tmpfs /tmp:rw,noexec,nosuid,size=128m \
  -v REPO:/repo:ro -v EVIDENCE:/evidence:rw python:3.11-slim \
  python /evidence/adapter_probe.py /repo /evidence/OUT.json CONFIGS
```

## Adapter и pair probes

| Репозиторий | SHA | adapter | literal pair | Raw JSON |
|---|---|---:|---:|---|
| azamxvit/audit-rl-env | `535aae42a4255edc2e5d57e4b12967befc4b4c53` | 26/26 | 5/5 | `azamxvit.json`, `pair-azamxvit.json` |
| ibrahimerbol01-prog/auditgym | `3ea176920667ec1401cb93b18b18d38da20f7c73` | 24/26 | 5/5 | `ibrahimerbol01.json`, `pair-ibrahimerbol01.json` |
| manskrutoy/auditgym-fintech | `aa5c7c098fd01f676c89fdb1c64b544733cad230` | 25/26 | 5/5 | `manskrutoy.json`, `pair-manskrutoy.json` |
| alimasik/genesis-hackaton | `412e914120324f5de792f4f1b5d35b8a4be46e8f` | 24/26 | 5/5 | `alimasik-new.json`, `pair-alimasik-new.json` |
| k41270075-stack/vouchai | `41b1c6856c580d30061858f4292b83e1b1e941a7` | 26/26 | 0/5 | `vouchai-current.json`, `pair-vouchai-current.json` |

Literal pair — намеренно неполный преобразователь, который не меняет все документированные ссылки.
Он сохранён как negative control, а не как эмуляция официальной проверки.

## azamxvit: learned detector

Команды в том же network-none контейнере, `PYTHONPATH=/repo/backend`:

```sh
python /repo/scripts/train_learned.py --check
python /repo/scripts/eval_learned.py
```

Результат первого запуска: `learned model reproducible: OK`.

Второй запуск воспроизвёл опубликованный агрегат по 4 dev configs × 20 fresh seeds: learned
354/354 TP, 0 FP, 0 FN, micro-F1 1, mean return 0.967; simple reconciler micro-F1 0.558,
mean return 0.310; clean return 0.964. SHA-256 пересозданного JSON:
`3cdbcb1221a5bc59083b7d3107018e27cd665e99f06391cf423eed51bd158b9d`, совпадает с committed
`backend/runs/learned_eval.json`.

Попытка собрать собственный Dockerfile проекта остановилась на загрузке `pytest` из PyPI из-за
`CERTIFICATE_VERIFY_FAILED` в SSL-цепочке build-среды. Это ограничение среды рецензента; оно не
записано как ошибка проекта и не повлияло на network-none прогоны stdlib-кода.

## ibrahimerbol: verifier и quick campaign

```sh
PYTHONPATH=/repo/src python -m auditgym.verify
PYTHONPATH=/repo/src python /repo/train/campaign.py --quick
```

Verifier завершился с кодом 0: 85/85 собственных checks, включая contract/isolation, data/oracle,
reward, reproducibility и engineering; время 18.2 s. Его строка `Estimated score: 100.0 / 100`
является самооценкой проекта, не официальным баллом.

Quick campaign завершился 14/14. Измеренные строки отчёта: dev learned reward −0.028 против
untrained −0.252 и noise −0.600; test learned −0.003 против noise −0.500. Также прошли HTTP API,
red-team и desktop smoke. Это подтверждает pipeline, но не воспроизводит полный авторский claim
`+0.961`.

## manskrutoy: project check

```sh
PYTHONPATH=/repo/src python -m auditgym check
```

Код выхода 0 за 12.2 s: 78 tests, воспроизводимость 20 episodes, 40 independent oracle checks,
holdout policies, 20/20 paired checks и leak scan. Это собственный check проекта, не официальный
результат Genesis.

## Непроверенное в этом окне

- Полные training runs ibrahimerbol, vouchai и tryHard не запускались.
- Hosted demos не проходили повторный security review.
- Docker build azamxvit не был завершён по указанной SSL-причине.
