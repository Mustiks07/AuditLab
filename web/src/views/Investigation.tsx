import { useEffect, useMemo, useState } from "react";
import { api, FindingView, RunDetail, Step } from "../api";
import { DerivationView, EngineLine, Empty, ErrorBox, KeyChip, LabelBadge, Panel, StatusPill } from "../components";
import { money, reward, REASONS } from "../format";
import { RecordPane } from "./RecordPane";
import { RescoreForm } from "./RescoreForm";

type Selection = { kind: "record"; key: string } | { kind: "step"; step: number } | null;

export function Investigation({ runId, initialKey }: { runId: string; initialKey?: string }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [selection, setSelection] = useState<Selection>(initialKey ? { kind: "record", key: initialKey } : null);
  const [focus, setFocus] = useState<number | null>(0);

  useEffect(() => {
    api.run(runId).then(setDetail).catch(setError);
  }, [runId]);

  const openKey = (key: string) => {
    setSelection({ kind: "record", key });
    history.replaceState(null, "", `#/runs/${encodeURIComponent(runId)}/record/${key}`);
  };

  const focused: FindingView | null = detail && focus !== null ? detail.findings[focus] ?? null : null;
  const evidenceKeys = useMemo(() => new Set(focused?.finding.evidence_ids ?? []), [focused]);
  const highlightFields = useMemo(() => {
    const map: Record<string, string[]> = {};
    focused?.derivation?.operands.forEach((op) => (map[op.key] = [...(map[op.key] ?? []), op.field]));
    return map;
  }, [focused]);

  if (error) return <ErrorBox error={error} />;
  if (!detail) return <div className="loading">Загрузка прогона {runId}…</div>;
  const run = detail.run;
  const selectedKey = selection?.kind === "record" ? selection.key : null;
  const selectedStep = selection?.kind === "step" ? detail.steps.find((s) => s.step === selection.step) ?? null : null;
  const openedAt = (key: string) => detail.steps.find((s) => s.opened_key === key)?.step ?? null;
  const listedAt = (key: string) => detail.steps.find((s) => s.listed_keys.includes(key))?.step ?? null;

  return (
    <div className="investigation">
      <div className="run-head">
        <div className="run-title">
          <a href="#/" className="back">← Запуски</a>
          <LabelBadge run={run} />
          <h1>{run.company ?? run.episode_id}</h1>
          <StatusPill status={run.status} />
        </div>
        <div className="run-meta">
          <span className="mono">{run.run_id}</span>
          <span>эпизод <b className="mono">{run.episode_id}</b></span>
          <span>политика <b className="mono">{run.policy}</b></span>
          <span>режим <b>{run.mode === "train" ? "открытый" : run.mode}</b></span>
          <span>шагов {run.steps} · инспекций {run.inspection_steps ?? "—"}</span>
          <span>награда <b className="mono">{reward(run.reward)}</b></span>
          <span className="spacer" />
          {detail.journal.available && <a className="btn btn-small btn-ghost" href={`#/runs/${run.run_id}/replay`}>▶ Повтор опыта</a>}
          {run.parent_run_id && <a className="btn btn-small" href={`#/compare/${run.parent_run_id}/${run.run_id}`}>Сравнить с исходной {run.parent_run_id}</a>}
          {detail.children.map((c) => (
            <a key={c} className="btn btn-small" href={`#/compare/${run.run_id}/${c}`}>Сравнить с {c}</a>
          ))}
        </div>
        <EngineLine engine={run.engine} />
        {run.label === "training_sample" && (
          <div className="banner banner-sample">
            ОТКРЫТЫЕ ДАННЫЕ, ЗАПИСАННЫЙ ПРОГОН. {run.note}
            {run.source && <> Источник: <code>{run.source}</code>.</>}
          </div>
        )}
        {run.change && (
          <div className="banner banner-live">
            ЖИВОЙ ОПЫТ: факт <b className="mono">{run.change.target}.{run.change.field}</b> = <b className="mono">{run.change.value}</b> в новой компании.
            Исходная попытка <a href={`#/runs/${run.parent_run_id}`}>{run.parent_run_id}</a> сохранена без изменений.
          </div>
        )}
        {run.derived_from && (
          <div className="banner banner-copy">
            КОПИЯ СДАЧИ попытки <a href={`#/runs/${run.derived_from.run_id}`}>{run.derived_from.run_id}</a>: в выводе #{run.derived_from.finding_index}{" "}
            <span className="mono">{run.derived_from.replaced.from}</span> заменено на <span className="mono">{run.derived_from.replaced.to}</span>.
            Действия до сдачи взяты из исходной попытки.
          </div>
        )}
        {run.reason && (
          <div className="banner banner-error">
            Попытка оборвана: {run.reason}. Журнал {run.journal_available ? `доступен, ${detail.journal.lines} строк, неполный` : "недоступен"}. Итог не засчитан.
          </div>
        )}
      </div>

      <div className="three">
        <Panel title={`Действия аудитора · ${detail.steps.length}`} className="col-steps">
          <StepsList steps={detail.steps} selection={selection} evidence={evidenceKeys}
            onSelect={(s) => (s.opened_key ? openKey(s.opened_key) : setSelection({ kind: "step", step: s.step }))} />
        </Panel>

        <Panel title={selectedKey ? "Запись документа" : selectedStep ? `Шаг ${selectedStep.step}` : "Документ"} className="col-record">
          {selectedKey ? (
            <RecordPane runId={run.run_id} recordKey={selectedKey} onOpen={openKey} highlightFields={highlightFields[selectedKey] ?? []}
              openedAt={openedAt(selectedKey)} listedAt={listedAt(selectedKey)} cited={evidenceKeys.has(selectedKey)} canChange={run.status === "completed"}
              generator={run.engine?.generator} />
          ) : selectedStep ? (
            <StepView step={selectedStep} onOpen={openKey} />
          ) : (
            <Empty title="Выберите доказательство, сумму или шаг">
              Нажмите на ссылку доказательства в выводе справа — здесь откроется конкретная строка исходного файла.
              <div className="manifest mono small">
                {Object.entries(detail.snapshot.tables).map(([t, n]) => <span key={t}>{t}: {n}</span>)}
              </div>
            </Empty>
          )}
        </Panel>

        <Panel title="Проверяемый вывод" className="col-findings">
          {detail.submission === null && <Empty title="Сдачи нет">Попытка не дошла до submit; итог не засчитан.</Empty>}
          {detail.empty_submission && (
            <div className="no-findings">
              <div className="no-findings-title">Ошибок не заявлено</div>
              <div className="small">Аудитор сдал пустой список.</div>
              <ScoreBlock detail={detail} />
              <ConsoleScan detail={detail} onOpen={openKey} />
            </div>
          )}
          {detail.findings.map((f) => (
            <FindingCard key={f.index} runId={run.run_id} view={f} focused={focus === f.index} selectedKey={selectedKey}
              onFocus={() => setFocus(focus === f.index ? null : f.index)} onOpen={openKey} keys={detail.keys}
              canRescore={run.status === "completed"} exponents={detail.snapshot.manifest.currency_exponents ?? {}} />
          ))}
          {detail.findings.length > 0 && <ScoreBlock detail={detail} />}
        </Panel>
      </div>
    </div>
  );
}

function calcValue(result: unknown): string {
  // Public contract: the calculator returns a decimal string.
  if (typeof result === "string") return result;
  if (result && typeof result === "object" && "value" in result) return String((result as { value: unknown }).value);
  return String(result);
}

function StepsList({ steps, selection, evidence, onSelect }: { steps: Step[]; selection: Selection; evidence: Set<string>; onSelect: (s: Step) => void }) {
  return (
    <ol className="steps">
      {steps.map((s) => {
        const active = (selection?.kind === "step" && selection.step === s.step) || (selection?.kind === "record" && s.opened_key === selection.key);
        return (
          <li key={s.step}>
            <button type="button" className={`step ${active ? "step-active" : ""} ${s.ok ? "" : "step-error"}`} onClick={() => onSelect(s)}>
              <span className="step-n">{s.step}</span>
              <span className="step-text mono">{s.text}</span>
              {((s.opened_key && evidence.has(s.opened_key)) || s.listed_keys.some((k) => evidence.has(k))) &&
                <span className="step-tag" title="Шаг вернул запись, процитированную в выбранном выводе">док.</span>}
              {s.action.action === "calculator" && s.ok && <span className="step-tag step-tag-calc">= {calcValue(s.observation.result)}</span>}
              {s.terminated && <span className="step-tag step-tag-end">сдача</span>}
              {s.truncated && <span className="step-tag step-tag-end">усечение</span>}
              <span className="step-rest">{s.remaining_steps}</span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

export function StepView({ step, onOpen }: { step: Step; onOpen: (k: string) => void }) {
  return (
    <div className="step-view">
      <div className="kv"><span>действие</span><code>{JSON.stringify(step.action)}</code></div>
      <div className="kv"><span>результат</span><b>{step.ok ? "ok" : `ошибка ${step.error?.code}`}</b></div>
      <div className="kv"><span>осталось шагов</span><b>{step.remaining_steps}</b></div>
      {step.listed_keys.length > 0 && (
        <div className="listed">
          <div className="small muted">Страница содержит записи:</div>
          {step.listed_keys.map((k) => <KeyChip key={k} k={k} onOpen={onOpen} />)}
        </div>
      )}
      {step.opened_key && <div className="listed"><KeyChip k={step.opened_key} onOpen={onOpen} /></div>}
      <pre className="observation">{JSON.stringify(step.observation, null, 1)}</pre>
    </div>
  );
}

function FindingCard({ runId, view, focused, selectedKey, onFocus, onOpen, keys, canRescore, exponents }: {
  runId: string; view: FindingView; focused: boolean; selectedKey: string | null; onFocus: () => void;
  onOpen: (k: string) => void; keys: string[]; canRescore: boolean; exponents: Record<string, number>;
}) {
  const f = view.finding;
  const calc = view.derivation;
  const [rescoring, setRescoring] = useState(false);
  return (
    <article className={`finding ${focused ? "finding-focused" : ""}`}>
      <header className="finding-head">
        <span className="finding-n">#{view.index}</span>
        <span className="finding-type">{view.type_name}</span>
        <span className="small mono muted">{f.type}</span>
      </header>
      <div className="affected">
        {view.affected.map((a) => (
          <KeyChip key={a.id} k={a.id} onOpen={a.key ? () => onOpen(a.key!) : undefined} hint={a.key ?? "нет документа"} />
        ))}
      </div>

      <div className="line line-agent">
        <span className="line-label">Агент</span>
        <button type="button" className="amount" onClick={onFocus} title="Откуда сумма? Показать операнды">
          {money(f.claimed_amount_minor, f.currency, exponents)}
        </button>
        <span className="small muted">{focused ? "скрыть расчёт" : "откуда сумма?"}</span>
      </div>
      <div className="evidence">
        <div className="small muted">Доказательства ({f.evidence_ids.length}):</div>
        {view.evidence.map((e) => (
          <div key={e.key} className={`evidence-row ${selectedKey === e.key ? "evidence-active" : ""}`}>
            <KeyChip k={e.key} onOpen={e.exists ? onOpen : undefined} active={selectedKey === e.key} missing={!e.exists} />
            <span className="mono small">{e.exists ? `${e.file?.replace("public/", "")}:${e.line}` : "нет такой записи в снимке"}</span>
            <span className="small muted">{e.opened_at_step ? `открыт на шаге ${e.opened_at_step}` : e.listed_at_step ? `получен на странице шага ${e.listed_at_step}` : "аудитор не получал"}</span>
          </div>
        ))}
      </div>
      <p className="explanation">{f.explanation}</p>

      {focused && calc && (
        <DerivationView calc={calc} exponents={exponents} onOpen={onOpen} cited={f.evidence_ids} activeKey={selectedKey} />
      )}
      {focused && view.uncited_operands.length > 0 && (
        <div className="warn">
          Не подкреплено ссылкой в выводе: {view.uncited_operands.map((k) => <KeyChip key={k} k={k} onOpen={onOpen} missing />)}
        </div>
      )}

      <div className="verdicts">
        <div className="line">
          <span className="line-label">Оценщик</span>
          {view.verdict ? (
            <span className={view.verdict.matched ? "ok-text" : "bad-text"}>
              {view.verdict.matched ? "✓ засчитан (TP)" : `✗ не засчитан — ${REASONS[view.verdict.reason] ?? view.verdict.reason}`}
              {view.verdict.source === "aggregate" && <span className="muted small"> · выведено из итога TP/FP/FN</span>}
            </span>
          ) : <span className="muted">оценщик опубликовал только итог TP/FP/FN; вердикт по этому выводу неоднозначен</span>}
        </div>
        <div className="line">
          <span className="line-label">Консоль</span>
          {calc ? (
            <span className={view.console_agrees ? "ok-text" : "bad-text"}>
              {money(calc.result_minor === null ? null : Math.abs(calc.result_minor), calc.currency, exponents)} по открытым данным {view.console_agrees ? "— совпадает" : "— не совпадает"}
            </span>
          ) : <span className="muted">консоль пересчитывает только распределение и FX; для этого типа независимый расчёт — SQL ниже</span>}
        </div>
        <div className="line">
          <span className="line-label">SQL</span>
          {view.verifier.connected
            ? <span className={view.verifier.agrees ? "ok-text" : "bad-text"}>{view.verifier.text ?? `${money(view.verifier.amount_minor ?? null, view.verifier.currency ?? "", exponents)} (${view.verifier.engine})`}{view.verifier.currency_matches === false && " — валюта не совпадает"}</span>
            : <span className="muted">{view.verifier.text}</span>}
        </div>
        {view.verifier.explanation && <div className="small mono muted sql-arith">{view.verifier.explanation}</div>}
      </div>
      <div className="small muted">Совпадение суммы и валюты не заменяет зачёт доказательств оценщиком.</div>
      {canRescore && (
        <div className="finding-actions">
          {!rescoring ? (
            <button type="button" className="btn btn-small btn-ghost" onClick={() => setRescoring(true)}>Заменить доказательство в копии сдачи…</button>
          ) : (
            <RescoreForm runId={runId} findingIndex={view.index} evidence={f.evidence_ids} keys={keys} onCancel={() => setRescoring(false)} />
          )}
        </div>
      )}
    </article>
  );
}

function ScoreBlock({ detail }: { detail: RunDetail }) {
  const s = detail.score;
  if (!s) return null;
  return (
    <div className="score">
      <div className="score-title">Вердикт оценщика · {detail.run.engine?.evaluator === "demo-standin" ? "учебная замена" : detail.run.engine?.evaluator}</div>
      <div className="score-grid">
        <div><span>TP</span><b>{s.tp}</b></div>
        <div><span>FP</span><b>{s.fp}</b></div>
        <div><span>FN</span><b>{s.fn}</b></div>
        <div><span>F1</span><b>{s.f1.toFixed(3)}</b></div>
        <div><span>инспекций</span><b>{s.inspection_steps ?? "—"}</b></div>
        <div><span>награда</span><b>{reward(s.reward)}</b></div>
      </div>
      <div className="small muted mono">clip(2×F1 − 1 − 0.002×{s.inspection_steps}, −1, 1) = {s.reward}</div>
    </div>
  );
}

function ConsoleScan({ detail, onOpen }: { detail: RunDetail; onOpen: (k: string) => void }) {
  return (
    <div className="console-scan">
      <div className="line"><span className="line-label">Консоль</span>
        {detail.console_scan.length === 0
          ? <span className="ok-text">распределение и FX: расхождений не найдено; другие типы здесь не проверяются</span>
          : <span className="bad-text">по открытым данным есть расхождения, которых нет в сдаче:</span>}
      </div>
      {detail.console_scan.map((c) => (
        <div key={c.type + c.subject} className="small">
          <KeyChip k={c.subject} onOpen={onOpen} /> {c.type}: {money(Math.abs(c.result_minor), c.currency, detail.snapshot.manifest.currency_exponents ?? {})}
        </div>
      ))}
      <div className="line"><span className="line-label">SQL</span>
        {detail.verifier.connected
          ? <span className={detail.verifier.findings.length ? "bad-text" : "ok-text"}>
              {detail.verifier.engine}: найдено расхождений {detail.verifier.findings.length}
            </span>
          : <span className="muted">SQL-сверка не подключена в этой сборке</span>}
      </div>
      {detail.verifier.findings.map((finding, index) => (
        <div key={index} className="small">
          {finding.type}: {money(finding.claimed_amount_minor, finding.currency, detail.snapshot.manifest.currency_exponents ?? {})}
          {finding.evidence_ids.map((key) => <KeyChip key={key} k={key} onOpen={onOpen} />)}
        </div>
      ))}
    </div>
  );
}
