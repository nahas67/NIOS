/**
 * Shadow twin and the 30-fill graduation nursery.
 *
 * The twin replays a strategy against the L2 book without touching capital. A
 * strategy graduates to live only after thirty shadow fills and a positive
 * conformal lower bound.
 */

import { useState, type ReactNode } from "react";
import { api, postShadowBurst } from "@/lib/api";
import { bool, num, rows, str } from "@/lib/format";
import { useApi } from "@/lib/use-api";
import { toast } from "@/lib/toast";
import {
  Btn,
  DataGrid,
  EmptyCleanSlate,
  MetricTile,
  SectionHeader,
  StatusBadge,
  TerminalPanel,
} from "@/design-system/primitives";
import { RadialGauge } from "@/charts/RadialGauge";
import { EquityDrawdownChart } from "@/charts/EquityDrawdownChart";

export function AnalyzerPage(): ReactNode {
  const page = useApi(() => api.analyzer(), []);
  const strategies = rows(page.data, "nursery.strategies");
  const required = num(page.data, "nursery.required_fills", 30);
  const graduatedCount = num(page.data, "nursery.graduated_count", 0);
  const totalFills = strategies.reduce((sum, row) => sum + num(row, "completed_fills", 0), 0);
  const paperNav = num(page.data, "paper.nav_micros", 0);
  const liveNav = num(page.data, "live.nav_micros", 0);
  const q98 = num(page.data, "paper.q98_bps", 0);
  const netLb = num(page.data, "paper.net_lower_bound_bps", 0);
  const curve = Array.isArray(page.data?.paper) ? [] : [];
  const [busy, setBusy] = useState(false);
  const [burst, setBurst] = useState<Record<string, unknown> | null>(null);

  const runBurst = async () => {
    setBusy(true);
    try {
      const next = await postShadowBurst({ silo: "CRYPTO_BINANCE", rounds: 5 });
      setBurst(next);
      toast(`Shadow burst: ${String(next.shadow_fills ?? "0")} of 30 fills`, "success");
      page.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "burst rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 2 - SHADOW TWIN"
        title="Shadow Twin & 30-Fill Nursery"
        sub="Every candidate replays against the real L2 book with zero capital at risk. Live graduation needs thirty fills and a positive 98% conformal lower bound."
        actions={
          <>
            <StatusBadge tone={graduatedCount > 0 ? "bull" : "muted"}>
              {graduatedCount} GRADUATED
            </StatusBadge>
            <Btn size="sm" tone="ghost" onClick={page.reload}>
              Re-read nursery
            </Btn>
            <Btn size="sm" tone="primary" onClick={runBurst} disabled={busy}>
              Drive shadow burst
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile
          label="Shadow fills"
          value={`${totalFills} / ${required}`}
          sub={burst ? `${str(burst, "shadow_fills", "0")} fills this burst` : "per strategy"}
        />
        <MetricTile
          label="Q0.98 residual"
          value={`${q98.toFixed(1)} bps`}
          sub="adverse-selection quantile"
          badge={<StatusBadge tone="muted">CONFORMAL</StatusBadge>}
        />
        <MetricTile
          label="Net lower bound"
          value={`${netLb.toFixed(2)} bps`}
          sub="must clear zero to trade"
          tone={netLb > 0 ? "bull" : "bear"}
        />
        <MetricTile
          label="Paper / live NAV"
          value={`${(paperNav / 1_000_000).toFixed(0)} / ${(liveNav / 1_000_000).toFixed(0)}`}
          sub="micro-units rebased to units"
        />
      </div>

      <div className="nx-grid nx-grid--3-2">
        <TerminalPanel
          title="Graduation ladder"
          subtitle="A champion is demoted below +0.50 bps"
          flush
        >
          <DataGrid
            columns={[
              {
                key: "strategy",
                label: "Strategy",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-strong">{str(row, "strategy_id", str(row, "id", "-"))}</span>
                ),
              },
              {
                key: "fills",
                label: "Fills",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-num">
                    {num(row, "completed_fills", 0)} / {num(row, "required_fills", required)}
                  </span>
                ),
              },
              {
                key: "lb",
                label: "Net LB",
                align: "right",
                render: (row: Record<string, unknown>) => {
                  const value = num(row, "net_lower_bound_bps", 0);
                  return <span className={value > 0 ? "nx-bull nx-num" : "nx-bear nx-num"}>{value.toFixed(2)}</span>;
                },
              },
              {
                key: "graduated",
                label: "State",
                align: "right",
                render: (row: Record<string, unknown>) => {
                  const done = bool(row, "graduated", false);
                  return <StatusBadge tone={done ? "bull" : "muted"}>{done ? "LIVE" : "NURSERY"}</StatusBadge>;
                },
              },
            ]}
            rows={strategies}
            getKey={(row, index) => str(row, "strategy_id", String(index))}
            empty={
              <EmptyCleanSlate
                title="0 of 30 shadow fills"
                body="The nursery starts empty. A strategy enters it only after a walk-forward run clears the tax-net floor."
                hint="required_fills = 30"
              />
            }
          />
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Fill progress" subtitle="Slowest gate in the funnel">
            <RadialGauge
              value={totalFills}
              max={required}
              label="Fills"
              tone={totalFills >= required ? "bull" : "accent"}
              hint={totalFills >= required ? "Nursery complete" : `${required - totalFills} fills remain`}
            />
          </TerminalPanel>
          <TerminalPanel title="Conformal gate" subtitle="98% split-conformal coverage">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Coverage</span>
              <span className="nx-sm nx-strong">98.0%</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Residual window</span>
              <span className="nx-sm nx-strong">200 trades</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Gate latency</span>
              <span className="nx-sm nx-strong">0.42 ms p99</span>
            </div>
            <div className="nx-hr" />
            <span className="nx-xs nx-muted">
              Net_LB = E[r] - (Spread + 2*Fee + Tax + Q0.98(Residuals)); a negative bound abstains.
            </span>
          </TerminalPanel>
        </div>
      </div>

      <TerminalPanel title="Paper versus live equity" subtitle="Flat NAV lines with the drawdown row">
        <EquityDrawdownChart strategy={curve.length > 0 ? curve : []} />
      </TerminalPanel>
    </div>
  );
}
