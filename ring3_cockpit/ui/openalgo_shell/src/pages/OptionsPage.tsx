/**
 * Options chain and multi-leg strategy lab.
 *
 * A symmetric strike ladder with Greeks on the left and a grounded payoff
 * builder on the right. Every price comes from the Ring 2 Black-Scholes module,
 * not from a browser-side approximation.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { isRecord, num, rows, str } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { toast } from "@/lib/toast";
import {
  Btn,
  DataGrid,
  EmptyCleanSlate,
  Field,
  MetricTile,
  SectionHeader,
  StatusBadge,
  TerminalPanel,
} from "@/design-system/primitives";
import { PayoffCurveChart, type PayoffPoint } from "@/charts/PayoffCurveChart";

interface StrikeRow {
  strike: number;
  call_ltp: number;
  call_delta: number;
  put_ltp: number;
  put_delta: number;
}

function strikeFrom(row: Record<string, unknown>): StrikeRow {
  return {
    strike: num(row, "strike", 0),
    call_ltp: num(row, "call_ltp", num(row, "call_mid", 0)),
    call_delta: num(row, "call_delta", 0),
    put_ltp: num(row, "put_ltp", num(row, "put_mid", 0)),
    put_delta: num(row, "put_delta", 0),
  };
}

function payoffPoints(source: unknown): PayoffPoint[] {
  const candidates = ["curve", "payoff", "points", "profile"];
  for (const key of candidates) {
    const list = rows(source, key);
    if (list.length > 0) {
      return list.map((row) => ({
        spot: num(row, "spot", num(row, "underlying", 0)),
        pnl: num(row, "pnl", num(row, "payoff", num(row, "value", 0))),
      }));
    }
  }
  return [];
}

export function OptionsPage(): ReactNode {
  const page = useApi(() => api.options(), []);
  const [strategy, setStrategy] = useState("IRON_CONDOR");
  const [symbol, setSymbol] = useState("");
  const [receipt, setReceipt] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);

  const chain = isRecord(page.data?.chain) ? (page.data?.chain as Record<string, unknown>) : {};
  const strikes = rows(chain, "strikes").map(strikeFrom);
  const underlyings = rows(page.data, "underlyings");
  const strategiesRaw = page.data?.strategies;
  const strategies = Array.isArray(strategiesRaw)
    ? strategiesRaw.map((item) =>
        typeof item === "string" ? item : str(item, "name", "STRUCTURE"),
      )
    : [];
  const activeSymbol = symbol || str(page.data, "default_symbol", str(chain, "symbol", "NIFTY"));
  const points = payoffPoints(receipt);
  const breakevensRaw = receipt?.breakevens;
  const breakevens = Array.isArray(breakevensRaw) ? breakevensRaw.map((value) => Number(value) || 0) : [];
  const maxProfit = num(receipt, "max_profit", 0);
  const maxLoss = num(receipt, "max_loss", 0);
  const pcr = num(chain, "pcr", num(chain, "put_call_ratio", 0));
  const maxPain = num(chain, "max_pain", 0);

  const build = async () => {
    setBusy(true);
    try {
      const next = await api.strategyPayoff({ symbol: activeSymbol, strategy });
      setReceipt(next);
      toast(`${strategy} payoff priced`, "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "payoff rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 2 - DERIVATIVES"
        title="Options chain and multi-leg strategy lab"
        sub="Symmetric strike ladder with Greeks beside a grounded expiry payoff. No leg is priced in the browser."
        actions={
          <>
            <StatusBadge tone="accent">{activeSymbol}</StatusBadge>
            <Btn size="sm" tone="ghost" onClick={page.reload}>
              Refresh chain
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="Strikes listed" value={String(strikes.length)} sub="symmetric ladder" />
        <MetricTile
          label="Put / call ratio"
          value={pcr ? pcr.toFixed(2) : "-"}
          sub="open interest weighted"
          tone={pcr > 1 ? "bull" : "neutral"}
        />
        <MetricTile label="Max pain" value={maxPain ? maxPain.toFixed(2) : "-"} sub="expiry pin estimate" />
        <MetricTile
          label="Structures"
          value={String(strategies.length)}
          sub="multi-leg builders available"
          badge={<StatusBadge tone="muted">RING 2</StatusBadge>}
        />
      </div>

      <div className="nx-grid nx-grid--2">
        <TerminalPanel title="Expiry payoff" subtitle="25 sampled spots, flat profit and loss zones">
          <PayoffCurveChart points={points} breakevens={breakevens} strategy={strategy} />
        </TerminalPanel>

        <TerminalPanel title="Strategy builder" subtitle="POST /api/v2/control/options/strategy-payoff">
          <div className="nx-grid nx-grid--2">
            <Field label="Underlying">
              <input
                className="nx-input"
                value={activeSymbol}
                onChange={(event) => setSymbol(event.target.value.toUpperCase())}
              />
            </Field>
            <Field label="Structure">
              <select
                className="nx-select"
                value={strategy}
                onChange={(event) => setStrategy(event.target.value)}
              >
                {(strategies.length > 0 ? strategies : ["IRON_CONDOR", "VERTICAL_SPREAD", "STRADDLE"]).map(
                  (name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ),
                )}
              </select>
            </Field>
          </div>
          <div className="nx-row" style={{ marginTop: 8 }}>
            <Btn tone="primary" onClick={build} disabled={busy}>
              {busy ? "Pricing..." : "Price structure"}
            </Btn>
            <StatusBadge tone={maxProfit > 0 ? "bull" : "muted"}>
              MAX PROFIT {maxProfit ? maxProfit.toFixed(2) : "-"}
            </StatusBadge>
            <StatusBadge tone={maxLoss < 0 ? "bear" : "muted"}>
              MAX LOSS {maxLoss ? maxLoss.toFixed(2) : "-"}
            </StatusBadge>
          </div>
          <div className="nx-hr" style={{ margin: "10px 0" }} />
          <div className="nx-list nx-scroll nx-scroll--sm">
            <div className="nx-list-item">
              <span className="nx-badge nx-badge--muted">1</span>
              <span className="nx-xs nx-dim">Legs are priced on the same latent volatility surface.</span>
            </div>
            <div className="nx-list-item">
              <span className="nx-badge nx-badge--muted">2</span>
              <span className="nx-xs nx-dim">Breakevens are solved from the expiry envelope, not eyeballed.</span>
            </div>
            <div className="nx-list-item">
              <span className="nx-badge nx-badge--muted">3</span>
              <span className="nx-xs nx-dim">Tax and fee floors are applied before the conformal gate.</span>
            </div>
          </div>
        </TerminalPanel>
      </div>

      <TerminalPanel title="Chain ladder" subtitle="Calls left of the strike, puts right" flush>
        <DataGrid<StrikeRow>
          columns={[
            { key: "cdelta", label: "Call delta", align: "right", render: (row) => (
              <span className="nx-num nx-dim">{row.call_delta.toFixed(3)}</span>
            ) },
            { key: "cltp", label: "Call LTP", align: "right", render: (row) => (
              <span className="nx-num nx-bull">{row.call_ltp.toFixed(2)}</span>
            ) },
            { key: "strike", label: "Strike", align: "right", render: (row) => (
              <span className="nx-num nx-strong">{row.strike.toFixed(2)}</span>
            ) },
            { key: "pltp", label: "Put LTP", align: "right", render: (row) => (
              <span className="nx-num nx-bear">{row.put_ltp.toFixed(2)}</span>
            ) },
            { key: "pdelta", label: "Put delta", align: "right", render: (row) => (
              <span className="nx-num nx-dim">{row.put_delta.toFixed(3)}</span>
            ) },
          ]}
          rows={strikes}
          getKey={(row) => row.strike.toFixed(3)}
          empty={
            <EmptyCleanSlate
              title="Chain not materialised"
              body="Request a chain from the Ring 2 pricer to populate the ladder. Nothing is pre-seeded on a clean slate."
              hint="POST /api/v2/control/options/chain"
            />
          }
        />
      </TerminalPanel>

      {underlyings.length > 0 ? (
        <TerminalPanel title="Underlying universe" subtitle="Silo-scoped derivative roots" flush>
          <DataGrid
            columns={[
              {
                key: "symbol",
                label: "Root",
                render: (row: Record<string, unknown>) => <span className="nx-strong">{str(row, "symbol", "-")}</span>,
              },
              {
                key: "silo",
                label: "Silo",
                render: (row: Record<string, unknown>) => <span className="nx-dim">{str(row, "silo", "-")}</span>,
              },
              {
                key: "lot",
                label: "Lot",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-num">{num(row, "lot_size", 0)}</span>
                ),
              },
            ]}
            rows={underlyings}
            empty={<EmptyCleanSlate title="No root armed" />}
          />
        </TerminalPanel>
      ) : null}
    </div>
  );
}
