# Передача офлайн-комплекта независимому получателю

Документ для человека, который получает архив AuditLab и проверяет его **без помощи авторов**.

**Опубликованный кандидат:** [v0.1.0-rc.3](https://github.com/Mustiks07/AuditLab/releases/tag/v0.1.0-rc.3),
исходный SHA `b7a1857a51bf47bb36e42699988b38b4d897fa1f`.
Архив `AuditLab-v0.1.0-rc.3-offline.zip`: **330 616 243 B**, SHA-256
`a5363412f4509b7ae39aa21aa9eee27154e2af5357347f7d02d7dc31ddb52b81`.
Публикация и хеши проверены через GitHub; повторите сверку файла после скачивания.

RC2 — исторический кандидат. Его результаты и обнаруженные тогда дефекты сохранены без изменения в
[RC2_AGENT_CHECK.md](RC2_AGENT_CHECK.md). Исправления финализации, генератора, сохранения результатов,
отмены запуска и консоли относятся к новому SHA. Старые измерения к нему не переносятся.
Встроенные в `source.tar.gz` документы зафиксированы вместе с кодом; после публикации актуальную
инструкцию получателя берите со страницы релиза или из
[HANDOFF.md в main](https://github.com/Mustiks07/AuditLab/blob/main/docs/demo/HANDOFF.md).

Что нельзя делать во время проверки: устанавливать пакеты проекта (`pip install`), собирать образы, скачивать
образы из registry, подменять комплект исходным checkout. Всё нужное лежит в архиве.

## 1. Требования к машине

| Что | Требование | Как проверить |
|---|---|---|
| ОС | Linux x86_64. Windows и Docker Desktop этим комплектом не заявлены | `uname -srm` |
| Python | **Ровно 3.11**: launcher отказывает другим версиям (`Offline launch failed: launcher requires Python 3.11`). В Ubuntu 24.04 по умолчанию 3.12; Python 3.11 нужно установить заранее, до офлайн-проверки | `python3.11 --version` |
| Docker | Локальный Docker Engine с Linux-контейнерами и сокетом `/var/run/docker.sock`. Удалённые contexts и `DOCKER_HOST` launcher игнорирует. Пользователь должен иметь доступ к сокету | `docker --host unix:///var/run/docker.sock version`, `docker --host unix:///var/run/docker.sock info` |
| cgroup | **v2** для подтверждения лимитов ресурсов. На v1 проверка функциональная, ресурсы остаются «не проверено», коды 2 | `docker --host unix:///var/run/docker.sock info --format '{{.CgroupVersion}}'` |
| CPU | Минимум 2 CPU в affinity процесса (берутся первые два, или явно `--cpu-set 0,1`) | `nproc` |
| Диск | Место для ZIP, распакованного комплекта, загруженных Docker-слоёв и результатов. Размер ZIP берите со страницы RC3, payload — из `manifest.json`; они не равны суммарному месту на диске | `df -h .`, `docker --host unix:///var/run/docker.sock system df` |
| Сеть | Не нужна для проверки. Все контейнеры проверки запускаются с `--network none`. Демо использует bridge только для порта на `127.0.0.1` | |

Контроллер и один одновременно работающий контейнер политики либо verifier ограничены суммарно **3 GiB**.
Это не измерение памяти всей машины: ОС, Docker daemon и файловый кеш хоста требуют отдельной
проверки. Не записывайте «весь хост уложился в 4 GB» по одним контейнерным лимитам.

## 2. Две конфигурации Docker

Комплекты начиная с `v0.1.0-rc.2` работают на обоих хранилищах образов, переключать демон не нужно. Узнайте
своё хранилище и запишите вывод в журнал:

```sh
docker --host unix:///var/run/docker.sock info --format '{{.Driver}} {{.DriverStatus}} cgroup={{.CgroupVersion}}'
```

| Вывод содержит | Хранилище | Как адресуется образ |
|---|---|---|
| `overlay2` | классическое | config ID из `manifest.json` (`images.<роль>.id`) |
| `io.containerd.snapshotter.v1` (Driver `overlayfs`) | containerd image store, по умолчанию в **новых** установках Docker Engine 29 | OCI manifest digest; launcher выбирает проверенный ID сам |

На машине, обновлённой со старого Docker, с уже существующими образами классического хранилища, Docker 29
может остаться на `overlay2`. Хранилище включается явно настройкой
`{"features": {"containerd-snapshotter": true}}` (или `false`) в `/etc/docker/daemon.json` с перезапуском
демона. Образы другого хранилища после переключения не видны. Не меняйте рабочий демон с чужими
контейнерами только ради проверки.

Для `demo` и `replay` всегда берите ID командой `python3.11 offline.py image --role runtime`, а не из
`manifest.json`: на containerd это другой идентификатор.

## 3. Получение и проверка хеша архива

Скачайте в новый пустой каталог архив и файл контрольных сумм со страницы RC3.
Скачивание выполняется **до** отключения сети; сам запуск не требует интернета.

```sh
TAG=v0.1.0-rc.3
ZIP="AuditLab-${TAG}-offline.zip"
SUMS="AuditLab-${TAG}-SHA256SUMS.txt"
```

Имена скачанных файлов должны совпадать с `$ZIP` и `$SUMS`. В том же терминале:

```sh
test -f "$ZIP" && sha256sum -c "$SUMS" --ignore-missing
```

Ожидается именно `AuditLab-v0.1.0-rc.3-offline.zip: OK`, exit 0. Отсутствие файла, ошибка проверки
или строка о том, что ни один файл не проверен, не являются успехом. Не используйте checksum RC2.

Архив **не содержит общего каталога**: шесть файлов лежат в корне. Распаковывайте в новый пустой каталог:

```sh
mkdir bundle
unzip "$ZIP" -d bundle
cd bundle
```

Ожидаемые файлы: `offline.py`, `images.tar`, `source.tar.gz`, `licenses.json`, `manifest.json`, `SHA256SUMS`.

## 4. Первый офлайн-запуск

Все команды — из каталога `bundle`. Запишите в журнал вывод, код возврата (`echo $?`) и длительность.

| № | Команда | Ожидаемый вывод и код |
|---|---|---|
| 1 | `python3.11 offline.py check` | Проверка payload и каталога образов, **exit 0**, Docker не нужен. Source должен быть `b7a1857a51bf47bb36e42699988b38b4d897fa1f` |
| 2 | `python3.11 offline.py verify --suite quick --output-root ./results` | **347 тестов** в 8 наборах, без пропусков. **Exit 0**, если все исполненные проверки и фактические лимиты контроллера подтверждены; **exit 2**, если ресурсы не подтверждены. Это быстрые проверки, не полная приёмка |
| 3 | `python3.11 offline.py verify --suite rehearsal --output-root ./results` | Те же тесты, 20 снимков, 4 политики × 20 попыток, 20 пар и независимые проверки исходных книг. Ожидаемая строгая приёмка — **exit 2**, пока OPS-01 / OPS-02 остаются «не проверено» |
| 4 | evidence checker (ниже) | При полном внутреннем прогоне и подтверждённых ресурсах: `internal_rehearsal_passed`, `failures: []`, `missing: []`, **exit 0**. Недостающие ресурсные свидетельства — `not_checked`, **exit 2**; обнаруженная ошибка — **exit 1** |
| 5 | `python3.11 offline.py image --role runtime` | Одна строка `sha256:…`, **exit 0**, после загрузки образов шагом 2 или 3 |

Число 347 подтверждено CI исходного SHA; оно не является утверждением, что получатель уже прошёл
проверку. Длительность, image IDs, пиковую память и результат своего запуска записывайте фактически.

Evidence checker использует только стандартную библиотеку хостового Python. Извлеките поставленные
исходники, не клонируя репозиторий и не устанавливая зависимости:

```sh
mkdir -p source
tar -xzf source.tar.gz -C source
```

Задайте `RESULTS` равным точному каталогу `results/verify-…` **завершённого шага 3**:

```sh
RESULTS=./results/verify-REPLACE-WITH-YOUR-RUN
python3.11 -I source/tools/check_rehearsal_evidence.py "$RESULTS" --bundle . --output evidence-summary.json
```

**Различайте три результата.** Быстрые проверки могут завершиться кодом 0 при ещё открытой полной
матрице SPEC. Полная репетиция возвращает 2 из-за непроверенных обязательных пунктов OPS-01 / OPS-02;
проверьте причины в `verify/report.json`, а не принимайте любой код 2 за ожидаемый. Отдельный evidence
checker может вернуть 0, подтвердив именно внутренний прогон, архив и контейнерные ресурсы. Это не
подтверждает внешний адаптер жюри, независимую человеческую приёмку или память всей ОС.

Коды launcher: **0** — выбранные проверки и ресурсы подтверждены; **1** — ошибка; **2** — неполная
приёмка/ресурсные свидетельства; **130** — SIGINT; **143** — SIGTERM. Для обоих сигналов проверьте
поле `cleanup`: прерывание не превращается в завершённый успешный прогон.

Во втором терминале на время шагов 2–3 запишите события Docker:

```sh
docker --host unix:///var/run/docker.sock events --format '{{.Type}} {{.Action}} {{.Actor.Attributes.name}}'
```

Ожидается: ни одного `image pull`; события `network connect` только с сетью `none`.

## 5. Ожидаемые файлы результатов

Каждый вызов `verify` создаёт новый `results/verify-<UTC>-<id>/` и никогда не перезаписывает прежние.

| Файл | Что проверить |
|---|---|
| `launch.json` | `status`, `exit_code`, `source_revision`, `loaded_images` (три роли, ID и OCI digest), `launch_id`, фактические времена, `cleanup.status` и `cleanup.remaining_containers` |
| `controller-resources.json` | Фактические лимиты контроллера; на cgroup v1 `resource_limits_verified` не `true` |
| `verify/report.json` | `status`, `exit_code`, `commit`, `python`, `checks` по наборам; после репетиции — `rehearsal.summary` |
| `verify/report.md` | Человекочитаемая сводка того же отчёта |
| `verify/<набор>.json`, `.log`, `-artifacts/` | ID каждого теста и журналы |
| `verify/rehearsal/` | `attempts/<политика>/internal-NN/final.json` (сдача и оценка), `snapshots/`, `independent/`, `pairs/` |

Сводка нового прогона — внутренний диагностический результат, не конкурсный балл. Для каждой политики
нужно `planned = measured = 20`, `score_status = complete`, `failed = not_checked = 0`.
При недостающих измерениях сводные `micro` и `mean_reward` равны `null`; `measured_only` содержит
только частичную выборку и не заменяет полный результат. Не подставляйте числа RC2 вместо измерений RC3.

```sh
python3.11 - "$RESULTS/verify/report.json" <<'PYCODE'
import json, sys
with open(sys.argv[1], encoding="utf-8") as stream:
    report = json.load(stream)
for policy, row in report["rehearsal"]["summary"].items():
    print(policy, "planned/measured:", row["planned"], row["measured"],
          "status:", row["score_status"], "failed:", row["failed"],
          "not_checked:", row["not_checked"], "micro:", row["micro"],
          "mean_reward:", row["mean_reward"])
PYCODE
```

Сравните результат с опубликованным evidence RC3 **для этого source SHA и вашего хранилища**.
Самостоятельные финансовые результаты и показатели ресурса не заменяются статусом CI.

## 5а. Обязательные политики на 10 train и 10 dev

Критерий OPS-02 в SPEC §14 требует приложить результаты обязательных политик на открытых наборах
`datasets/open/train` и `datasets/open/dev` (по 10 компаний). Репетиция §4 их не включает: в ней 20 внутренних
снимков. Наборы лежат внутри runtime-образа. Каждая обычная политика запускается в отдельном контейнере без
сети, как в `verify`. Выполняйте после шага 2 или 3, когда образы загружены.

Всё делает один инструмент `open_sets.py` на стандартной библиотеке. Он записывает по каждой попытке код
возврата, разобранный ответ или ошибку разбора, а затем строго проверяет полноту результатов.

**Где взять инструмент.** В комплектах, собранных после этого изменения, он лежит в исходниках:

```sh
mkdir -p source && tar -xzf source.tar.gz -C source
cp source/auditlab/demo/open_sets.py open_sets.py
```

В опубликованном `v0.1.0-rc.3` его нет. Для RC3 создайте файл командой ниже: это тот же текст, байт в байт,
что проверяет тест репозитория. Сам архив RC3 при этом не меняется.

```sh
cat > open_sets.py <<'OPEN_SETS_PY'
"""Mandatory policies on the open train/dev sets for the receiver (OPS-02), with a strict summary.

Standard library only. The same file works with an unchanged v0.1.0-rc.3 bundle (copy it from
docs/demo/HANDOFF.md, section 5a) and with later bundles (``source/auditlab/demo/open_sets.py``).

Inside the runtime image (policies run as sibling containers without network, like ``verify``)::

    python - run --policy-image IMAGE --results /results < open_sets.py > summary.jsonl

On the host::

    python3.11 -I open_sets.py summarize summary.jsonl --datasets source/datasets/open --output open-sets.json

``run`` records, for every attempt, the child return code, the parsed final JSON line or the parse
error, and bounded stderr. It never invents a result. ``summarize`` accepts the run as complete only
when every expected (split, episode, policy) appears exactly once, the child exited with 0, the
status is ``submitted`` and score/reward are present. Otherwise the overall metrics of the affected
policy are ``null`` ("не проверено"), the measured part is reported separately with its denominator,
and the exit code is not 0: 1 for failed or malformed attempts, 2 for missing attempts only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SPLITS = ("train", "dev")
POLICIES = ("random", "reconcile", "flag_all", "oracle")
EPISODES_PER_SPLIT = 10
TAIL = 2000


# ----------------------------------------------------------------------------- run
def attempt_command(split: str, episode: str, policy: str, image: str, results: str) -> list[str]:
    snapshot = f"datasets/open/{split}/{episode}"
    command = [sys.executable, "-m", "auditlab.runner"]
    if policy == "oracle":
        command += ["oracle", "--snapshot", snapshot]
    else:
        command += ["policy", "--snapshot", snapshot, "--name", policy, "--image", image]
    return command + ["--mode", "closed", "--trusted-dir", f"{results}/attempts/{split}/{episode}/{policy}"]


def record(split: str, episode: str, policy: str, returncode: int, stdout: str, stderr: str) -> dict:
    """One attempt as observed: the result is the last JSON line of stdout, or null with the parse error."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    result, parse_error = None, None
    if not lines:
        parse_error = "no output"
    else:
        try:
            result = json.loads(lines[-1])
        except ValueError as error:
            parse_error = f"last stdout line is not JSON: {error}"
        else:
            if not isinstance(result, dict):
                result, parse_error = None, "last stdout line is not a JSON object"
    return {"split": split, "episode": episode, "policy": policy, "returncode": returncode,
            "result": result, "parse_error": parse_error, "stderr_tail": stderr[-TAIL:]}


def run(image: str, results: str, root: Path) -> int:
    failed = 0
    for split in SPLITS:
        for episode in sorted(os.listdir(root / split)):
            for policy in POLICIES:
                try:
                    done = subprocess.run(attempt_command(split, episode, policy, image, results),
                                          capture_output=True, text=True, timeout=300)
                    row = record(split, episode, policy, done.returncode, done.stdout, done.stderr)
                except (OSError, subprocess.SubprocessError) as error:
                    row = record(split, episode, policy, -1, "", f"{type(error).__name__}: {error}")
                failed += row["returncode"] != 0 or row["result"] is None
                print(json.dumps(row, ensure_ascii=False), flush=True)
    return 1 if failed else 0


# ----------------------------------------------------------------------- summarize
def attempt_problem(row: dict) -> str | None:
    if row.get("returncode") != 0:
        return f"процесс завершился с кодом {row.get('returncode')}"
    if row.get("parse_error"):
        return f"ответ не разобран: {row['parse_error']}"
    result = row.get("result")
    if not isinstance(result, dict):
        return "нет результата"
    if result.get("status") != "submitted":
        return f"статус {result.get('status')!r}, а не submitted"
    score = result.get("score")
    if not isinstance(score, dict) or any(type(score.get(k)) is not int for k in ("tp", "fp", "fn")):
        return "нет score с целыми tp/fp/fn"
    if type(result.get("reward")) not in (int, float):
        return "нет reward"
    return None


def expected_episodes(datasets: Path | None) -> dict[str, list[str]] | None:
    if datasets is None:
        return None
    return {split: sorted(p.name for p in (datasets / split).iterdir() if p.is_dir()) for split in SPLITS}


def summarize(lines: list[str], expected: dict[str, list[str]] | None = None) -> dict:
    problems: list[str] = []
    malformed = False  # unreadable, empty, duplicated or unexpected rows: an error, not just a gap
    rows: dict[tuple, dict] = {}
    duplicated: set[tuple] = set()
    if not any(line.strip() for line in lines):
        problems.append("файл результатов пуст")
        malformed = True
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            key = (row["split"], row["episode"], row["policy"])
        except (ValueError, TypeError, KeyError) as error:
            problems.append(f"строка {number} не разобрана: {type(error).__name__}")
            malformed = True
            continue
        if key in rows:
            duplicated.add(key)
            malformed = True
            problems.append(f"повтор строки {'/'.join(key)}")
            continue
        rows[key] = row

    seen = {split: sorted({k[1] for k in rows if k[0] == split}) for split in SPLITS}
    episodes = expected or seen
    for split in SPLITS:
        if expected is None and len(seen[split]) != EPISODES_PER_SPLIT:
            problems.append(f"{split}: компаний {len(seen[split])}, ожидается {EPISODES_PER_SPLIT}")
        if expected is not None and len(expected[split]) != EPISODES_PER_SPLIT:
            problems.append(f"{split}: в наборе данных {len(expected[split])} компаний, ожидается {EPISODES_PER_SPLIT}")
            malformed = True
    for key in sorted(rows):
        if key[0] not in SPLITS or key[2] not in POLICIES or key[1] not in episodes.get(key[0], []):
            problems.append(f"неожиданная строка {'/'.join(key)}")
            malformed = True

    policies, failed_any, missing_any = {}, False, False
    for split in SPLITS:
        for policy in POLICIES:
            planned = len(episodes[split])
            measured, failures, missing = [], [], []
            for episode in episodes[split]:
                key = (split, episode, policy)
                if key in duplicated:
                    failures.append({"episode": episode, "problem": "повтор строки: результат неоднозначен"})
                elif key not in rows:
                    missing.append(episode)
                else:
                    problem = attempt_problem(rows[key])
                    if problem:
                        failures.append({"episode": episode, "problem": problem})
                    else:
                        measured.append(rows[key]["result"])
            failed_any |= bool(failures)
            missing_any |= bool(missing)
            part = None
            if measured:
                part = {"measured": len(measured), "planned": planned,
                        "tp": sum(r["score"]["tp"] for r in measured),
                        "fp": sum(r["score"]["fp"] for r in measured),
                        "fn": sum(r["score"]["fn"] for r in measured),
                        "mean_reward": round(sum(r["reward"] for r in measured) / len(measured), 6)}
            complete = len(measured) == planned == EPISODES_PER_SPLIT and not failures and not missing
            policies[f"{split}/{policy}"] = {
                "planned": planned, "measured": len(measured), "failed": failures, "missing": missing,
                "status": "complete" if complete else "не проверено",
                "micro": {k: part[k] for k in ("tp", "fp", "fn")} if complete else None,
                "mean_reward": part["mean_reward"] if complete else None,
                "measured_only": None if complete else part,
            }
    ok = not problems and not failed_any and not missing_any
    exit_code = 0 if ok else 1 if (failed_any or malformed) else 2
    return {"status": "complete" if ok else "не проверено", "exit_code": exit_code,
            "expected_attempts": sum(len(v) for v in episodes.values()) * len(POLICIES),
            "rows": len(rows), "problems": problems, "policies": policies,
            "scope": "Internal diagnostics on the open train/dev sets; not a contest score."}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="open_sets.py", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("run", help="inside the runtime image: run 4 policies on 10 train + 10 dev")
    r.add_argument("--policy-image", required=True)
    r.add_argument("--results", default="/results")
    r.add_argument("--datasets", type=Path, default=Path("datasets/open"))
    s = sub.add_parser("summarize", help="on the host: strict summary of the JSONL written by run")
    s.add_argument("file", type=Path)
    s.add_argument("--datasets", type=Path, default=None,
                   help="datasets/open of the bundle sources: the exact expected episode IDs")
    s.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.command == "run":
        return run(args.policy_image, args.results, args.datasets)
    lines = args.file.read_text(encoding="utf-8").splitlines() if args.file.exists() else []
    report = summarize(lines, expected_episodes(args.datasets))
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for name, row in report["policies"].items():
        if row["status"] == "complete":
            print(f"{name:<18} {row['measured']}/{row['planned']} TP/FP/FN {row['micro']['tp']}/{row['micro']['fp']}/"
                  f"{row['micro']['fn']} mean_reward {row['mean_reward']}")
        else:
            part = row["measured_only"]
            measured = (f"измерено {part['measured']}/{part['planned']}: TP/FP/FN {part['tp']}/{part['fp']}/{part['fn']}, "
                        f"среднее по измеренным {part['mean_reward']}") if part else "измерений нет"
            print(f"{name:<18} НЕ ПРОВЕРЕНО: mean_reward null; {measured}; ошибок {len(row['failed'])}, "
                  f"пропусков {len(row['missing'])}")
    for problem in report["problems"]:
        print(f"проблема: {problem}")
    print(f"status: {report['status']}, exit {report['exit_code']}")
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
OPEN_SETS_PY
```

**Запуск** в поставленном runtime-образе, результаты в новом каталоге `open-results`:

```sh
AUDITLAB_RUNTIME_IMAGE=$(python3.11 offline.py image --role runtime)
AUDITLAB_POLICY_IMAGE=$(python3.11 offline.py image --role policy)
mkdir open-results
```

```sh
docker --host unix:///var/run/docker.sock run -i --rm --pull never --network none --read-only --cap-drop ALL \
  --security-opt no-new-privileges --user "$(id -u):$(id -g)" \
  --group-add "$(stat -c %g /var/run/docker.sock)" --cpus 2 --memory 1536m \
  --tmpfs /tmp:rw,nosuid,size=1g \
  --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
  --mount "type=bind,src=$(pwd)/open-results,dst=/results" \
  "$AUDITLAB_RUNTIME_IMAGE" python - run --policy-image "$AUDITLAB_POLICY_IMAGE" --results /results \
  < open_sets.py > open-results/summary.jsonl
echo "run exit=$?"
```

**Сводка** на хосте. Ожидаемые ID компаний берутся из исходников комплекта; распакуйте `source.tar.gz`, если
ещё не сделали этого для evidence checker:

```sh
mkdir -p source && tar -xzf source.tar.gz -C source
python3.11 -I open_sets.py summarize open-results/summary.jsonl --datasets source/datasets/open \
  --output open-results/open-sets.json
echo "summarize exit=$?"
```

Сводка принимает запуск, только если каждая из 2 × 10 × 4 = 80 ожидаемых попыток встречается ровно один
раз. Для каждой попытки нужны: код возврата 0, статус `submitted`, целые `tp`/`fp`/`fn` и `reward`.

**Коды `summarize`:**
- **0** — всё полно.
- **1** — есть упавшая или неразобранная попытка, отсутствующий score/reward, повторная или неожиданная
  строка, пустой файл.
- **2** — недостаёт попыток.

При любом коде, кроме 0, `mean_reward` и `micro` затронутой политики равны `null` («не проверено»).
Измеренная часть показывается отдельно в `measured_only` с числом измеренных и запланированных попыток.
Это не замена полного результата.

Ожидается `status: complete`, exit 0 у обеих команд. Агентская проверка RC3 получила следующее. Это
TP / FP / FN и средняя награда; внутренняя диагностика открытых наборов, не конкурсный балл:

| Набор | random | reconcile | flag_all | oracle |
|---|---|---|---|---|
| train | 0 / 10 / 11, −1 | 11 / 0 / 0, 0,982 | 0 / 300 / 11, −1 | 11 / 0 / 0, 1 |
| dev | 0 / 10 / 11, −1 | 11 / 0 / 0, 0,982 | 0 / 300 / 11, −1 | 11 / 0 / 0, 1 |

В каждом наборе две чистые компании; reconcile не дал на них ложных обвинений. Около 1,5 минуты на 2 CPU.
Сравните со своим запуском, а не переписывайте эти числа. Подкоманда runner называется `policy`; в
docstring модуля RC3 по ошибке написано `public`.

## 6. Консоль и сценарий

Запуск демо, адрес и сценарий на 3 минуты — [DEFENSE.md](DEFENSE.md). Кратко:

```sh
AUDITLAB_RUNTIME_IMAGE=$(python3.11 offline.py image --role runtime)
mkdir -p demo-results
docker --host unix:///var/run/docker.sock run --rm --pull never --user "$(id -u):$(id -g)" --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 2 --memory 2g \
  --tmpfs /tmp:rw,nosuid,size=1g -p 127.0.0.1:8765:8765 \
  --mount "type=bind,src=$(pwd)/demo-results,dst=/results" \
  "$AUDITLAB_RUNTIME_IMAGE" python run.py demo --host 0.0.0.0 --root /results/demo
```

Откройте `http://127.0.0.1:8765/`; адрес `0.0.0.0` в браузере не открывайте. `curl -s http://127.0.0.1:8765/demo/health`
отвечает `{"ok":true,"runs":6,"static":true}` после создания шести учебных экспортов в новом каталоге.
После собственных опытов число запусков растёт. Сверка консоли с экспортами в контейнере без сети:

```sh
docker --host unix:///var/run/docker.sock run --rm --pull never --network none --user "$(id -u):$(id -g)" --read-only --cap-drop ALL \
  --security-opt no-new-privileges --cpus 2 --memory 1536m --memory-swap 1536m --pids-limit 256 \
  --tmpfs /tmp:rw,size=64m \
  --mount "type=bind,src=$(pwd)/demo-results,dst=/results" "$AUDITLAB_RUNTIME_IMAGE" \
  python -m auditlab.demo --root /results/demo crosscheck --live
```

Успех `crosscheck --live` требует непустых экспортов и реально выполненного пересчёта оценки.
Пустой набор — `not_checked`, код 2. Повреждение экспорта, недоступный эталон пересчёта или расхождение
не дают `ok: true`. Для резервного `replay --check` используйте ID из списка запусков, а не `S01`/`S02`.

## 7. Диагностика ошибок

Сообщения и их смысл соответствуют интерфейсу launcher; исторические воспроизведения RC2 находятся
в отдельном отчёте. Сохраните точный текст ошибки нового запуска.

| Симптом | Причина и что делать |
|---|---|
| `Offline launch failed: launcher requires Python 3.11`, exit 1 | Запущен другой Python. Используйте `python3.11` |
| `Offline launch failed: checksum or size mismatch: licenses.json; no images were loaded`, exit 1 | Файл комплекта повреждён или изменён. Образы не загружались, каталог результатов не создан. Распакуйте архив заново после проверки хеша (раздел 3) |
| `Offline launch failed: saved Docker catalog lost the tagged runtime identity` (или `saved OCI index …`) | Архив без тегированных записей образов, например RC1 на новом launcher. Возьмите архив `v0.1.0-rc.2` или новее |
| `Resource limits are not supported by this daemon; functional checks continue…` | cgroup v1 или демон без делегирования контроллеров. Функциональные проверки идут, итоговые коды 2. Для ресурсной приёмки нужна машина с cgroup v2 |
| `permission denied … /var/run/docker.sock` или `Cannot connect to the Docker daemon` | Демон не запущен или у пользователя нет доступа к сокету. `docker --host unix:///var/run/docker.sock info` должен работать от того же пользователя |
| Порт `8765` занят | Остановите прежний контейнер демо (`docker ps`, `docker stop <name>`) или пробросьте другой локальный порт `-p 127.0.0.1:8766:8765` |
| В консоли `"runs":0` | Каталог `demo-results` недоступен на запись пользователю контейнера. Создайте его от своего пользователя до `docker --host unix:///var/run/docker.sock run` |
| Оценка или наблюдения разошлись в `replay --check` / `crosscheck` | Дефект: сохраните вывод и каталог `demo-results` и сообщите |

## 8. Прерывание и повтор

- **Ctrl+C / SIGINT:** код **130**. **SIGTERM:** код **143**. При существующем `launch.json` статус
  `interrupted`, поле `signal` содержит номер сигнала. Дождитесь выхода launcher: он завершает очистку.
- Начиная с RC3 это относится и к сигналу **только процессу launcher** (`kill -INT <PID>` или
  `kill -TERM <PID>` из другого терминала). Контроллер, политики и verifier этого запуска получают
  случайную метку `org.auditlab.launch=<launch_id>`; launcher удаляет только эти контейнеры.
  Другие запуски и чужие контейнеры сохраняются. После старта контроллера ожидается
  `cleanup.status = passed`, `cleanup.remaining_containers = 0`.
- Проверьте отсутствие контейнеров конкретного запуска; **не** требуйте пустого общего `docker ps`:

```sh
AUDITLAB_LAUNCH_ID=$(python3.11 -c 'import json,sys; print(json.load(open(sys.argv[1]))["launch_id"])' "$RESULTS/launch.json")
docker --host unix:///var/run/docker.sock ps --all \
  --filter "label=org.auditlab.launch=$AUDITLAB_LAUNCH_ID" --format '{{.ID}} {{.Names}}'
```

Ожидается пустой вывод и код 0. Для проверки сигнала дождитесь начала контейнерной проверки и сохраните
PID launcher. Если прервали раньше создания контроллера, `launch_id`/`cleanup` могут отсутствовать:
это не проверка удаления работающих контейнеров. Если Docker недоступен во время очистки,
`cleanup.status = failed`; сохраните ошибку и проверьте эту метку после восстановления демона.
Не удаляйте контейнеры других пользователей и не подменяйте фактический вывод Docker.

- Каталог прерванного запуска содержит частичные результаты; не используйте его как свидетельство
  успешной проверки. Повтор той же команды создаёт **новый** каталог `results/verify-…`, прежний сохраняется.
  Образы снова проверяются и загружаются из локального архива, без registry pulls.
- SIGKILL и выключение машины не позволяют выполнить `finally`; автоматическая очистка при них не
  заявлена. После восстановления проверьте метку прерванного запуска вручную.
- После перезагрузки загруженные Docker-образы сохраняются; повторите `verify` или `image`.

## 9. Что вернуть авторам

Заполните [форму журнала](../reviews/receiver-acceptance-rc3/JOURNAL_FORM.md); короткая версия всего
порядка — [пакет получателя](../reviews/receiver-acceptance-rc3/RECEIVER_PACKAGE.md). Агентская проверка RC3 —
[отчёт](../reviews/receiver-acceptance-rc3/REPORT.md); она не заменяет ваш журнал.


Журнал по разделам 1–8: версии ОС, Python и Docker, хранилище и cgroup, SHA архива и исходников, три image ID
из `launch.json`, команды, коды возврата и длительности, сводку репетиции, вывод evidence checker и
`docker --host unix:///var/run/docker.sock events`. Пройдите чек-лист [ACCEPTANCE_CHECKLIST.md](ACCEPTANCE_CHECKLIST.md) и добавьте снимки экрана
консоли. Каждое затруднение с инструкцией записывайте дословно. Вывод формулируйте как «независимая
человеческая проверка комплекта … на машине …» без утверждений о конкурсных баллах. До такого отчёта
пункт OPS-02 / UI-01 (#15) остаётся открытым.
