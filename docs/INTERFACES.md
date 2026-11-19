# Контракты первого среза

Авторитетные финансовые правила и закрытый режим описаны в [SPEC.md](SPEC.md). Типы Python для интеграции находятся в [contracts.py](../auditlab/contracts.py). Следующие функции — согласованные точки подключения для рабочих веток.

| Владелец | Публичная точка входа | Договор |
|---|---|---|
| GPT Pro 1 | `auditlab.generator.generate_episode(seed, config, output_dir)` | Создать неизменяемый снимок в новом каталоге с public/ и private/. Отдельные аргументы — seed и конфигурация. |
| GPT Pro 1 | `auditlab.environment.open_session(public_dir) -> PublicSession` | Метод inspect(action) обслуживает list_records, get_record, get_document, calculator. Возвращает ok и result либо ok=false и error. Чтение только публичных файлов. |
| Claude Pro 1 | `auditlab.runner.make_env(mode) -> Environment` | Runner оборачивает inspect и submit, один раз расходует шаг, прикрепляет remaining_steps, ведёт журнал, возвращает reset/step по ТЗ. Mode train или closed. |
| Claude Pro 1 | `auditlab.evaluator.score_submission(submission, truth, inspection_steps) -> Score` | Оценщик принимает JSON-объекты. Структурно ошибочная сдача фиксируется по ТЗ. truth — объект с episode_id и issues; поле допустимых наборов называется evidence_sets. |
| GPT Pro 1 / Claude Pro 1 | `auditlab.policies.make_policy(name, seed) -> Policy` / доверенный oracle | Имена публичных политик random, reconcile, flag_all. Новый экземпляр на эпизод. Oracle выполняет отдельный доверенный адаптер раннера. |
| Claude Pro 2 | `auditlab.verifier.verify_snapshot(public_dir) -> list[Finding]` | Воспроизводит результат по открытым данным; сопоставление с truth идёт вне этой функции. |
| Claude Pro 1 / Claude Pro 3 | Завершённый публичный экспорт | Каталог прогона: run.json (run_id, episode_id, policy, mode, status), public/ со снимком, trajectories.jsonl со строками формата §11, submission.json и score.json. Для демо доступны только завершённые открытые прогоны. |
| GPT Pro 2 | `python run.py verify`, `demo`, `replay` | Финальные команды собираются после подключения веток. На стартовом коммите проверка фундамента — unittest из README. |

## Публичная контрольная пара

Два небольших открытых разработческих примера находятся в fixtures/foundation/. В каждом эпизоде есть public/ с настоящими CSV/JSON/JSONL и отдельная private/ с открытым учебным эталоном. Загрузчик обычного аудитора получает только public/. Эти снимки предназначены для интеграции и не входят в 10 train и 10 dev. Условные локальные имена каталогов не передаются агенту. Индекс settlement_index перечисляет все движения по нераспределённому остатку до исключающей границы. Лишняя строка следующего месяца имеет значение как контекст. Для row_id в get_record используется локальный ключ, для цитирования — ledger/ROW. Денежные суммы в тиынах.

Синтетический платёж 50 000 ₸, первый зачёт 30 000 ₸. Второй зачёт 20 000 ₸ за минуту до границы закрывает обязательство. Зачёт через минуту после неё оставляет на отчётную дату расхождение 20 000 ₸ (2 000 000 тиын). У обоих дел одинаковые суммы и одинаковое число записей. Идентификаторы в разных эпизодах могут совпадать; поиск всегда ограничен текущим эпизодом.
