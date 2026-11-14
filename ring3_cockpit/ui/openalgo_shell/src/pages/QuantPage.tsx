/**
 * Ring 2 quant lab: the 490-factor universe scan, loader/engine activation
 * readiness, and the grounded Black-Scholes pricer.
 *
 * The ranked table is the scanner's own output (per-row SHA-256 packet), the
 * readiness panel proves the 47 loaders and 17 regional engines import, and the
 * pricer seals every input and Greek into a grounded receipt before it renders.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import type { ActivationEntryInfo, InteractiveQuantScan, JsonRecord, OptionPriceReceipt } from "@/lib/types";
import { int, latency, num, record, text, titleize } from "@/lib/format";
import { useAction, useResource } from "@/lib/use-api";
import {
  CopyHash,
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  InlineBar,
  LoadingBlock,
  MetricTile,
  SectionHeader,
  StatusPill,
  TabularValue,
  passTone,
  type ValueKind,
} from "@/design-system/primitives";

function moduleLabel(moduleName: string): string {
  const value = moduleName.trim();
  if (!value) return "—";
  const parts = value.split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : value;
}

function scalarText(value: unknown): string {
  if (typeof value === "number") return num(value, 2);
  if (typeof value === "boolean") return value ? "YES" : "NO";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) {
    return value.filter((item) => typeof item === "string" || typeof item === "number").join(" · ");
  }
  return "—";
}

function asPercent(value: unknown): unknown {
  return typeof value === "number" && Number.isFinite(value) ? value * 100 : value;
}

function positiveNumber(value: string): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function Echo({
  label,
  value,
  kind = "num",
  digits,
  unit,
}: {
  label: string;
  value: unknown;
  kind?: ValueKind;
  digits?: number;
  unit?: string;
}): ReactNode {
  return (
    <div className="min-w-0 rounded-[var(--r-xs)] border border-[var(--line)] px-2 py-1">
      <p className="kpi-label">{label}</p>
      <div className="mono tnum truncate text-[11px] text-[var(--ink-soft)]">
        <TabularValue value={value} kind={kind} digits={digits} />
        {unit ? <span className="ml-0.5 text-[var(--dim)]">{unit}</span> : null}
      </div>
    </div>
  );
}

function ActivationGrid({
  title,
  kind,
  entries,
}: {
  title: string;
  kind: "loader" | "engine";
  entries: ActivationEntryInfo[];
}): ReactNode {
  return (
    <div className="stack-6 min-w-0">
      <div className="row-between">
        <p className="eyebrow">{title}</p>
        <span className="mono tnum text-[9.5px] text-[var(--dim)]">{int(entries.length)} modules</span>
      </div>
      {entries.length === 0 ? (
        <p className="text-[10.5px] text-[var(--dim)]">No {title.toLowerCase()} registered in this workspace.</p>
      ) : (
        <ul className="grid list-none grid-cols-1 gap-1 p-0 sm:grid-cols-2 xl:grid-cols-3">
          {entries.map((entry) => (
            <li
              key={entry.module}
              className="flex min-w-0 items-center gap-1.5 rounded-[var(--r-xs)] border border-[var(--line)] bg-[rgba(255,255,255,0.012)] px-1.5 py-1"
              aria-label={`${kind} module ${entry.module} ${entry.ok ? "importable" : "failed"}`}
              title={entry.ok ? entry.module : `${entry.module} — ${entry.error || "import failed"}`}
            >
              <span
                className="h-1.5 w-1.5 flex-none rounded-full"
                style={{ background: entry.ok ? "var(--pos)" : "var(--neg)" }}
                aria-hidden="true"
              />
              <span className="mono truncate text-[10px] text-[var(--ink-soft)]">{moduleLabel(entry.module)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DonorCard({ valuation }: { valuation: JsonRecord }): ReactNode {
  const donors = Object.entries(valuation)
    .map(([label, value]) => ({ label, info: record(value) }))
    .filter((donor) => typeof donor.info.importable === "boolean");
  const active = typeof valuation.active === "number" ? valuation.active : null;
  const total = typeof valuation.total === "number" ? valuation.total : null;
  const percent = typeof valuation.percent === "number" ? valuation.percent : null;
  const failures = donors.filter((donor) => donor.info.importable === false);

  if (Object.keys(valuation).length === 0) {
    return (
      <GlassPanel eyebrow="Valuation donor lab" title="DCF · comps · 3-statement">
        <EmptyCleanSlate
          title="Valuation lab telemetry unavailable"
          body="The DCF, comps and three-statement donors report through Ring 2 only. Nothing here can price or sign an order."
        />
      </GlassPanel>
    );
  }

  if (donors.length === 0) {
    return (
      <GlassPanel eyebrow="Valuation donor lab" title="DCF · comps · 3-statement">
        <div className="grid grid-cols-2 gap-2">
          {Object.entries(valuation).map(([key, value]) => (
            <div key={key} className="tile" title={`${titleize(key)}: ${scalarText(value)}`}>
              <p className="kpi-label">{titleize(key)}</p>
              <p className="mono tnum truncate text-[11px] text-[var(--ink-soft)]">{scalarText(value)}</p>
            </div>
          ))}
        </div>
      </GlassPanel>
    );
  }

  return (
    <GlassPanel
      eyebrow="Valuation donor lab"
      title="DCF · comps · 3-statement"
      actions={
        active !== null && total !== null ? (
          <StatusPill
            value={`${int(active)} / ${int(total)} importable`}
            tone={active === total ? "ok" : "warn"}
          />
        ) : undefined
      }
    >
      <div className="grid grid-cols-2 gap-2">
        <MetricTile label="Active modules" value={`${int(active)} / ${int(total)}`} hint="ValuationLab probe" />
        <MetricTile
          label="Coverage"
          value={<TabularValue value={percent} kind="pct" />}
          hint="importable share of the donor set"
          tone={(percent ?? 0) >= 100 ? "pos" : "warn"}
        />
      </div>
      <div className="mt-2 grid gap-1.5 sm:grid-cols-2">
        {donors.map((donor) => (
          <div
            key={donor.label}
            className="flex min-w-0 items-center gap-1.5 rounded-[var(--r-xs)] border border-[var(--line)] px-2 py-1"
            title={text(donor.info.reason, text(donor.info.module, titleize(donor.label)))}
          >
            <span
              className="h-1.5 w-1.5 flex-none rounded-full"
              style={{ background: donor.info.importable ? "var(--pos)" : "var(--neg)" }}
              aria-hidden="true"
            />
            <span className="mono truncate text-[10px] text-[var(--ink-soft)]">{titleize(donor.label)}</span>
          </div>
        ))}
      </div>
      {failures.length > 0 ? (
        <div className="mt-2 rounded-[var(--r-sm)] border border-[rgba(251,113,133,0.35)] bg-[var(--neg-soft)] p-2">
          <p className="eyebrow text-[var(--neg)]">Donor import failures</p>
          <ul className="mt-1 flex list-none flex-col gap-1 p-0">
            {failures.slice(0, 6).map((donor) => (
              <li key={donor.label} className="mono text-[10px] break-words text-[var(--ink-soft)]">
                {titleize(donor.label)} · {text(donor.info.reason, "import error")}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </GlassPanel>
  );
}

function TailHedgerCard({ hedger }: { hedger: JsonRecord }): ReactNode {
  const thresholds = record(hedger.thresholds);
  const lastStatus = record(hedger.last_status);
  const lastPlan = record(hedger.last_plan);
  const hasPlan = Object.keys(lastPlan).length > 0;
  const hasMonitor = Object.keys(lastStatus).length > 0;
  const triggered = lastStatus.triggered === true;

  if (Object.keys(hedger).length === 0) {
    return (
      <GlassPanel eyebrow="Ring 2 · tail risk" title="Tactical tail hedger">
        <EmptyCleanSlate
          title="Tail hedger telemetry unavailable"
          body="The hedger reports through Ring 2 only. It never reads broker keys and never signs a hedge."
        />
      </GlassPanel>
    );
  }

  return (
    <GlassPanel
      eyebrow="Ring 2 · tail risk"
      title={titleize(text(hedger.kind, "tactical_tail_hedger"))}
      actions={
        <div className="flex flex-wrap items-center gap-1.5">
          <StatusPill
            value={hedger.contagion_attached === true ? "contagion linked" : "contagion cold"}
            tone={hedger.contagion_attached === true ? "ok" : "neutral"}
          />
          <StatusPill
            value={hedger.copula_attached === true ? "copula linked" : "copula cold"}
            tone={hedger.copula_attached === true ? "ok" : "neutral"}
          />
        </div>
      }
    >
      <div className="grid grid-cols-3 gap-2">
        <MetricTile label="Triggers" value={int(hedger.triggers)} hint="fragility / correlation breaches" />
        <MetricTile label="Hedges emitted" value={int(hedger.hedges_emitted)} hint="grounded put proposals" />
        <MetricTile
          label="Premium cap"
          value={<TabularValue value={thresholds.max_premium_bps} kind="bps" />}
          hint="max premium bps"
        />
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2">
        <Echo label="Fragility trigger" value={thresholds.cascade_fragility_index} digits={3} />
        <Echo label="Tail correlation trigger" value={thresholds.tail_correlation} digits={3} />
      </div>

      <div className="mt-2 rounded-[var(--r-sm)] border border-[var(--line)] p-2.5">
        <div className="row-between">
          <p className="eyebrow">Last monitor</p>
          <StatusPill
            value={hasMonitor ? (triggered ? "TRIGGERED" : "ARMED") : "NO TICK"}
            tone={hasMonitor ? (triggered ? "bad" : "accent") : "neutral"}
          />
        </div>
        {hasMonitor ? (
          <p className="mono tnum mt-1 text-[10px] text-[var(--dim)]">
            fragility {num(lastStatus.cascade_fragility_index, 3)} · tail correlation{" "}
            {num(lastStatus.tail_correlation, 3)}
          </p>
        ) : (
          <p className="mt-1 text-[10.5px] text-[var(--dim)]">No monitor tick has been recorded yet.</p>
        )}
      </div>

      {hasPlan ? (
        <div className="mt-2 rounded-[var(--r-sm)] border border-[var(--line)] p-2.5">
          <div className="row-between">
            <p className="eyebrow">Last hedge plan</p>
            <StatusPill
              value={lastPlan.grounding_verified === true ? "GROUNDED" : "UNVERIFIED"}
              tone={lastPlan.grounding_verified === true ? "ok" : "warn"}
            />
          </div>
          <div className="mt-1.5 grid grid-cols-2 gap-1.5 sm:grid-cols-3">
            <Echo label="Symbol" value={lastPlan.symbol} kind="text" />
            <Echo label="Strike" value={lastPlan.strike} kind="price" />
            <Echo label="Contracts" value={lastPlan.contracts} digits={0} />
            <Echo label="Premium" value={lastPlan.premium_bps} kind="bps" />
            <Echo label="Hedge ratio" value={lastPlan.hedge_ratio} digits={3} />
            <Echo label="Delta" value={record(lastPlan.greeks).delta} digits={4} />
          </div>
        </div>
      ) : (
        <p className="mt-2 text-[10.5px] leading-relaxed text-[var(--dim)]">
          No hedge has been emitted. The protective put is priced only when the cascade clears the trigger band.
        </p>
      )}
    </GlassPanel>
  );
}

function PricerPanel(): ReactNode {
  const priced = useAction<OptionPriceReceipt>();
  const [spotInput, setSpotInput] = useState("100");
  const [strikeInput, setStrikeInput] = useState("105");
  const [volInput, setVolInput] = useState("22");
  const [daysInput, setDaysInput] = useState("30");
  const [rateInput, setRateInput] = useState("5");
  const [kind, setKind] = useState<"CALL" | "PUT">("CALL");
  const [formError, setFormError] = useState<string | null>(null);

  const receipt = priced.receipt;
  const receiptRecord = record(receipt);
  const greeks = record(receiptRecord.greeks);
  const inputs = record(receiptRecord.inputs);
  const grounding = record(receiptRecord.grounding);
  const priceValue = greeks.price ?? receiptRecord.price;
  const verified = receipt ? Boolean(receiptRecord.verified ?? grounding.verified) : false;
  const digest = receiptRecord.sha256_digest ?? grounding.sha256_digest ?? "";

  const submit = async (): Promise<void> => {
    setFormError(null);
    const spot = positiveNumber(spotInput);
    const strike = positiveNumber(strikeInput);
    const volatility = positiveNumber(volInput);
    const days = positiveNumber(daysInput);
    const rate = Number(rateInput);
    if (spot === null || strike === null || volatility === null || days === null) {
      setFormError("Spot, strike, volatility and days to expiry must all be positive numbers.");
      return;
    }
    if (!Number.isFinite(rate) || Math.abs(rate) > 100) {
      setFormError("Risk-free rate must be a percentage between -100 and 100.");
      return;
    }
    await priced.run(() =>
      api.priceOption({
        spot,
        strike,
        volatility: volatility / 100,
        days_to_expiry: days,
        risk_free_rate: rate / 100,
        kind,
        // The gateway's request model names the field `option_type`; send both so
        // the CALL/PUT selection is honoured without changing the shared client.
        option_type: kind,
      }),
    );
  };

  return (
    <GlassPanel
      eyebrow="Grounded valuation"
      title="Grounded Black-Scholes pricer"
      actions={<StatusPill value="grounded packet" tone="accent2" />}
    >
      <form
        className="flex flex-col gap-2.5"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="grid grid-cols-2 gap-2">
          <label className="field">
            <span className="field-label">Spot</span>
            <input
              className="input tnum"
              inputMode="decimal"
              value={spotInput}
              onChange={(event) => setSpotInput(event.target.value)}
              aria-label="Option spot price"
            />
          </label>
          <label className="field">
            <span className="field-label">Strike</span>
            <input
              className="input tnum"
              inputMode="decimal"
              value={strikeInput}
              onChange={(event) => setStrikeInput(event.target.value)}
              aria-label="Option strike price"
            />
          </label>
          <label className="field">
            <span className="field-label">Volatility %</span>
            <input
              className="input tnum"
              inputMode="decimal"
              value={volInput}
              onChange={(event) => setVolInput(event.target.value)}
              aria-label="Annualised volatility in percent"
            />
          </label>
          <label className="field">
            <span className="field-label">Days to expiry</span>
            <input
              className="input tnum"
              inputMode="decimal"
              value={daysInput}
              onChange={(event) => setDaysInput(event.target.value)}
              aria-label="Days to expiry"
            />
          </label>
          <label className="field">
            <span className="field-label">Risk-free %</span>
            <input
              className="input tnum"
              inputMode="decimal"
              value={rateInput}
              onChange={(event) => setRateInput(event.target.value)}
              aria-label="Risk-free rate in percent"
            />
          </label>
          <label className="field">
            <span className="field-label">Kind</span>
            <select
              className="select"
              value={kind}
              onChange={(event) => setKind(event.target.value === "PUT" ? "PUT" : "CALL")}
              aria-label="Option kind"
            >
              <option value="CALL">CALL</option>
              <option value="PUT">PUT</option>
            </select>
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <button
            type="submit"
            className="btn btn-accent btn-sm"
            disabled={priced.busy}
            aria-label="Price the option and seal a grounded receipt"
          >
            {priced.busy ? "Pricing…" : "Price Option"}
          </button>
          <span className="mono text-[9.5px] text-[var(--dim)]">
            inputs and Greeks are re-computed by the grounding verifier
          </span>
        </div>

        {formError ? (
          <p
            role="alert"
            className="rounded-[var(--r-sm)] border border-[rgba(251,191,36,0.35)] bg-[var(--warn-soft)] px-2.5 py-1.5 text-[11px] text-[var(--warn)]"
          >
            {formError}
          </p>
        ) : null}
        {priced.error ? <ErrorBlock message={priced.error} /> : null}
      </form>

      {receipt ? (
        <div className="mt-3 border-t border-[var(--line)] pt-3">
          <div className="row-between flex-wrap gap-2">
            <div>
              <p className="kpi-label">Grounded price</p>
              <p className="mono tnum text-[26px] font-semibold tracking-tight text-[var(--accent)]">
                <TabularValue value={priceValue} kind="price" />
              </p>
            </div>
            <div className="flex flex-wrap items-center gap-1.5">
              <StatusPill
                value={verified ? "GROUNDING VERIFIED" : "UNVERIFIED"}
                tone={verified ? "ok" : "warn"}
              />
              <CopyHash value={digest} label="copy option pricing digest" />
            </div>
          </div>
          <p className="mono tnum mt-1 text-[9.5px] text-[var(--dim)]">
            {int(grounding.claims_checked)} claims checked · {int(grounding.derived_expressions)} derived expressions ·
            tolerance {num(grounding.tolerance, 4)}
          </p>

          <div className="mt-2 grid grid-cols-2 gap-1.5 sm:grid-cols-3">
            <Echo label="Spot" value={inputs.spot} kind="price" />
            <Echo label="Strike" value={inputs.strike} kind="price" />
            <Echo label="Volatility" value={asPercent(inputs.volatility)} kind="pct" />
            <Echo label="Days" value={inputs.days_to_expiry} digits={0} unit="d" />
            <Echo label="Risk-free" value={asPercent(inputs.risk_free_rate)} kind="pct" />
            <Echo label="Kind" value={text(receiptRecord.option_type, kind)} kind="text" />
          </div>

          <div className="mt-2 grid grid-cols-2 gap-2 sm:grid-cols-3">
            <MetricTile
              label="Delta"
              value={<TabularValue value={greeks.delta} digits={4} signTone />}
              hint="spot sensitivity"
            />
            <MetricTile
              label="Gamma"
              value={<TabularValue value={greeks.gamma} digits={4} signTone />}
              hint="delta convexity"
            />
            <MetricTile
              label="Vega"
              value={<TabularValue value={greeks.vega} digits={4} signTone />}
              hint="volatility sensitivity"
            />
            <MetricTile
              label="Theta"
              value={<TabularValue value={greeks.theta} digits={4} signTone />}
              hint="time decay"
            />
            <MetricTile
              label="Rho"
              value={<TabularValue value={greeks.rho} digits={4} signTone />}
              hint="rate sensitivity"
            />
          </div>
        </div>
      ) : (
        <p className="mt-3 text-[11px] text-[var(--dim)]">
          Price a contract to seal a grounded receipt. Nothing renders until the verifier has re-computed every figure.
        </p>
      )}
    </GlassPanel>
  );
}

export function QuantPage(): ReactNode {
  const res = useResource("quant", () => api.quant(), 15_000);
  const scan = useAction<InteractiveQuantScan>();

  const data = res.data;
  const universe = data?.universe_scan;
  const factors = data?.factors;
  const scanReceipt = scan.receipt;
  const scanRecord = record(scanReceipt);
  const candidates = scanReceipt?.candidates ?? universe?.candidates ?? [];
  const scoreCeiling = candidates.reduce(
    (max, candidate) => (Number.isFinite(candidate.score) ? Math.max(max, candidate.score) : max),
    0,
  );
  const barMax = scoreCeiling > 0 ? scoreCeiling : 1;
  const factorCount = scanReceipt?.factor_count ?? universe?.factor_count ?? factors?.count ?? 0;
  const elapsedMs = scanReceipt?.elapsed_ms ?? scanRecord.scan_latency_ms ?? universe?.elapsed_ms;
  const universeSize = scanReceipt?.universe_size ?? universe?.universe_size;
  const scanUnavailable = scanReceipt === null && universe?.available === false;

  const datasets = data?.datasets;
  const loaderEntries = datasets?.loaders;
  const engineEntries = datasets?.engines;
  const failureEntries = datasets?.failures;
  const loaders: ActivationEntryInfo[] = Array.isArray(loaderEntries) ? loaderEntries : [];
  const engines: ActivationEntryInfo[] = Array.isArray(engineEntries) ? engineEntries : [];
  const failures: string[] = Array.isArray(failureEntries) ? failureEntries : [];
  const loaderCount = datasets?.loader_module_count ?? loaders.length;
  const engineCount = datasets?.engine_module_count ?? engines.length;
  const importErrors = datasets?.import_errors ?? failures.length;
  const datasetsOk = datasets?.ok === true;
  const datasetError = datasets ? text(record(datasets).error, "") : "";

  const runScan = async (): Promise<void> => {
    const receipt = await scan.run(() => api.quantScan({ top_k: 50 }));
    if (receipt) res.reload();
  };

  return (
    <div className="flex flex-col gap-[14px]">
      <SectionHeader
        eyebrow="Ring 2 · quant lab"
        title="490-Factor Quant Lab"
        subtitle="Ranked factor matrix, 47 loaders × 17 regional engines readiness and the grounded Black-Scholes pricer. Research only — Ring 2 never signs."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill
              value={factors?.warm ? "kernels warm" : "cold kernels"}
              tone={factors?.warm ? "ok" : "warn"}
            />
            <StatusPill value={`${int(factors?.count ?? 0)} factors`} tone="accent" />
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={res.reload}
              aria-label="Reload quant telemetry"
            >
              Refresh
            </button>
          </div>
        }
      />

      {!data && res.loading ? <LoadingBlock rows={4} /> : null}
      {!data && res.error ? <ErrorBlock message={res.error} onRetry={res.reload} /> : null}
      {data && res.error ? <ErrorBlock message={res.error} onRetry={res.reload} /> : null}

      {data ? (
        <div className="split-2">
          <div className="flex min-w-0 flex-col gap-[14px]">
            <GlassPanel
              eyebrow="Universe scan"
              title="Ranked universe · Top 50"
              actions={
                <button
                  type="button"
                  className="btn btn-sm btn-accent"
                  onClick={() => void runScan()}
                  disabled={scan.busy}
                  aria-label="Re-scan 490 factors over the universe"
                >
                  {scan.busy ? "Scanning…" : "Re-Scan 490 Factors"}
                </button>
              }
            >
              <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-[var(--line)] pb-2">
                <span className="mono tnum text-[10px] text-[var(--ink-soft)]">{int(factorCount)} factors</span>
                <span className="text-[var(--faint)]">·</span>
                <span className="mono tnum text-[10px] text-[var(--ink-soft)]">{latency(elapsedMs)}</span>
                <span className="mono text-[9px] tracking-[0.1em] text-[var(--dim)] uppercase">scan elapsed</span>
                <span className="text-[var(--faint)]">·</span>
                <span className="mono tnum text-[10px] text-[var(--ink-soft)]">universe {int(universeSize)}</span>
                <span className="mono tnum ml-auto text-[9.5px] text-[var(--dim)]">
                  top {int(candidates.length)}
                </span>
              </div>

              {scanUnavailable ? (
                <EmptyCleanSlate
                  title="Universe scan unavailable"
                  body={text(universe?.error, "The 490-factor panel did not return a scan.")}
                />
              ) : candidates.length === 0 ? (
                <EmptyCleanSlate
                  title="No candidate survived the filter"
                  body="The top-1% dislocation cut returned an empty set on this tape. Re-scan after a fresh factor panel is generated."
                />
              ) : (
                <div className="scroll-x">
                  <table className="table table-compact" aria-label="Ranked universe candidates">
                    <thead>
                      <tr>
                        <th scope="col" className="num">
                          Rank
                        </th>
                        <th scope="col">Symbol</th>
                        <th scope="col">Score</th>
                        <th scope="col" className="num">
                          Price
                        </th>
                        <th scope="col" className="num">
                          Features
                        </th>
                        <th scope="col">Digest</th>
                      </tr>
                    </thead>
                    <tbody>
                      {candidates.map((candidate) => (
                        <tr key={`${candidate.rank}-${candidate.symbol}`}>
                          <td className="num">{int(candidate.rank)}</td>
                          <td>
                            <span className="mono text-[11.5px] text-[var(--ink)]">{candidate.symbol}</span>
                          </td>
                          <td>
                            <div className="flex items-center gap-2">
                              <div className="w-[84px]">
                                <InlineBar
                                  value={candidate.score}
                                  max={barMax}
                                  label={`${candidate.symbol} dislocation score`}
                                />
                              </div>
                              <span className="mono tnum text-[10.5px]">{num(candidate.score, 2)}</span>
                            </div>
                          </td>
                          <td className="num">
                            <TabularValue value={candidate.price} kind="price" />
                          </td>
                          <td className="num">
                            <TabularValue value={candidate.feature_count} kind="int" />
                          </td>
                          <td>
                            <CopyHash
                              value={candidate.sha256_digest}
                              label={`copy ${candidate.symbol} grounding digest`}
                            />
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {scan.error ? <div className="mt-2"><ErrorBlock message={scan.error} /></div> : null}
            </GlassPanel>

            <GlassPanel
              eyebrow="Datasets"
              title="Loader & engine readiness"
              actions={
                <StatusPill
                  value={datasetsOk ? "all importable" : "import errors"}
                  tone={passTone(datasetsOk)}
                />
              }
            >
              <div className="row-between flex-wrap gap-2">
                <p className="mono tnum text-[10px] text-[var(--ink-soft)]">
                  {int(loaderCount)} loaders · {int(engineCount)} engines · {int(importErrors)} import errors
                </p>
                <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--dim)] uppercase">
                  activation matrix
                </span>
              </div>

              <div className="mt-3 grid gap-4 xl:grid-cols-2">
                <ActivationGrid title="Data loaders" kind="loader" entries={loaders} />
                <ActivationGrid title="Regional engines" kind="engine" entries={engines} />
              </div>

              {failures.length > 0 ? (
                <div className="mt-3 rounded-[var(--r-md)] border border-[rgba(251,113,133,0.35)] bg-[var(--neg-soft)] p-2.5">
                  <p className="eyebrow text-[var(--neg)]">Import failures</p>
                  <ul className="mt-1 flex list-none flex-col gap-1 p-0">
                    {failures.map((failure) => (
                      <li key={failure} className="mono text-[10px] break-words text-[var(--ink-soft)]">
                        {failure}
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}

              {datasetError ? (
                <p className="mono mt-2 rounded-[var(--r-sm)] border border-[rgba(251,191,36,0.35)] bg-[var(--warn-soft)] px-2.5 py-1.5 text-[10px] break-words text-[var(--warn)]">
                  {datasetError}
                </p>
              ) : null}
            </GlassPanel>
          </div>

          <div className="flex min-w-0 flex-col gap-[14px]">
            <PricerPanel />
            <DonorCard valuation={record(data.valuation)} />
            <TailHedgerCard hedger={record(data.tail_hedger)} />
          </div>
        </div>
      ) : null}
    </div>
  );
}
