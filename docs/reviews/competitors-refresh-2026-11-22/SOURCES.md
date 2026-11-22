# Источники обновлённого разбора — 22.11.2026

Все SHA — ветки по умолчанию на момент клонирования (13:56–15:50 UTC 22.11.2026). Код конкурентов
выполнялся только в изоляции: `nobody`, `unshare -n`, `env -i`. CI конкурентов не проверен:
GitHub API для чужих репозиториев в этой среде закрыт.

## AuditLab

| Что | Ссылка |
|---|---|
| `main`, исследованный код | [`5ced9b9`](https://github.com/Mustiks07/AuditLab/commit/5ced9b948b27c9306e788e62d99179d49852771a); итоговый `main` [`c9ce1df`](https://github.com/Mustiks07/AuditLab/commit/c9ce1dfce6855eee431b881fef6c93c623852dd1), код продукта тот же |
| CI | [run 45028606259](https://github.com/Mustiks07/AuditLab/actions/runs/45028606259) (success, `5ced9b9`) |
| Релиз | [v0.1.0-rc.5](https://github.com/Mustiks07/AuditLab/releases/tag/v0.1.0-rc.5), код `920b251ee3b1c12f9bc5ffe0eca3b5cb1bf8d78a` |
| Профиль 1.3 и ключ | [`auditlab/generator/profile13.py`](https://github.com/Mustiks07/AuditLab/blob/c9ce1dfce6855eee431b881fef6c93c623852dd1/auditlab/generator/profile13.py), [`auditlab/runner/genesis.py` L42](https://github.com/Mustiks07/AuditLab/blob/c9ce1dfce6855eee431b881fef6c93c623852dd1/auditlab/runner/genesis.py#L42) |
| Формы ссылок | [`docs/GENESIS_ADAPTER.md`](https://github.com/Mustiks07/AuditLab/blob/c9ce1dfce6855eee431b881fef6c93c623852dd1/docs/GENESIS_ADAPTER.md) («Формы ссылок») |
| Измерение N2 (оракул 4/24) | [`docs/reviews/jury-compatibility-v3/README.md`](https://github.com/Mustiks07/AuditLab/blob/c9ce1dfce6855eee431b881fef6c93c623852dd1/docs/reviews/jury-compatibility-v3/README.md) |
| Мутанты оценщика (22: 15 убито, 5 выжило) | [`mutation-report.json`](https://github.com/Mustiks07/AuditLab/blob/c9ce1dfce6855eee431b881fef6c93c623852dd1/docs/research/independent-assurance-v2/evidence/mutation-report.json) |
| A/B/C | [`ABC_LIVE_REPORT.md`](https://github.com/Mustiks07/AuditLab/blob/c9ce1dfce6855eee431b881fef6c93c623852dd1/docs/research/agent-improvement-v2/ABC_LIVE_REPORT.md) |
| Единственный `trajectories.jsonl` в дереве (вне тестов) | [`docs/demo/sample_export/TRAIN-S02-reconcile/trajectories.jsonl`](https://github.com/Mustiks07/AuditLab/blob/c9ce1dfce6855eee431b881fef6c93c623852dd1/docs/demo/sample_export/TRAIN-S02-reconcile/trajectories.jsonl) (7 строк) |
| Описание наборов | [`datasets/README.md`](https://github.com/Mustiks07/AuditLab/blob/c9ce1dfce6855eee431b881fef6c93c623852dd1/datasets/README.md) — заголовок «Открытые наборы AuditLab 1.1», 240 строк; про `open-1.3` отдельно |

## Главные конкуренты (глубокий разбор)

| Проект | Кат. | Ветка, SHA, последнее изменение | Ключевые свидетельства |
|---|---|---|---|
| [madiyar-abi/genesis](https://github.com/madiyar-abi/genesis) | A (README: Genesis 2026 FinTech) | `main` [`911845ff`](https://github.com/madiyar-abi/genesis/tree/911845ff6b90c30add9906575e2e7d57e6049e86), 22.11 13:22 +05 | `python3.11 -m auditenv.check` → ALL CHECKS PASSED, 162,7 с [изм.] ([лог](evidence/madiyar-auditenv-check.log)); ключ жюри — README «For the jury», `auditenv/grader_key.py`; [`trajectories.jsonl`](https://github.com/madiyar-abi/genesis/blob/911845ff6b90c30add9906575e2e7d57e6049e86/trajectories.jsonl) |
| [amirlankalm/genesis-fintech-intuition](https://github.com/amirlankalm/genesis-fintech-intuition) | A (Team Intuition) | `main` [`5c460aea`](https://github.com/amirlankalm/genesis-fintech-intuition/tree/5c460aea5241613c708a17b4acd5da88f4790c6f), 21.11 18:30 +05 (без изменений) | Зонд 26/26, пары 5/5 [изм.]; [`src/genesis_audit/sandbox.py`](https://github.com/amirlankalm/genesis-fintech-intuition/blob/5c460aea5241613c708a17b4acd5da88f4790c6f/src/genesis_audit/sandbox.py); `tests/jury/` |
| [itsaibarr/auditgym](https://github.com/itsaibarr/auditgym) | A (Team tryHard) | `master` [`2fabb0df`](https://github.com/itsaibarr/auditgym/tree/2fabb0df0be37c7307863a71d938cbe707de7dfa), 22.11 18:50 +05 | Адаптер с коммита `02f4e3e`; зонд 25/26, пары 5/5 [изм.]; [`training/README.md`](https://github.com/itsaibarr/auditgym/blob/2fabb0df0be37c7307863a71d938cbe707de7dfa/training/README.md) (раунды 1–4, наборы A–D, бутстрап) [авт.]; нужны `msgspec`, `tzdata` |
| [MaratelloMaJo/letro](https://github.com/MaratelloMaJo/letro) | A (README: REC Genesis Hackathon) | `master` [`c04c0f03`](https://github.com/MaratelloMaJo/letro/tree/c04c0f03ddb00b168b454f9fb830aa376fb69006), 22.11 18:34 +05 | Зонд 25/26 (`amount_tolerance_minor` по умолчанию 1, [`judge_adapter.py` L287](https://github.com/MaratelloMaJo/letro/blob/c04c0f03ddb00b168b454f9fb830aa376fb69006/judge_adapter.py#L287)), пары 5/5 [изм.]; `letro.redteam` |
| [k41270075-stack/vouchai](https://github.com/k41270075-stack/vouchai) | A (README: Genesis Hackathon 2026) | `main` [`0e741228`](https://github.com/k41270075-stack/vouchai/tree/0e7412280196175b57205c0208d53b92203dc702), 21.11 21:34 +05 | Зонд 26/26, пары 0/5 [изм.]; демо [vouchai-ai.netlify.app](https://vouchai-ai.netlify.app/) (HTTP 200, [скриншот](evidence/demos/vouchai.png)) |

## Остальные кандидаты

| Проект | Кат. | SHA | Последнее изменение | Наблюдение |
|---|---|---|---|---|
| [Goslinuly/audit-rl-env](https://github.com/Goslinuly/audit-rl-env) | B (хакатон 19–22.11, команда из 4) | `4cd66a9a45ca` | 22.11 15:44 +05 | **Новый**; зонд 26/26, пары 0/5 (составные `invoices:…`); демо [railway](https://explorer-production-573177ce.up.railway.app) ([скриншот](evidence/demos/goslinuly-explorer.png)) |
| [Kumisbek-Orken/genesis-audit-env](https://github.com/Kumisbek-Orken/genesis-audit-env) | A | `3dbe0bc53117` | 21.11 | 26/26, пары 5/5 |
| [aibarryss/Redline](https://github.com/aibarryss/Redline) | A | `30e6e271c250` | 21.11 | 26/26, пары 5/5 |
| [Beka486/audit-rl-env](https://github.com/Beka486/audit-rl-env) | B | `f1d689d8d6ba` | 22.11 | **Новый**; 26/26, пары 5/5; `AUDITENV_SECRET` |
| [worldgay/finaudit-env](https://github.com/worldgay/finaudit-env) | B | `c8a99c7423a1` | 22.11 | **Новый**; 26/26 (нужен `tzdata`), пары 5/5 |
| [rustem190209/genesis-fintech-audit-env](https://github.com/rustem190209/genesis-fintech-audit-env) | A | `d10b0f41cdab` | 21.11 | 26/26, пары 5/5 |
| [thzsqeze/audit-rl-environment](https://github.com/thzsqeze/audit-rl-environment) | B | `04f0e01ede45` | 21.11 | 26/26, пары 5/5 |
| [trapprey/audit-rl-env](https://github.com/trapprey/audit-rl-env) | B | `f224ec71d5a0` | 22.11 | 26/26, пары 5/5 |
| [alimasik/genesis-hackaton](https://github.com/alimasik/genesis-hackaton) | A | `e5b88994e9b7` | 21.11 | 24/26: M06, X07 дают invalid вместо FP; пары 5/5 |
| [ddemess1/genesis-fin-env](https://github.com/ddemess1/genesis-fin-env) | A | `728d6846f780` | 21.11 | 6/26: `steps_left`, калькулятор-объект, `score_episode` → `reward` ([L45](https://github.com/ddemess1/genesis-fin-env/blob/728d6846f7801a734c08287a95d8c5355df0cc89/judge_adapter.py#L45)) |
| [Kaliakbarb/standout-fin-audit](https://github.com/Kaliakbarb/standout-fin-audit) | A | `6a18137cd415` | 21.11 12:19 | Без изменений; `judge_adapter.py` нет |
| [nikogonetno/auditenv](https://github.com/nikogonetno/auditenv) | B | `1d3d57caf9a5` | 22.11 | **Новый**; около 477 тестов, CI-файл; `score_episode` не найден |
| [sultandosym300-ops/auditforge](https://github.com/sultandosym300-ops/auditforge) | A (README: Хакатон Genesis) | `94571856b90f` | 22.11 | **Новый**; демо [vercel](https://auditforge-gilt.vercel.app) («REINFORCE … 21 вес» [авт.]); адаптера нет |
| [manskrutoy/auditgym-fintech](https://github.com/manskrutoy/auditgym-fintech), [Aibek-S/auditgym](https://github.com/Aibek-S/auditgym) | B | `d0efabafd98b`, `aa5010ff5cf2` | 22.11 | **Новые**; адаптера нет |
| [aiguzhin/in-audit-rl-gym](https://github.com/aiguzhin/in-audit-rl-gym) | B | `67269032bf41` | 22.11 | Демо [onrender](https://fin-audit-rl-gym.onrender.com) ([скриншот](evidence/demos/finspector.png)) |
| azamxvit/audit-rl-env, alver714/auditgym, Tasel1/fin-audit-gym, AurorianJade136/genesis-audit-env | A/B | `82e08c90fcf9`, `5e91d930ca46`, `7a34c0ae0b18`, `f63b00c3ba41` | 22.11 | Мелкие обновления; не углублялся |
| AyanbergenSaduakassov07/ledgerforge, nuradil-student/…, Own11/airship, slynqcode/hackaton, mansurt78956-glitch/fin-audit, xevrinn/ledger-lab, abylaichik0w0/rec-genesis-audit, Goslinuly/fintech-auditgym | A/B | см. обзор 21.11 | 19–22.11 | Без существенных изменений |
| nk275c59kk-hub/auditforge, Boob-op/genesis-fintech-audit-rl, kaussaryerzhan-cloud/fintech-rl-audit., ayaruu/audit-rl-simulator, programmerdream2011/fintech-auditor, alivenoviop-afk/FinAuditAI | B (слабые или пустые) | — | 19–22.11 | MVP, пустые или не по кейсу |

## Внешние аналоги (категория C, из обзора 21.11)

ESCTR (OpenEnv Hackathon, GRPO), FinancialAuditBench, arXiv 2506.17282, FinMaster — для
сравнения подходов, не конкуренты.

## Как искали

- Поиск кода GitHub: `"judge_adapter" score_episode make_env`, `claimed_amount_minor evidence_ids inspection_steps`, `debit_minor credit_minor invoice_refs`.
- Поиск репозиториев: `genesis audit`, `audit rl`, `financial audit`, `fintech audit`, `audit env`, `аудит`, `genesis fintech`, `auditor environment`, `ledger audit rl`, `audit gym` с фильтрами `created:`/`pushed:` от 20–21.11.
- Сайты и демо из README кандидатов.
- Все кандидаты из обзора 21.11 перекачаны (`git fetch`).
