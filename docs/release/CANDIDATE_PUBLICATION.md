# Публикация проверенного кандидата

Workflow `Publish verified candidate` запускается вручную только с ветки `main`.
Он не собирает новую версию и не повторяет измерения: публикуются ровно артефакты
указанного успешного `Integration checks` run.

```sh
gh workflow run publish-candidate.yml --repo Mustiks07/AuditLab --ref main \
  -f tag=v0.1.0-rc.3 -f source=FULL_TESTED_SHA -f run=SUCCESSFUL_FULL_RUN_ID
```

Перед запуском исходный SHA должен быть предком `origin/main`. Успешный run должен
относиться к этому полному SHA, тому же репозиторию и `verify.yml`; обязательны
успешные jobs `verify`, `frontend`, `Build offline bundle` и обе
`Internal offline rehearsal (classic/containerd)` в одной актуальной попытке.
PR merge-ref, чужой SHA, пропущенный job, истёкший или подменённый artifact
отклоняются. Нужны ровно три именованных артефакта: offline-bundle и два
store-specific offline-evidence; остальные CI-артефакты не публикуются автоматически.

Проверка перед загрузкой закрепляет ID, SHA-256 и размер GitHub artifacts.
После скачивания эти идентичности повторно сверяются. В каждом evidence наборе
проверяются ограничения 1536 MiB, 20/80/20, все config/OCI/tag идентичности до
загрузки, полученные image IDs, текущая матрица 22 внутренних групп с явными
пробелами OPS-01/OPS-02 и 20 независимых исходных книг. Хеши receipt связаны
не только с JSON-отчётом, но и с фактическими файлами снимков в evidence.zip.

Поставка состоит из offline.zip, evidence.zip, REPORT.json и SHA256SUMS.txt.
Сначала создаётся **draft** с `target_commitish` равным проверенному SHA.
После загрузки проверяются точные имена, размеры и серверные SHA-256 всех assets
через `/releases/ID` (доступ к draft через `/releases/tags/TAG` ненадёжен).
Только затем draft становится prerelease, и проверяется опубликованный tag.
Ни существующий tag, ни release, включая draft, ни asset не заменяются.
При ошибке загрузки остаётся draft для разбора; автоматическое повторение
не перезаписывает его. Предыдущие кандидаты сохраняются.

Это публикация внутренних свидетельств. Официальный конкурсный балл,
человеческая приёмка и полный расход RAM машины не выводятся из зелёного CI.
