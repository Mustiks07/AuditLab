# Агентская проверка `v0.1.0-rc.1` по инструкции получателя

**Это проверка AI-агента (Claude Pro 3) 2026-11-19, а не независимая человеческая приёмка.**
Она не закрывает OPS-02 и визуальную часть UI-01 (#15); для человека подготовлен
[чек-лист](ACCEPTANCE_CHECKLIST.md). Агент участвовал в разработке консоли.

## Что проверялось и на чём

| | |
|---|---|
| Комплект | `AuditLab-v0.1.0-rc.1-offline.zip`, внешний SHA-256 `44b626d3…8723` — `OK` по `…-SHA256SUMS.txt` |
| Источник | `b16456cab15d65c75dac5fc9d249af2d2ee8c055` (`offline.py check`) |
| Образы | runtime `sha256:9cad681d…f88f`, verifier `sha256:e1133a7d…723e`, policy `sha256:f6edc235…f486` — совпали с manifest после загрузки |
| Машина | Linux 6.18 x86_64 (контейнер облачной сессии), Ubuntu 24.04.5, Python 3.11.18, Docker Engine 29.10.0, **cgroup v1** |
| До загрузки | 0 образов в демоне |

Ограничение машины: cgroup v1. Лимиты ресурсов здесь не подтверждаются, это ожидаемо даёт exit 2.
Ресурсные строки этой проверкой не закрываются.

## Команды и результаты

| Шаг | Команда | Результат |
|---|---|---|
| 1 | `sha256sum -c AuditLab-v0.1.0-rc.1-SHA256SUMS.txt --ignore-missing` | offline и evidence ZIP — `OK` |
| 2 | `python3.11 offline.py check` | `Bundle hashes verified; source b16456c…`, exit 0, 0,3 с |
| 3 | `python3.11 offline.py verify --output-root ./results` на демоне по умолчанию (containerd image store) | **exit 1**: `Loaded image ID: sha256:801c3c…`, затем `Offline launch failed: … docker image inspect sha256:9cad68… returned non-zero exit status 1` — см. затруднение 1 |
| 4 | то же после `{"features":{"containerd-snapshotter":false}}` и перезапуска демона | загружены все три ожидаемых ID; 8 наборов, 237 тестов пройдены; launcher: `Resource limits are not supported by this daemon`; **exit 2**, загрузка образов 3,14 с, всего 93,6 с |
| 5 | `python3.11 offline.py verify --suite rehearsal --output-root ./results` | 237 тестов пройдены, `status=incomplete`, **exit 2**, 176,6 с. Репетиция: reconcile TP/FP/FN 38/0/0, средняя награда 0,9512; random 0/0/38, −0,5035; flag_all 0/600/38, −1; oracle 38/0/0, 1 — совпадает с VALIDATION.md |
| 6 | `check_rehearsal_evidence.py RESULTS --bundle . --output evidence-summary.json` (из `source.tar.gz`; команда взята из CI) | `datasets 20, attempts 80, pairs 20`, `failures: []`, `missing`: 102 ресурсные строки, `not_checked`, **exit 2** — ожидаемо для cgroup v1 |
| 7 | `docker events` во время шагов 4–5 | 0 событий `image pull`; 107 контейнеров подключались только к сети `none` |
| 8 | Демо по offline.md (bridge, `-p 127.0.0.1:8765:8765`) | `/demo/health` → `{"ok":true,"runs":3,"static":true}`; Playwright-обход: 0 запросов к внешним хостам, 0 ошибок страницы |
| 9 | `replay --run RUN_ID --check` в контейнере `--network none` для учебного S02 и живого опыта | «Повтор действий: 5 шагов, наблюдения совпали; повтор оценки: совпала», exit 0 |

Проверено в консоли образа RC: выбор компании, контрольная пара, `ledger/R5` → `public/ledger.csv · строка 6`,
операнды суммы, строки «Оценщик», «Консоль», «SQL (auditlab.verifier)», пустая сдача S01, изменение факта
S01 `ledger/R7` → новый прогон с 20 000,00 ₸ (TP), копия сдачи `ledger/R5 → R6` (TP 1 → 0, −1),
replay живого опыта, две ошибки операций (`unsupported_counterfactual`, `invalid_value`).

## Найденные затруднения

| № | Что | Где | Статус |
|---|---|---|---|
| 1 | На containerd image store (по умолчанию в новых установках Docker 29) `docker load` загружает один безымянный образ из трёх. В OCI `index.json` из `images.tar` один манифест, в `manifest.json` три образа с `RepoTags: null`. `verify` падает с exit 1 | `tools/build_release.py` (сохранение образов по ID), `offline.py` | В offline.md добавлены проверка демона и обход. **Исправление сборки или launcher остаётся владельцу релиза** |
| 2 | Как запускать evidence checker, нигде, кроме CI-workflow, не описано | offline.md | Добавлена команда |
| 3 | Сервер демо печатает `http://0.0.0.0:8765/` | консоль | Печатает `http://127.0.0.1:…`; в offline.md предупреждение для образа RC |
| 4 | «Сравнить пару» с настоящими `run_id` открывала пару в случайном порядке: «Исходная: S02», Δ = −20 000 ₸ | консоль | Порядок по эпизоду: A = S01, B = S02, Δ = B − A; нейтральные подписи «Компания A/B» |
| 5 | Ошибки операций показывались сырым английским сообщением лаборатории | консоль | Русское объяснение по коду; код и исходное сообщение сохранены |
| 6 | «Больше не подтверждено: … 3000000» — сумма без единиц | консоль | «30 000,00 ₸ (3 000 000 мин. ед. KZT)»; у операндов подпись «мин. ед.» и сумма в валюте |
| 7 | Разные знаки минуса: «−20 000,00 ₸», но «-2 000 000 мин. ед.» | консоль | Единый знак «−» |
| 8 | На экране не сказано, что награды консоли — учебная демонстрация, а измерения лежат в отчёте репетиции | консоль, docs/demo | Плашка на странице запусков, разделы в README и DEFENSE |
| 9 | Резервные CLI-команды ссылались на `TRAIN-S02-reconcile`; с настоящим раннером такого ID не бывает | docs/demo/README.md | Заполнители и `list` |

Исправления 3–9 проверены в консоли из ветки (checkout, тот же lock). Образ RC остаётся прежним,
пока релиз не пересоберут.
