# Первый живой прогон Gemini — 22 ноября 2026

Это небольшой открытый dev-пилот, не закрытый тест и не оценка организаторов.
Одна экономическая семья, чистый и ошибочный варианты, два агента, по одной попытке.
Модель `gemini-3.1-flash-lite`, reasoning `minimal`, 4096 output tokens на вызов,
публичные инструменты, Docker-мост, 60 секунд с reset и 100 действий.

## Фактический результат

| Агент | Компания | Результат | Действия | Время |
|---|---|---|---:|---:|
| Без памяти | Частичное распределение | Отправлен пустой ответ; пропущена ошибка | 7 | 28,457 с |
| Без памяти | Чистая | Ответ не отправлен: HTTP 429 | 15 | 46,773 с |
| С памятью | Частичное распределение | HTTP 429 до первого действия | 0 | 14,087 с |
| С памятью | Чистая | HTTP 429 до первого действия | 0 | 13,946 с |

Независимый расчёт по public/: незачтённый остаток **3 024 000 минимальных единиц KZT
= 30 240 ₸**. Ответ `findings: []` не обнаружил его; независимый пересчёт и адаптер
дали TP=0, FP=0, FN=1, return=-1. Траектория показывает фактические чтения и submit;
причину решения внутри модели мы не устанавливаем. Smoke с calculator 1+1 прошёл.

[Квитанция](evidence/measurement.json) фиксирует SHA кода, план, образ и хеш CA.
[Независимый ответ](evidence/allocation-independent.json), [пересчёт](evidence/comparison.json)
и [публичный экспорт консоли](evidence/console) сохранены. Успешные API-ответы пришли
от настоящего сервиса; test doubles не использовались.

Три незавершённые попытки сохранены. Их официальное правило обработки записано
отдельно от вывода о способности модели: HTTP 429 не доказывает отсутствие ложных
обвинений или качество памяти. По одному семейству не публикуется доверительный
интервал и не заявляется преимущество. Повторяемость пропуска, сокращение с новыми
попытками Gemini, эффект обучения и закрытый holdout остаются **не проверены**.

Предварительные 4 сбоя маршрута прокси и 4 сбоя доступа к CA-файлу не дали финансовых
ответов. Они отдельно перечислены в квитанции. Gemini 3.8 Flash не ответила в лимиты
пробных запросов; Gemini 2.5 Flash вернула 404 для нового пользователя. Эти модели
не смешиваются с измерениями Flash-Lite.

## Просмотр без ключа и повторных API-запросов

```sh
python -m pip install --require-hashes -r requirements.lock
npm --prefix web ci
npm --prefix web run build
python -m auditlab.demo --root artifacts/gemini-console serve --port 8765 \
  --research docs/research/gemini/evidence/console
```

Открыть `http://127.0.0.1:8765/#/research`. Это просмотр сохранённых измерений.
Ключ не нужен. Запись видео по этим данным ещё не выполнена.

## Повторный живой запуск

Нужен ключ в переменной окружения `GEMINI_API_KEY`; его не сохраняют в конфигурации.
`provider.free-tier.json` и `plan.json` используют нулевые цены только для заявленного
пользователем Free Tier. Платёжный аккаунт/счёт не проверялся; при иной тарифной
категории сначала задают действительные цены и бюджет. Неизвестная стоимость не
подменяется нулём. См. [тарифы Google](https://aistudio.google.com/docs/pricing#gemini-3.1-flash-lite)
и [совместимый API](https://aistudio.google.com/docs/openai).

Из корня checkout на Python 3.11.14 подготовить ту же пару:

```sh
python - <<'PY'
from auditlab.generator import generate_episode
for name, count in [('allocation', 1), ('clean', 0)]:
    generate_episode(922101, {'config_version':'1.1', 'base_currency':'KZT',
        'currencies':['KZT'], 'allocation_mode':'partial',
        'issue_counts':{'payment_allocation':count}}, 'artifacts/gemini-episodes/'+name)
PY
python -m pip download --require-hashes --only-binary=:all: \
  --dest docker/wheels/bridge -r docker/bridge/requirements.lock
docker build --network=none -f docker/bridge/Dockerfile -t auditlab-llm-bridge:gemini .
python -m auditlab.research run --plan docs/research/gemini/plan.json \
  --out artifacts/gemini-live --confirm-paid
```

Имя исторического CLI-флага `--confirm-paid` означает явное разрешение сетевых вызовов;
оно не включает биллинг у Google. План имеет конечные лимиты. Не запускайте повторно
при исчерпанной квоте; новая попытка получает новый выходной каталог и остаётся в отчёте.
На управляемой сети нужны доверенные upstream proxy и CA согласно
[инструкции интеграции](../INTEGRATION.md). В нашем дополнительном образе CA скопирован
с `COPY --chmod=644`, заданы `SSL_CERT_FILE` и `REQUESTS_CA_BUNDLE`; проверка TLS включена.
Адрес конкретного облачного прокси и CA не включены в переносимый план.
