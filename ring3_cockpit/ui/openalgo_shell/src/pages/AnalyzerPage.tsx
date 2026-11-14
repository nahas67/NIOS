/**
 * Ring 2 shadow twin and the 30-fill graduation nursery.
 *
 * Law 4: the default answer is no trade. A strategy earns live eligibility only
 * after 30 L2 shadow fills, and only while the split-conformal Q0.98 residual
 * clears the adverse-selection cost. The ladder is rendered as evidence: a
 * gauge, the progress bar, the two residual tiles, then the paper-vs-live
 * execution diagnostic and the paper-only burst control.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import { api, postShadowBurst } from "@/lib/api";
import type { AnalyzerStrategyRow, JsonRecord } from "@/lib/types";
import { int, num, record, text, titleize } from "@/lib/format";
import { useAction, useResource } from "@/lib/use-api";
import { navigate, pagePath, useLocation } from "@/lib/router";
import {
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  InlineBar,
  LoadingBlock,
  MetricTile,
  pillTone,
  SectionHeader,
  StatusPill,
  TabularValue,
} from "@/design-system/primitives";
import { RadialGauge } from "@/charts/RadialGauge";

const SILOS = ["CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"];

function StrategyGraduationCard({ row }: { row: AnalyzerStrategyRow }): ReactNode {
  const required = row.required_fills > 0 ? row.required_fills : 30;
  const progress = Number.isFinite(row.progress_pct) ? row.progress_pct : 0;
  return (
    <GlassPanel
      eyebrow="Shadow strategy"
      title={<span className="mono">{row.strategy_id}</span>}
      actions={
        <StatusPill
          value={row.graduated ? "GRADUATED" : "IN NURSERY"}
          tone={row.graduated ? "ok" : "accent"}
          title={row.graduated ? "Live eligibility unlocked" : "Still accruing shadow fills"}
        />
      }
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start">
        <RadialGauge
          value={row.completed_fills}
          max={required}
          label={row.strategy_id}
          sublabel={`${int(row.remaining_fills)} fills to live eligibility`}
          tone={row.graduated ? "pos" : "accent"}
          size={124}
        />
        <div className="stack-6 min-w-0 flex-1">
          <div>
            <div className="row-between">
              <p className="kpi-label">Graduation progress</p>
              <p className="mono tnum text-[11px]">{num(progress, 1)}%</p>
            </div>
            <div className="mt-1">
              <InlineBar
                value={progress}
                label={`${row.strategy_id} graduation progress`}
                tone={row.graduated ? "pos" : "accent"}
              />
            </div>
            <p className="mono tnum mt-1 text-[9.5px] text-[var(--dim)]">
              {int(row.completed_fills)} / {int(required)} shadow fills
            </p>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <MetricTile
              label="Q0.98 residual"
              value={<TabularValue value={row.q98_bps} kind="bps" signTone />}
              hint="split-conformal quantile"
              tone={pillTone(row.q98_bps)}
            />
            <MetricTile
              label="Adverse selection"
              value={<TabularValue value={row.adverse_selection_bps} kind="bps" signTone />}
              hint="slippage the fills paid"
              tone={pillTone(row.adverse_selection_bps)}
            />
          </div>
        </div>
      </div>
    </GlassPanel>
  );
}

function ExecutionColumn({
  scope,
  label,
  metrics,
}: {
  scope: "paper" | "live";
  label: string;
  metrics: Record<string, number>;
}): ReactNode {
  const entries = Object.entries(metrics);
  return (
    <div className="stack-6">
      <div className="row-between">
        <p className="eyebrow">{label}</p>
        <StatusPill value={`${int(entries.length)} measures`} tone={scope === "paper" ? "accent" : "accent2"} />
      </div>
      {entries.length === 0 ? (
        <p className="text-[11px] text-[var(--dim)]">No {scope} execution diagnostic has been recorded yet.</p>
      ) : (
        <div className="grid grid-cols-2 gap-2">
          {entries.map(([key, value]) => (
            <div
              key={key}
              className="tile"
              title={`${titleize(key)} — ${scope}-workspace execution diagnostic. Fill and breaker counts are integers; latency and slippage measures are quoted in bps.`}
            >
              <p className="kpi-label">{titleize(key)}</p>
              <div className="kpi-value mono tnum">
                <TabularValue value={value} kind="int" />
              </div>
              <p className="kpi-hint">paper / live execution diagnostic</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export function AnalyzerPage(): ReactNode {
  const location = useLocation();
  const res = useResource("analyzer", () => api.analyzer(), 8000);
  const burst = useAction<JsonRecord>();
  const [silo, setSilo] = useState<string>(SILOS[0]);

  const workspace = location.workspace;
  const isLive = workspace === "live";
  const data = res.data;
  const nursery = data?.nursery;
  const strategies = nursery?.strategies ?? [];
  const graduatedCount = nursery?.graduated_count ?? 0;
  const requiredFills = nursery?.required_fills ?? 30;
  const calibrated = nursery?.calibrated === true;
  const paper = data?.paper ?? {};
  const live = data?.live ?? {};
  const source = data?.source;

  const receipt = burst.receipt ? record(burst.receipt) : null;
  const fillsLanded = receipt?.fills_gained ?? receipt?.fills ?? 0;
  const shadowFills = receipt?.shadow_fills;
  const receiptRequired = receipt?.required_fills ?? requiredFills;
  const receiptGraduated = receipt?.graduated === true;

  const runBurst = async (): Promise<void> => {
    const landed = await burst.run(() => postShadowBurst({ silo, fills: 10 }));
    if (landed) res.reload();
  };

  return (
    <div className="flex flex-col gap-[14px]">
      <SectionHeader
        eyebrow="Ring 2 · shadow twin"
        title="Shadow Twin & 30-Fill Nursery"
        subtitle="L2 shadow execution twin, adverse-selection Q0.98 comparison and the graduation ladder to live. No fill count, no promotion."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill
              value={isLive ? "live vault · read-only" : workspace === "paper" ? "paper sandbox" : "base workspace"}
              tone={isLive ? "bad" : "accent"}
            />
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={res.reload}
              aria-label="Reload analyzer telemetry"
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
        <>
          <GlassPanel
            eyebrow="sec_018 · Law 4"
            title="The 30-fill graduation law"
            actions={
              <StatusPill
                value={calibrated ? "98% conformal window" : "cold-start window"}
                tone={calibrated ? "ok" : "warn"}
                title="The split-conformal residual window needs 200 realised fills before it is calibrated"
              />
            }
            footer={
              <div className="row-between flex-wrap gap-2">
                <span className="mono text-[9.5px] text-[var(--dim)]">
                  source · {text(source?.package, "ring0_vault/silos/analyzer_shadow")}
                </span>
                <button
                  type="button"
                  className="btn btn-sm btn-ghost"
                  onClick={() => navigate(pagePath("backtest", workspace))}
                  aria-label="Open the walk-forward backtester"
                >
                  Walk-forward gate
                </button>
              </div>
            }
          >
            <div className="grid gap-4 lg:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)]">
              <div className="stack-6">
                <p className="text-[12px] leading-relaxed text-[var(--muted)]">
                  A strategy is eligible for live graduation only after{" "}
                  <span className="mono tnum text-[var(--ink)]">{int(requiredFills)}</span> L2 shadow fills. The twin
                  replays every signal, records the slippage it would have paid, and compares the{" "}
                  <span className="mono text-[var(--ink-soft)]">Q0.98</span> residual against adverse selection before
                  Ring 0 may sign anything.
                </p>
                <p className="text-[11.5px] leading-relaxed text-[var(--dim)]">
                  LLMs propose here; they never execute. A cold-start window reports an uncalibrated residual rather
                  than inventing confidence, and a graduated strategy still has to clear the 98% split-conformal gate on
                  every live proposal.
                </p>
                <div className="chip-row">
                  <StatusPill value={`${int(graduatedCount)} graduated`} tone={graduatedCount > 0 ? "ok" : "neutral"} />
                  <StatusPill value={`${int(strategies.length)} registered`} tone="neutral" />
                  <StatusPill value={calibrated ? "calibrated" : "uncalibrated"} tone={calibrated ? "ok" : "warn"} />
                </div>
              </div>
              <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-1">
                <MetricTile
                  label="Graduated"
                  value={int(graduatedCount)}
                  hint={`of ${int(strategies.length)} registered strategies`}
                  tone={graduatedCount > 0 ? "pos" : undefined}
                />
                <MetricTile
                  label="Nursery required fills"
                  value={int(requiredFills)}
                  hint="L2 shadow threshold before live"
                />
                <MetricTile
                  label="Conformal calibrated"
                  value={
                    <StatusPill
                      value={calibrated ? "YES" : "NO"}
                      tone={calibrated ? "ok" : "warn"}
                      title="Whether the 200-trade residual window has enough real observations"
                    />
                  }
                  hint="200-trade residual window"
                />
              </div>
            </div>
          </GlassPanel>

          {strategies.length === 0 ? (
            <GlassPanel eyebrow="Graduation ladder" title="Nursery strategies">
              <EmptyCleanSlate
                title="No shadow strategy is in the nursery"
                body="The split-conformal twin registers a strategy on its first shadow fill. Drive a paper tick or a shadow burst to seed the ladder."
              />
            </GlassPanel>
          ) : (
            <div className="grid-auto-300">
              {strategies.map((row) => (
                <StrategyGraduationCard key={row.strategy_id} row={row} />
              ))}
            </div>
          )}

          <GlassPanel
            eyebrow="Paper versus live"
            title="Execution diagnostics"
            actions={
              <StatusPill
                value={calibrated ? "98% coverage" : "cold start"}
                tone={calibrated ? "ok" : "warn"}
              />
            }
          >
            <div className="grid gap-4 lg:grid-cols-2">
              <ExecutionColumn scope="paper" label="Paper shadow twin" metrics={paper} />
              <ExecutionColumn scope="live" label="Live execution ladder" metrics={live} />
            </div>
            <p className="mt-3 text-[10.5px] leading-relaxed text-[var(--dim)]">
              Hover any measure for its unit note. Integer fields are event counts; latency and slippage fields are
              quoted in basis points so a paper run and a live run remain comparable.
            </p>
          </GlassPanel>

          <GlassPanel
            eyebrow="Research control"
            title="Shadow fill burst"
            actions={
              <StatusPill
                value={isLive ? "paper only" : "operator driven"}
                tone={isLive ? "warn" : "accent"}
              />
            }
          >
            {isLive ? (
              <p className="text-[12px] text-[var(--muted)]">Shadow burst is a paper-only research control.</p>
            ) : (
              <div className="flex flex-col gap-2.5">
                <div className="flex flex-wrap items-end gap-2.5">
                  <label className="field min-w-[210px]">
                    <span className="field-label">Target silo</span>
                    <select
                      className="select"
                      value={silo}
                      onChange={(event) => setSilo(event.target.value)}
                      aria-label="Shadow burst silo"
                    >
                      {SILOS.map((item) => (
                        <option key={item} value={item}>
                          {item}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="btn btn-accent btn-sm"
                    onClick={() => void runBurst()}
                    disabled={burst.busy}
                    aria-label="Run a ten-fill shadow burst"
                  >
                    {burst.busy ? "Executing…" : "+10 Shadow Fills Burst"}
                  </button>
                  <span className="mono pb-1 text-[9.5px] text-[var(--dim)]">
                    drives 10 paper ticks through the twin on the selected silo
                  </span>
                </div>

                {receipt ? (
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] px-3 py-2">
                    <span className="eyebrow text-[var(--accent)]">Burst receipt</span>
                    <span className="mono tnum text-[11px] text-[var(--ink)]">
                      {int(fillsLanded)} fills landed
                    </span>
                    <span className="text-[var(--faint)]">·</span>
                    <span className="mono tnum text-[11px] text-[var(--ink-soft)]">
                      nursery {int(shadowFills)} / {int(receiptRequired)}
                    </span>
                    <StatusPill
                      value={receiptGraduated ? "GRADUATED" : "IN NURSERY"}
                      tone={receiptGraduated ? "ok" : "accent"}
                    />
                    <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--dim)] uppercase">
                      {text(receipt.status, "BURST_COMPLETE")} · {text(receipt.silo, silo)}
                    </span>
                  </div>
                ) : null}

                {burst.error ? <ErrorBlock message={burst.error} /> : null}
              </div>
            )}
          </GlassPanel>
        </>
      ) : null}
    </div>
  );
}
