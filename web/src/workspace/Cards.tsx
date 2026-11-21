import type { CompareView, FindingView, RunDetail, RunMeta } from "../api";
import { money } from "../format";
import { compareLink, runLink } from "../navigation";
import { Icon } from "./Icon";
import { auditorName, documentConfirmed, findingName, independentConfirmed, periodName, runResult, timeline, type Pair } from "./model";

function MiniCalculation({ finding, exponents }: { finding: FindingView; exponents: Record<string, number> }) {
  const calc = finding.derivation;
  if (!calc || !Number.isSafeInteger(calc.result_minor)) return null;
  const showAmount = (amount: number | undefined | null) => money(amount, calc.currency, exponents);
  if (!Number.isInteger(exponents[calc.currency])) return null;
  let rows: [string, number | undefined][];
  if (calc.type === "payment_allocation" && calc.operands.length &&
      calc.operands.every(op => op.currency === calc.currency && Number.isSafeInteger(op.value_minor))) {
    rows = [
      ["Поступило", calc.operands.filter(op => op.sign > 0).reduce((sum, op) => sum + op.value_minor!, 0)],
      ["Распределено", calc.operands.filter(op => op.sign < 0).reduce((sum, op) => sum + op.value_minor!, 0)],
    ];
  } else if (calc.type === "fx_conversion" && calc.expected_minor != null && calc.recorded_minor != null) {
    rows = [["По курсу", calc.expected_minor], ["В учёте", calc.recorded_minor]];
  } else return null;
  return <dl className="calculation">
    {rows.map(([title, amount]) => <div key={title}><dt>{title}</dt><dd>{showAmount(amount)}</dd></div>)}
    <div className="balance"><dt>{calc.type === "payment_allocation" ? "Остаток" : "Разница"}</dt>
      <dd>{showAmount(calc.result_minor)}</dd></div>
  </dl>;
}

export function ContinueReview({ run, title, resumed, detail, error }: {
  run?: RunMeta; title?: string; resumed: boolean; detail?: RunDetail; error?: unknown;
}) {
  const finding = run?.status === "completed" ? detail?.findings[0] : undefined;
  const exponents = detail?.snapshot.manifest.currency_exponents ?? {};
  const amount = finding && money(finding.finding.claimed_amount_minor, finding.finding.currency, exponents);
  const verified = finding && (documentConfirmed(finding) ? "Подтверждено по документам"
    : independentConfirmed(finding) ? "Независимо подтверждено" : null);
  return <section className="review-card" aria-busy={Boolean(run && !detail && !error)}>
    <p className="eyebrow">{resumed ? "Продолжить проверку" : "Выбранная проверка"}</p>
    {run ? <>
      <div className="company">
        <span className="company-icon"><Icon name="building"/></span>
        <div><h2>{title}</h2><p>{periodName(run)} · {auditorName(run.policy)}</p></div>
      </div>
      {finding ? <div className="financial">
        <div className="financial-result">
          {amount && Number.isSafeInteger(finding.finding.claimed_amount_minor) && Number.isInteger(exponents[finding.finding.currency])
            ? <strong>{amount}</strong> : <p className="missing-amount">Не удалось отобразить сумму</p>}
          <p className="finding-label">{findingName(finding.finding.type)}
            {detail!.findings.length > 1 && <span> · ещё {detail!.findings.length - 1}</span>}</p>
        </div>
        <MiniCalculation finding={finding} exponents={exponents}/>
      </div> : <div className="financial financial-state">
        {error ? <p>Подробности проверки недоступны</p> : detail || run.status !== "completed"
          ? <p>{runResult(run).text}</p> : <div className="skeleton" role="status" aria-label="Загружаем результат"/>}
      </div>}
      <div className="continue-footer">
        {finding && (verified ? <span className="verified"><Icon name="check"/>{verified}</span>
          : <span className="muted">{finding.derivation ? "Расчёт требует проверки" : "Расчёт по документам недоступен"}</span>)}
        <a className="text-link open-review" href={runLink(run.run_id)}>Открыть проверку<Icon name="arrow"/></a>
      </div>
    </> : <div className="card-empty">Выберите запуск, чтобы вернуться к его результатам.</div>}
  </section>;
}

export function PairPreview({ pair, names, details }: {
  pair?: Pair; names: Map<string, string>; details?: CompareView;
}) {
  const dates = pair ? timeline(pair, details) : null;
  return <section className="pair-card">
    <div className="pair-title"><Icon name="compare"/><div><h2>Сравнить сценарии</h2>
      {pair && <p>{dates ? "Граница периода" : "Сравнение выбранных запусков"}</p>}</div>
    </div>
    {pair ? <>
      <div className="pair-values">
        <div className="pair-value">{pair.directed && <span className="pair-role role-a">A</span>}
          <b>{dates?.a ?? names.get(pair.left.run_id)}</b></div>
        {dates && <div className="boundary"><i aria-hidden="true"/><span>{dates.boundary}</span></div>}
        <div className="pair-value">{pair.directed && <span className="pair-role role-b">B</span>}
          <b>{dates?.b ?? names.get(pair.right.run_id)}</b></div>
      </div>
      <div className="pair-link"><a className="text-link" href={compareLink(pair.left.run_id, pair.right.run_id)}>
        Открыть сравнение<Icon name="arrow"/></a></div>
    </> : <p className="card-empty">Пока нет связанных запусков для сравнения.</p>}
  </section>;
}
