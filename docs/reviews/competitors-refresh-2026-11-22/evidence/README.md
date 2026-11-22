# Журналы запусков рецензента (22.11.2026)

Среда: Linux 6.18, 4 CPU, Python 3.11.18 (системный `/usr/bin/python3.11`); для AuditLab — venv с
`requirements.lock`; для itsaibarr и worldgay — venv с `msgspec==0.22.0`, `tzdata==2026.6`, `rich==15.0.0`
(только wheels). Без Docker.

Изоляция кода конкурентов: копия репозитория без `.git`, владелец `nobody`, затем
`timeout 300 unshare -n setpriv --reuid=65534 --regid=65534 --clear-groups env -i HOME=... PATH=/usr/bin:/bin python3.11 -I <probe> <repo> <out> [configs]`.
Сети нет, записи в чужие каталоги нет.

| Файл | Что это |
|---|---|
| `adapter_probe.py`, `adapter-probe/*.json` | 26 пунктов через `judge_adapter` каждой команды (аналоги C/R/M/X; не стенд жюри). Конфигурации с ошибками: AuditLab `{"team_config":{"issue_counts":{...}}}`, Intuition `{"generator":{},"inject":{...}}`, rustem `{"errors":{...}}`, остальные — `{}` |
| `literal_pair_probe.py`, `literal-pairs/*.json` | Буквальный преобразователь ID (трактовка U2), 5 эпизодов (seed 11–15) на проект |
| `probe_auditlab.py`, `probe_auditlab.json` | AuditLab: размеры по умолчанию, суммы и доли по 10 seed, перебор seed без ключа и с ключом |
| `madiyar-auditenv-check.log` | `python3.11 -m auditenv.check` в изоляции на `911845ff`: ALL CHECKS PASSED, 162,7 с |
| `demos/*.png` | Скриншоты публичных демо конкурентов (Playwright, 1280×800) |

Повтор для AuditLab из корня репозитория:

```sh
python docs/reviews/competitors-refresh-2026-11-22/evidence/probe_auditlab.py NEW_DIR
python docs/reviews/competitors-refresh-2026-11-22/evidence/adapter_probe.py . OUT.json '[{"team_config":{"issue_counts":{"duplicate_invoice":1,"payment_allocation":1,"period_cutoff":1}}}]'
python docs/reviews/competitors-refresh-2026-11-22/evidence/literal_pair_probe.py . OUT.json '[{"team_config":{"issue_counts":{"duplicate_invoice":1,"payment_allocation":1,"period_cutoff":1}}}]'
```

Время в `probe_auditlab.json` (9,7 с и 23 с) зависит от машины.
