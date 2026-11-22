# Источники и фиксация среза

Срез закрыт **22.11.2026 в 16:22 UTC / 21:22 Asia/Qyzylorda**. Репозитории сравнивались на
зафиксированных SHA, а не по изменяемой странице `main`. GitHub API и Actions в финальном проходе
были доступны. Скрытые и приватные проекты в каталог не входят.

## Официальная база

- [Genesis fintech checks](https://genesis-exe.com/fintech-checks.html), версия от 20.11.2026.
- Текстовая версия `https://genesis-exe.com/docs/fintech-checks.txt`, SHA-256
  `2a597ebfb94daec700ba9b1b09d62c032366e788c6e992486d444e063be8d870`.
- HTML SHA-256 `3d95db8e1509f3724fc4d1246a262e766b427ff0bcf6d55c0bf484882ee6f4a6`.
- Повторная загрузка в финальном проходе изменений не обнаружила.

## AuditLab

- `main`: `c9ce1dfce6855eee431b881fef6c93c623852dd1`.
- RC5: [`v0.1.0-rc.5`](https://github.com/Mustiks07/AuditLab/releases/tag/v0.1.0-rc.5),
  commit `920b251`.
- CI продукта: [run 45028606259](https://github.com/Mustiks07/AuditLab/actions/runs/45028606259),
  success на `5ced9b9`; ядро продукта совпадает с текущим `main`.
- Этот обзор: [PR #68](https://github.com/Mustiks07/AuditLab/pull/68), ветка
  `review/competitors-refresh-2026-11-22`.
- Отдельный submission-аудит: [PR #69](https://github.com/Mustiks07/AuditLab/pull/69), SHA
  `5baad3d69dc0e421c3da3835d33e65f27b6f3072`; на финальной сверке открыт, CI
  [45043703599](https://github.com/Mustiks07/AuditLab/actions/runs/45043703599) зелёный, изменения не
  входят в `main`/RC5.
- Полностью прочитаны: `README.md`, `docs/SPEC.md`, `docs/INTERFACES.md`,
  `docs/GENESIS_ADAPTER.md`, `docs/release/{READINESS,VALIDATION,ACCEPTANCE_MAPPING,
  CANDIDATE_PUBLICATION,GENERALIZATION,MEASUREMENT_SEMANTICS,PR_REVIEW}.md`, offline-инструкции,
  отчёты `jury-compatibility-v3`, `generator-hardening-v3`, `data-shortcuts-v3` и A/B/C.

## Основные конкуренты

| Репозиторий | SHA на срезе | Проверенные основания |
|---|---|---|
| [azamxvit/audit-rl-env](https://github.com/azamxvit/audit-rl-env) | `535aae42a4255edc2e5d57e4b12967befc4b4c53` | `backend/judge_adapter.py`, `scripts/train_learned.py`, `scripts/eval_learned.py`, `backend/runs/learned_eval.{md,json}`; adapter 26/26, pair 5/5, обучение-check и eval воспроизведены; GitHub Actions отсутствуют |
| [madiyar-abi/genesis](https://github.com/madiyar-abi/genesis) | `911845ff` | adapter 26/26, pair 5/5; `python -m auditenv.check` повторён; [Actions 45006274943](https://github.com/madiyar-abi/genesis/actions/runs/45006274943) success |
| [ibrahimerbol01-prog/auditgym](https://github.com/ibrahimerbol01-prog/auditgym) | `3ea176920667ec1401cb93b18b18d38da20f7c73` | adapter 24/26, pair 5/5; `python -m auditgym.verify` и `train/campaign.py --quick` повторены; [Actions 45041207429](https://github.com/ibrahimerbol01-prog/auditgym/actions/runs/45041207429) success |
| [amirlankalm/genesis-fintech-intuition](https://github.com/amirlankalm/genesis-fintech-intuition) | `5c460…` | adapter 26/26, pair 5/5; репозиторий и trajectories просмотрены; hosted Actions нет |
| [itsaibarr/auditgym](https://github.com/itsaibarr/auditgym) | `2fabb…` | adapter 25/26, pair 5/5; `training/README.md`, SFT-данные и bootstrap-отчёты просмотрены; hosted Actions нет |
| [manskrutoy/auditgym-fintech](https://github.com/manskrutoy/auditgym-fintech) | `aa5c7c098fd01f676c89fdb1c64b544733cad230` | adapter 25/26, pair 5/5; `python -m auditgym check` повторён; [Actions 45042405353](https://github.com/manskrutoy/auditgym-fintech/actions/runs/45042405353) success |
| [k41270075-stack/vouchai](https://github.com/k41270075-stack/vouchai) | `41b1c6856c580d30061858f4292b83e1b1e941a7` | adapter 26/26, pair 0/5; `reports/LEARNING.md`, learner и SFT-наборы просмотрены; [Actions 45040769677](https://github.com/k41270075-stack/vouchai/actions/runs/45040769677) success |
| [MaratelloMaJo/letro](https://github.com/MaratelloMaJo/letro) | `c04…` | adapter 25/26, pair 5/5; red-team/CI просмотрены; [Actions 45028219768](https://github.com/MaratelloMaJo/letro/actions/runs/45028219768) success |
| [alimasik/genesis-hackaton](https://github.com/alimasik/genesis-hackaton) | `412e914120324f5de792f4f1b5d35b8a4be46e8f` | adapter 24/26, pair 5/5; [Actions 45041836071](https://github.com/alimasik/genesis-hackaton/actions/runs/45041836071) success |
| [Goslinuly/audit-rl-env](https://github.com/Goslinuly/audit-rl-env) | SHA раннего среза в raw evidence | adapter 26/26, pair 0/5; release/demo/CI просмотрены |

Сокращённые SHA выше используются только там, где полный SHA уже зафиксирован сырым JSON либо
репозиторием раннего прохода; все поздно изменившиеся кандидаты приведены полностью.

## Остальной каталог и триаж

В повторный поиск и fetch вошли также:

- `Kumisbek-Orken/genesis-audit-env`, `aibarryss/Redline`, `Beka486/audit-rl-env`,
  `rustem190209/genesis-fintech-audit-env`, `thzsqeze/audit-rl-environment`,
  `trapprey/audit-rl-env`, `worldgay/finaudit-env`, `ddemess1/genesis-fin-env` — адаптеры
  измерены в [evidence/adapter-probe/](evidence/adapter-probe/);
- `Kaliakbarb/standout-fin-audit`, `nikogonetno/auditenv`, `sultandosym300-ops/auditforge`,
  `Aibek-S/auditgym`, `aiguzhin/in-audit-rl-gym`, `alver714/auditgym`,
  `Tasel1/fin-audit-gym`, `AurorianJade136/genesis-audit-env`,
  `AyanbergenSaduakassov07/ledgerforge`, `nuradil-student/*`, `Own11/airship`,
  `slynqcode/hackaton`, `mansurt78956-glitch/fin-audit`, `xevrinn/ledger-lab`,
  `abylaichik0w0/rec-genesis-audit`, `Goslinuly/fintech-auditgym` — просмотрены/обновлены в
  триаже, но не вытеснили лидеров по совокупности контракт+данные+agent evidence;
- слабые, пустые или не по кейсу: `nk275c59kk-hub/auditforge`,
  `Boob-op/genesis-fintech-audit-rl`, `kaussaryerzhan-cloud/fintech-rl-audit.`,
  `ayaruu/audit-rl-simulator`, `programmerdream2011/fintech-auditor`,
  `alivenoviop-afk/FinAuditAI`.

Публичные демо vouchai, Goslinuly, auditforge и FinSpector были smoke-проверены в раннем проходе;
скриншоты находятся в [evidence/demos/](evidence/demos/). Наличие страницы не считается баллом.

## Метод поиска

- GitHub repository search: `genesis audit`, `audit rl`, `financial audit`, `fintech audit`,
  `audit env`, `аудит`, `genesis fintech`, `auditor environment`, `ledger audit rl`, `audit gym`;
- GitHub code search: `judge_adapter score_episode make_env`,
  `claimed_amount_minor evidence_ids inspection_steps`,
  `debit_minor credit_minor invoice_refs`, `trajectories.jsonl`;
- фильтры `created:`/`pushed:` для 20–22 ноября; повтор в 16:18 UTC;
- fetch всех кандидатов прежнего обзора и отдельный просмотр поздних коммитов, CI, release и demo.

## Локальные измерения

- Инструменты: [adapter_probe.py](evidence/adapter_probe.py),
  [literal_pair_probe.py](evidence/literal_pair_probe.py),
  [probe_auditlab.py](evidence/probe_auditlab.py).
- Ранние результаты: [adapter-probe/](evidence/adapter-probe/),
  [literal-pairs/](evidence/literal-pairs/),
  [madiyar-auditenv-check.log](evidence/madiyar-auditenv-check.log).
- Поздние JSON и журналы запусков: [refresh-2110/](evidence/refresh-2110/).
- Поздние процессы запускались в `python:3.11-slim` с `--network none`, непривилегированным UID,
  read-only rootfs, `cap-drop=ALL`, `no-new-privileges` и ограничениями CPU/RAM. Точные команды и
  ограничения перечислены в `refresh-2110/RUNS.md`.

## Ограничения источников

- Самооценки команд (`100/100`, «96/96» и т. п.) не считаются официальными результатами.
- Ошибка SSL при попытке Docker build azamxvit связана с certificate chain нашей среды; она не
  засчитана как дефект проекта.
- Полные training claims vouchai, ibrahimerbol и tryHard не перезапускались; это отмечено как
  `[авт.]` или `[код/авт.]`.
- Код конкурентов использовался только для чтения и изолированных запусков; реализация не копировалась.
