# Повтор проверки подсказок на профиле 1.3

Используйте зафиксированные зависимости и чистый каталог вывода. Прогон Claude Pro 2
был выполнен с Python 3.12.14 и tzdata 2026.6 на `acec2ee9f1dc26210bc05b279f3c612bfd787de1`.
В `efeb99a1131d02d60855a20a702b5b8531b836ee` генератор и рецепты совпадают
побайтно. Штатный запуск #58 оставляет 1.2 для набора B. Для повтора ниже
явно выбран 1.3. C использует default адаптера.

```sh
python - <<'PY'
from collections import Counter
from pathlib import Path

from review.data_shortcuts import companies, datasets, diversity
from review.data_shortcuts.analysis import analyse, load
from review.data_shortcuts.heuristics import evaluate
from review.data_shortcuts.run import plan_c_fresh

root = Path.cwd()
out = root / "artifacts" / "profile13-recheck"
out.mkdir(parents=True, exist_ok=False)

def generate(name, plan):
    items = datasets.generate(root, plan, out / name / "generation")
    counts = Counter(item["status"] for item in items)
    assert counts == {"generated": len(plan)}, counts
    return items

def partition(item):
    return "explore" if int(item["family"][1:]) % 2 else "evaluate"

def cohort(name, per_stratum, start, prefix, part):
    plan = datasets.plan_b(out / name, per_stratum, start, prefix)
    for item in plan:
        item["config"] = {**item["config"], "config_version": "1.3"}
    items = generate(name, plan)
    validation = diversity.run(items)
    paired = companies.paired(companies.analyse(items, part, 0)["rows"])
    print(name, len(items), validation["situations"], validation["clean_control_p9"],
          paired["treasury_journals"])
    return load(items, part)

study = cohort("B13", 8, 9330001, "B", partition)
fresh = cohort("B13-fresh", 4, 9370001, "G", lambda _: "evaluate")

rules = analyse(study, resamples=10000)["rules"]
print("B13 rule statuses", Counter(rule["status"] for rule in rules))
for rule in rules:
    if rule["status"] in {"confirmed", "hypothesis"}:
        rows = [row for row in fresh if row["table"] == rule["table"]]
        print("fresh transfer", rule["feature"], rule["rule"], rule["target"],
              evaluate(rows, rule["feature"], rule["rule"], rule["target"], 10000))

plan = plan_c_fresh(out / "C13-fresh", 6)
items = generate("C13-fresh", [item for item in plan if "config_version" not in item["config"]])
rows = load(items, lambda _: "evaluate")
for table, feature, rule in [
    ("ledger", "hour", ("<=", 0)),
    ("ledger", "ratio_linked", ("<=", 0.6)),
    ("invoices", "amount_repeat", (">=", 3)),
]:
    for label in ("affected", "evidence"):
        print("C13-fresh", feature, label,
              evaluate([row for row in rows if row["table"] == table], feature, rule, label, 10000))
PY
```

Ожидаемые знаменатели — 232 снимка B13, 116 свежих B13, 30 C13.
Для B13 и свежих B13 цель P9 дала 0/104 и 0/52. Оба набора остаются
доступными как наборы отдельных финансовых сценариев.
