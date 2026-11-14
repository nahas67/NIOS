/**
 * Options Chain & Strategy Lab (sec_018 Law 3).
 *
 * A symmetric strike ladder with calls left of the axis and puts mirrored to the
 * right, then a grounded multi-leg payoff builder. The chain and the payoff are
 * two separate ring 2 receipts, each sealed with a SHA-256 digest, so the screen
 * never mixes a re-priced curve with a stale chain.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { useAction, useResource } from "@/lib/use-api";
import { useLocation } from "@/lib/router";
import { compact, int, isFiniteNumber, num, pct, price, ratioPct } from "@/lib/format";
import type { OptionsChainReceipt, StrategyPayoffReceipt } from "@/lib/types";
import {
  CopyHash,
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  LoadingBlock,
  MetricTile,
  SectionHeader,
  StatusPill,
  TabularValue,
  type PillTone,
} from "@/design-system/primitives";
import { PayoffCurveChart } from "@/charts/PayoffCurveChart";
import { pushToast, toastError } from "@/lib/toast";

function optionalNumber(value: string): number | undefined {
  const trimmed = value.trim();
  if (trimmed === "") return undefined;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : undefined;
}

/** Implied volatility arrives as a fraction on some payloads and a percent on others. */
function ivText(value: unknown): string {
  if (!isFiniteNumber(value)) return "—";
  return Math.abs(value) <= 1 ? ratioPct(value, 1) : pct(value, 1);
}

function regimeTone(regime: string): PillTone {
  if (regime === "BALANCED") return "ok";
  if (regime === "PUT_HEAVY" || regime === "CALL_HEAVY") return "warn";
  return "neutral";
}

function ChainTable({ receipt }: { receipt: OptionsChainReceipt }): ReactNode {
  if (receipt.chain.length === 0) {
    return (
      <EmptyCleanSlate
        title="No strikes returned"
        body="The pricer returned an empty strike ladder. Reload the chain or widen the strike count."
      />
    );
  }
  return (
    <div className="max-h-[560px] overflow-auto">
      <table className="table table-compact zebra">
        <caption className="sr-only">
          Symmetric option chain: calls left of the strike, puts mirrored to the right, each side with open interest,
          last price, implied volatility and greeks
        </caption>
        <thead>
          <tr>
            <th scope="colgroup" colSpan={7} className="text-left">
              Calls
            </th>
            <th scope="col" className="num">
              Strike
            </th>
            <th scope="colgroup" colSpan={7} className="text-right">
              Puts (mirrored)
            </th>
          </tr>
          <tr>
            <th scope="col" className="num">
              OI
            </th>
            <th scope="col" className="num">
              LTP
            </th>
            <th scope="col" className="num">
              IV
            </th>
            <th scope="col" className="num">
              Delta
            </th>
            <th scope="col" className="num">
              Gamma
            </th>
            <th scope="col" className="num">
              Theta
            </th>
            <th scope="col" className="num">
              Vega
            </th>
            <th scope="col" className="num">
              strike
            </th>
            <th scope="col" className="num">
              Vega
            </th>
            <th scope="col" className="num">
              Theta
            </th>
            <th scope="col" className="num">
              Gamma
            </th>
            <th scope="col" className="num">
              Delta
            </th>
            <th scope="col" className="num">
              IV
            </th>
            <th scope="col" className="num">
              LTP
            </th>
            <th scope="col" className="num">
              OI
            </th>
          </tr>
        </thead>
        <tbody>
          {receipt.chain.map((row) => {
            const atm = row.offset === 0;
            return (
              <tr key={row.strike} className={atm ? "bg-[var(--accent-soft)]" : undefined}>
                <td className="num">
                  <TabularValue value={row.call_oi} kind="int" />
                </td>
                <td className="num">
                  <TabularValue value={row.call_ltp} kind="price" />
                </td>
                <td className="num">
                  <span className="mono tnum">{ivText(row.call_iv)}</span>
                </td>
                <td className="num">
                  <TabularValue value={row.call_delta} kind="num" digits={2} />
                </td>
                <td className="num">
                  <TabularValue value={row.call_gamma} kind="num" digits={4} />
                </td>
                <td className="num">
                  <TabularValue value={row.call_theta} kind="num" digits={2} />
                </td>
                <td className="num">
                  <TabularValue value={row.call_vega} kind="num" digits={2} />
                </td>
                <th scope="row" className="num">
                  <span className="flex items-center justify-end gap-1.5">
                    <TabularValue value={row.strike} kind="price" />
                    {atm ? <span className="pill pill-accent">ATM</span> : null}
                  </span>
                </th>
                <td className="num">
                  <TabularValue value={row.put_vega} kind="num" digits={2} />
                </td>
                <td className="num">
                  <TabularValue value={row.put_theta} kind="num" digits={2} />
                </td>
                <td className="num">
                  <TabularValue value={row.put_gamma} kind="num" digits={4} />
                </td>
                <td className="num">
                  <TabularValue value={row.put_delta} kind="num" digits={2} />
                </td>
                <td className="num">
                  <span className="mono tnum">{ivText(row.put_iv)}</span>
                </td>
                <td className="num">
                  <TabularValue value={row.put_ltp} kind="price" />
                </td>
                <td className="num">
                  <TabularValue value={row.put_oi} kind="int" />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function PayoffPanel({ receipt }: { receipt: StrategyPayoffReceipt }): ReactNode {
  const verified = receipt.grounding?.verified === true;
  return (
    <GlassPanel
      title="Expiry payoff"
      eyebrow={`${receipt.strategy} · ${int(receipt.curve_points)} points`}
      actions={<StatusPill value={verified ? "GROUNDED" : "UNVERIFIED"} tone={verified ? "ok" : "warn"} />}
    >
      <div className="flex flex-col gap-3">
        <PayoffCurveChart
          points={receipt.payoff_curve}
          breakevens={receipt.breakeven_points}
          maxProfit={receipt.max_profit}
          maxLoss={receipt.max_loss}
          spot={receipt.underlying.spot}
          unboundedUpside={receipt.unbounded_upside}
          unboundedDownside={receipt.unbounded_downside}
          height={240}
        />

        <div className="grid-auto-180">
          <MetricTile
            label="Net premium / unit"
            value={<TabularValue value={receipt.net_premium_per_unit} kind="price" />}
            hint={receipt.net_position || "net position"}
          />
          <MetricTile
            label="Debit / credit"
            value={<TabularValue value={receipt.net_debit_credit} kind="usd" signTone />}
            hint={`${int(receipt.lot_size)} unit lot`}
          />
          <MetricTile
            label="Breakevens"
            value={
              receipt.breakeven_points.length === 0
                ? "—"
                : receipt.breakeven_points.map((point) => price(point)).join(" · ")
            }
            hint="expiry spot"
          />
          <MetricTile
            label="Max profit"
            value={<TabularValue value={receipt.max_profit} kind="usd" />}
            hint={receipt.unbounded_upside ? "unbounded upside" : "capped at expiry"}
            tone="pos"
          />
          <MetricTile
            label="Max loss"
            value={<TabularValue value={receipt.max_loss} kind="usd" />}
            hint={receipt.unbounded_downside ? "unbounded downside" : "capped at expiry"}
            tone="neg"
          />
        </div>

        <div className="max-h-[240px] overflow-auto">
          <table className="table table-compact">
            <caption className="sr-only">Strategy legs with strike, premium, implied volatility and delta</caption>
            <thead>
              <tr>
                <th scope="col">Leg</th>
                <th scope="col">Kind</th>
                <th scope="col">Side</th>
                <th scope="col" className="num">
                  Strike
                </th>
                <th scope="col" className="num">
                  Premium
                </th>
                <th scope="col" className="num">
                  IV
                </th>
                <th scope="col" className="num">
                  Delta
                </th>
                <th scope="col" className="num">
                  Lot
                </th>
              </tr>
            </thead>
            <tbody>
              {receipt.legs.map((leg, index) => (
                <tr key={`${leg.leg}-${index}`}>
                  <td className="mono">{leg.leg}</td>
                  <td>{leg.kind}</td>
                  <td className={leg.direction > 0 ? "pos" : "neg"}>{leg.direction > 0 ? "LONG" : "SHORT"}</td>
                  <td className="num">
                    <TabularValue value={leg.strike} kind="price" />
                  </td>
                  <td className="num">
                    <TabularValue value={leg.premium} kind="price" />
                  </td>
                  <td className="num">
                    <span className="mono tnum">{ivText(leg.iv)}</span>
                  </td>
                  <td className="num">
                    <TabularValue value={leg.delta} kind="num" digits={2} />
                  </td>
                  <td className="num">
                    <TabularValue value={leg.lot_size} kind="int" />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="row-between flex-wrap gap-2 border-t border-[var(--line)] pt-2">
          <div className="flex flex-wrap items-center gap-2">
            <CopyHash value={receipt.grounding?.sha256_digest} />
            <StatusPill value={verified ? "VERIFIED" : "UNVERIFIED"} tone={verified ? "ok" : "warn"} />
            {typeof receipt.grounding?.claims_checked === "number" ? (
              <span className="mono text-[9.5px] text-[var(--dim)]">
                {int(receipt.grounding.claims_checked)} claims checked
              </span>
            ) : null}
          </div>
          <span className="mono text-[9.5px] text-[var(--dim)]">
            spot {price(receipt.underlying.spot)} · vol {ivText(receipt.underlying.volatility)} · dte{" "}
            {int(receipt.underlying.days_to_expiry)}
          </span>
        </div>
      </div>
    </GlassPanel>
  );
}

export function OptionsPage(): ReactNode {
  const location = useLocation();
  const page = useResource("options-page", () => api.options(), 0);
  const chainAction = useAction<OptionsChainReceipt>();
  const payoffAction = useAction<StrategyPayoffReceipt>();

  const [chain, setChain] = useState<OptionsChainReceipt | null>(null);
  const [symbol, setSymbol] = useState("");
  const [strategy, setStrategy] = useState("");
  const [spot, setSpot] = useState<string | null>(null);
  const [volatility, setVolatility] = useState<string | null>(null);
  const [daysToExpiry, setDaysToExpiry] = useState<string | null>(null);

  const underlyings = page.data?.underlyings ?? [];
  const strategies = page.data?.strategies ?? [];
  const activeChain = chain ?? page.data?.chain ?? null;
  const activeSymbol = symbol || page.data?.default_symbol || activeChain?.symbol || "";
  const selectedUnderlying = underlyings.find((row) => row.symbol === activeSymbol) ?? underlyings.at(0) ?? null;
  const activeStrategy = strategy || strategies.at(0)?.id || "";
  const template = strategies.find((row) => row.id === activeStrategy) ?? null;

  const spotInput = spot ?? (selectedUnderlying ? String(selectedUnderlying.spot) : "");
  const volatilityInput = volatility ?? (selectedUnderlying ? String(selectedUnderlying.volatility) : "");
  const daysInput = daysToExpiry ?? (selectedUnderlying ? String(selectedUnderlying.days_to_expiry) : "");

  const summary = activeChain?.summary ?? null;
  const atmIv = summary ? summary.atm_iv : null;
  const pcr = summary ? summary.pcr : null;
  const regime = summary ? summary.pcr_regime : "";
  const totalCallOi = summary ? summary.total_call_oi : 0;
  const totalPutOi = summary ? summary.total_put_oi : 0;
  const strikeCount = activeChain ? activeChain.chain.length : 0;

  const chooseUnderlying = (next: string): void => {
    setSymbol(next);
    const spec = underlyings.find((row) => row.symbol === next);
    setSpot(spec ? String(spec.spot) : null);
    setVolatility(spec ? String(spec.volatility) : null);
    setDaysToExpiry(spec ? String(spec.days_to_expiry) : null);
  };

  const refreshChain = async (): Promise<void> => {
    const next = await chainAction.run(() =>
      api.optionsChain({
        symbol: activeSymbol,
        spot: optionalNumber(spotInput),
        volatility: optionalNumber(volatilityInput),
        days_to_expiry: optionalNumber(daysInput),
      }),
    );
    if (next) {
      setChain(next);
      return;
    }
    if (chainAction.error) toastError("Chain request rejected", chainAction.error);
  };

  const buildPayoff = async (): Promise<void> => {
    if (!activeStrategy) {
      pushToast("No strategy template", "The gateway returned no option templates", "warn");
      return;
    }
    const next = await payoffAction.run(() =>
      api.strategyPayoff({
        symbol: activeSymbol,
        strategy: activeStrategy,
        spot: optionalNumber(spotInput),
        volatility: optionalNumber(volatilityInput),
        days_to_expiry: optionalNumber(daysInput),
      }),
    );
    if (next) {
      pushToast("Payoff receipt sealed", `${next.strategy} · ${int(next.curve_points)} points`, "ok");
      return;
    }
    if (payoffAction.error) toastError("Payoff rejected", payoffAction.error);
  };

  const payoff = payoffAction.receipt;
  const pcrHint = "put open interest / call open interest";

  return (
    <>
      <SectionHeader
        eyebrow={`ring2 · ${location.workspace} workspace · grounded greeks`}
        title="Options Chain & Strategy Lab"
        subtitle="A symmetric strike ladder with Black-Scholes greeks, PCR and Max Pain, beside a multi-leg payoff builder sealed with a SHA-256 digest."
        actions={
          <>
            <StatusPill
              value={activeChain ? `${int(strikeCount)} strikes` : "NO CHAIN"}
              tone={activeChain ? "accent" : "neutral"}
            />
            <button
              type="button"
              className="btn btn-sm"
              onClick={page.reload}
              disabled={page.loading}
              aria-label="Reload the options landing payload"
            >
              {page.loading ? "Loading…" : "Reload"}
            </button>
          </>
        }
      />

      {page.error ? <ErrorBlock message={page.error} onRetry={page.reload} /> : null}

      {page.data === null && page.loading ? <LoadingBlock rows={4} /> : null}

      {!page.loading && !activeChain && !page.error ? (
        <EmptyCleanSlate
          title="No chain loaded"
          body="The ring 2 pricer has not returned a strike ladder for this workspace."
          action={
            <button
              type="button"
              className="btn btn-sm btn-accent"
              onClick={page.reload}
              aria-label="Request the options landing payload"
            >
              Request chain
            </button>
          }
        />
      ) : null}

      {activeChain ? (
        <div className="flex flex-col gap-[14px]">
          <GlassPanel title="Underlying & model inputs" eyebrow="ring2 · black-scholes with grounded metrics">
            <div className="flex flex-col gap-3">
              <div className="grid-auto-240">
                <label className="field">
                  <span className="field-label">Underlying</span>
                  <select
                    className="select"
                    value={activeSymbol}
                    onChange={(event) => chooseUnderlying(event.target.value)}
                    aria-label="Options underlying"
                  >
                    {underlyings.length === 0 ? (
                      <option value={activeSymbol}>{activeSymbol || "—"}</option>
                    ) : (
                      underlyings.map((row) => (
                        <option key={row.symbol} value={row.symbol}>
                          {`${row.symbol} · ${row.silo} · ${price(row.spot)}`}
                        </option>
                      ))
                    )}
                  </select>
                </label>

                <label className="field">
                  <span className="field-label">Spot</span>
                  <input
                    className="input tnum"
                    inputMode="decimal"
                    value={spotInput}
                    onChange={(event) => setSpot(event.target.value)}
                    aria-label="Underlying spot"
                  />
                </label>

                <label className="field">
                  <span className="field-label">Volatility (annualised)</span>
                  <input
                    className="input tnum"
                    inputMode="decimal"
                    value={volatilityInput}
                    onChange={(event) => setVolatility(event.target.value)}
                    aria-label="Implied volatility assumption"
                  />
                </label>

                <label className="field">
                  <span className="field-label">Days to expiry</span>
                  <input
                    className="input tnum"
                    inputMode="decimal"
                    value={daysInput}
                    onChange={(event) => setDaysToExpiry(event.target.value)}
                    aria-label="Days to expiry"
                  />
                </label>

                <div className="flex items-end">
                  <button
                    type="button"
                    className="btn btn-accent w-full"
                    onClick={() => void refreshChain()}
                    disabled={chainAction.busy}
                    aria-label="Refresh the option chain with the current model inputs"
                  >
                    {chainAction.busy ? "Pricing…" : "Refresh Chain"}
                  </button>
                </div>
              </div>

              {chainAction.error ? <ErrorBlock message={chainAction.error} /> : null}

              <div className="kpi-strip">
                <MetricTile
                  label="ATM IV"
                  value={ivText(atmIv ?? undefined)}
                  hint={`atm strike ${price(summary ? summary.atm_strike : undefined)}`}
                />
                <MetricTile
                  label="PCR"
                  value={
                    <span className="flex items-center gap-1.5">
                      {num(pcr, 2)}
                      <StatusPill value={regime || "—"} tone={regimeTone(regime)} dot={false} />
                    </span>
                  }
                  hint={pcrHint}
                />
                <MetricTile
                  label="Max pain"
                  value={price(summary ? summary.max_pain_strike : undefined)}
                  hint={`pain ${compact(summary ? summary.max_pain_value : undefined)}`}
                />
                <MetricTile
                  label="Total OI"
                  value={compact(totalCallOi + totalPutOi)}
                  hint={`call ${compact(totalCallOi)} · put ${compact(totalPutOi)}`}
                />
              </div>
            </div>
          </GlassPanel>

          <div className="split-2">
            <GlassPanel
              title={`Symmetric chain · ${int(strikeCount)} strikes`}
              eyebrow={`${activeChain.symbol} · ${activeChain.silo} · ring2`}
              footer={
                <div className="row-between flex-wrap gap-2">
                  <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--dim)] uppercase">
                    lot {int(activeChain.lot_size)} · step {price(activeChain.strike_step)} ·{" "}
                    {activeChain.currency}
                  </span>
                  <span className="flex flex-wrap items-center gap-2">
                    <CopyHash value={activeChain.grounding?.sha256_digest} />
                    <StatusPill
                      value={activeChain.grounding?.verified ? "VERIFIED" : "UNVERIFIED"}
                      tone={activeChain.grounding?.verified ? "ok" : "warn"}
                    />
                  </span>
                </div>
              }
            >
              <ChainTable receipt={activeChain} />
            </GlassPanel>

            <div className="flex flex-col gap-3">
              <GlassPanel title="Strategy builder" eyebrow="multi-leg · expiry pay-off">
                <div className="flex flex-col gap-2">
                  <label className="field">
                    <span className="field-label">Template</span>
                    <select
                      className="select"
                      value={activeStrategy}
                      onChange={(event) => setStrategy(event.target.value)}
                      aria-label="Options strategy template"
                    >
                      {strategies.length === 0 ? (
                        <option value="">No templates</option>
                      ) : (
                        strategies.map((row) => (
                          <option key={row.id} value={row.id}>
                            {row.name || row.id}
                          </option>
                        ))
                      )}
                    </select>
                  </label>

                  {template ? (
                    <p className="text-[11px] text-[var(--dim)]">
                      {template.summary} · {template.bias} bias · legs {template.legs}
                    </p>
                  ) : null}

                  <button
                    type="button"
                    className="btn btn-accent"
                    onClick={() => void buildPayoff()}
                    disabled={payoffAction.busy || activeStrategy === ""}
                    aria-label="Build the expiry payoff for the selected strategy"
                  >
                    {payoffAction.busy ? "Pricing…" : "Build payoff"}
                  </button>

                  {payoffAction.error ? <ErrorBlock message={payoffAction.error} /> : null}
                </div>
              </GlassPanel>

              {payoff ? (
                <PayoffPanel receipt={payoff} />
              ) : (
                <GlassPanel title="Expiry payoff" eyebrow="grounded">
                  <EmptyCleanSlate
                    title="No payoff yet"
                    body="Build a payoff to see the expiry curve, breakevens, the per-leg greeks and the SHA-256 receipt."
                  />
                </GlassPanel>
              )}
            </div>
          </div>
        </div>
      ) : null}
    </>
  );
}
