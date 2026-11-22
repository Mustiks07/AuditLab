# Предложение: включить полную приёмку Genesis в CI

Изменение общего CI согласует интегратор; в этой ветке `.github/workflows/` не менялся.

## Что уже выполняется автоматически

- Набор `runner`: аналоги C01–C20 адаптера, canary песочницы, а после слияния этой ветки — и четыре враждебные политики в Docker (`AUDITLAB_POLICY_IMAGE` в CI уже задан).
- Набор `evaluator`: векторы награды 1–20, M01–M12 и правило выбора.
- `tests/genesis_acceptance`: только самопроверка эталонного matcher, и только с `--include-research`.

## Чего нет

Полного стенда `review/genesis_acceptance/run.py` — C01–C20 через `judge_adapter`, D01–D20 с независимым
расчётом до открытия эталона, P01–P20 через адаптер, утечки закрытого режима, срок 60 с — и внешней
проверки ID. Оба инструмента запускаются только вручную. Последний ручной отчёт через адаптер в репозитории
(`docs/reviews/genesis-checks-2026-11-20/report-adapter-f9a0ea6.json`) снят 21.11.2026 на коммите `8c8e075`, а не на RC4.

## Шаг для job `verify` (после `python run.py verify …`)

```yaml
      - name: Full local Genesis acceptance and external ID compatibility (not an official score)
        run: |
          python review/genesis_acceptance/run.py --output artifacts/genesis-acceptance
          python -m review.jury_compatibility_v3.check_ids --generate --output artifacts/jury-v3/ids
          python -m review.jury_compatibility_v3.gate \
            --genesis artifacts/genesis-acceptance/report.json \
            --ids artifacts/jury-v3/ids/id-transform.json \
            --expected-commit "$GITHUB_SHA"
```

Артефакты `artifacts/genesis-acceptance` и `artifacts/jury-v3` стоит добавить к уже загружаемым.
Для pull request ожидаемый коммит — merge-коммит проверки (`$GITHUB_SHA` события), как в существующем
`review_evidence.py`.

## Стоимость и условия

| Шаг | Время на машине проверки (2 CPU) | Нужен Docker |
|---|---:|---|
| `genesis_acceptance/run.py` (с 61-секундным C18) | 74 с | нет |
| `check_ids --generate` (24 эпизода × 7 профилей) | меньше 2 мин | нет |
| `gate` | меньше секунды | нет |

## Семантика шлюза

| Код | Значение |
|---|---|
| 0 | Официальный слой стенда: все блоки `passed`, `failed` = 0, `not_checked` = 0. Отчёты от `--expected-commit`, продуктовый код чистый. Обязательные профили ID (`reorder`, `discover-*`) прошли на всех эпизодах, повтор 100 % |
| 1 | Хотя бы одно требование провалено или не проверено |
| 2 | Отчёт отсутствует, не читается, от другого коммита или собран из грязного дерева |

Профили `literal+reorder` и `literal-opaque-truth` моделируют неопределённое поведение жюри (U1/U2 в
[ID_RULES.md](ID_RULES.md)). Шлюз их только печатает и никогда не валит сборку из-за них.

## Проверено здесь

На коде `d0efe7f`: стенд и проверка ID выполнены, `gate` вернул 0 ([gate.json](evidence/checks/gate.json)).
Тесты шлюза — `tests/jury_compatibility_v3/test_id_checker.py::Gate`.
