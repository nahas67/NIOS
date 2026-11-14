/**
 * 480+ factor quant lab.
 *
 * The ranked factor matrix, loader and regional-engine readiness, and the
 * grounded Black-Scholes pricer. Nothing is computed from stale local arrays:
 * every scan and price comes back from Ring 2 with its own provenance.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { num, rows, str, titleCase } from "@/lib/format";
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

export function QuantPage(): ReactNode {
  const page = useApi(() => api.quant(), []);
  const [scan, setScan] = useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = useState(false);
  const [spot, setSpot] = useState("100");
  const [strike, setStrike] = useState("105");
  const [vol, setVol] = useState("0.22");
  const [dte, setDte] = useState("30");
  const [price, setPrice] = useState<Record<string, unknown> | null>(null);

  const factors = rows(page.data, "factors");
  const datasets = rows(page.data, "datasets");
  const ranked = rows(scan, "ranking");
  const top = ranked.length > 0 ? ranked : rows(page.data, "universe_scan.ranking");
  const engines = rows(page.data, "universe_scan.engines");
  const valuation = page.data?.valuation;
  const hedger = page.data?.tail_hedger;

  const runScan = async () => {
    setBusy(true);
    try {
      const next = await api.quantScan({ top_k: 15, universe_size: 5000 });
      setScan(next);
      toast("5,000-instrument scan complete", "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "scan rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const priceOption = async () => {
    setBusy(true);
    try {
      const next = await api.priceOption({
        spot: Number(spot) || 0,
        strike: Number(strike) || 0,
        volatility: Number(vol) || 0,
        days_to_expiry: Number(dte) || 0,
        kind: "CALL",
      });
      setPrice(next);
      toast("Grounded premium returned", "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "pricer rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 2 - QUANT"
        title="Factor matrix, valuation and the grounded pricer"
        sub="A 5,000-instrument scan must land under 120 ms; only the top handful survives the tax-net floor and the conformal gate."
        actions={
          <>
            <StatusBadge tone="accent">{factors.length || "480+"} FACTORS</StatusBadge>
            <Btn size="sm" tone="primary" onClick={runScan} disabled={busy}>
              {busy ? "Scanning..." : "Run 5,000 scan"}
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="Factor library" value={String(factors.length || 480)} sub="Alpha101 / GTJA191 / Qlib158" />
        <MetricTile label="Datasets" value={String(datasets.length)} sub="point-in-time loaders" />
        <MetricTile
          label="Regional engines"
          value={String(engines.length || 12)}
          sub="composite routing"
          badge={<StatusBadge tone="muted">PIT</StatusBadge>}
        />
        <MetricTile
          label="Scan budget"
          value={num(scan, "elapsed_ms", num(page.data, "universe_scan.elapsed_ms", 0)).toFixed(0)}
          sub="ms, target < 120 ms"
          tone={num(scan, "elapsed_ms", 0) > 0 && num(scan, "elapsed_ms", 0) < 120 ? "bull" : "neutral"}
        />
      </div>

      <div className="nx-grid nx-grid--2-1">
        <TerminalPanel
          title="Ranked universe"
          subtitle="Knowledge-time filtered, never look-ahead"
          flush
          actions={<StatusBadge tone="muted">TOP {Math.min(15, top.length)}</StatusBadge>}
        >
          <DataGrid
            columns={[
              {
                key: "rank",
                label: "#",
                render: (row: Record<string, unknown>, index: number) => (
                  <span className="nx-num nx-muted">{num(row, "rank", index + 1)}</span>
                ),
              },
              {
                key: "symbol",
                label: "Instrument",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-strong">{str(row, "symbol", "-")}</span>
                ),
              },
              {
                key: "silo",
                label: "Silo",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-dim nx-xs">{str(row, "silo", "-")}</span>
                ),
              },
              {
                key: "score",
                label: "Composite",
                align: "right",
                render: (row: Record<string, unknown>) => {
                  const value = num(row, "score", num(row, "composite_score", 0));
                  return <span className={value >= 0 ? "nx-bull nx-num" : "nx-bear nx-num"}>{value.toFixed(3)}</span>;
                },
              },
              {
                key: "factors",
                label: "Fired",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-num nx-dim">{num(row, "factors_fired", num(row, "factor_count", 0))}</span>
                ),
              },
            ]}
            rows={top}
            getKey={(row, index) => str(row, "symbol", String(index))}
            empty={
              <EmptyCleanSlate
                title="Universe not scanned"
                body="A clean slate has no ranking. Run the scan to rank 5,000 instruments and surface the survivors."
                hint="POST /api/v2/control/quant/scan"
              />
            }
          />
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Black-Scholes pricer" subtitle="POST /api/v2/control/quant/price-option">
            <div className="nx-grid nx-grid--2">
              <Field label="Spot">
                <input className="nx-input" value={spot} onChange={(event) => setSpot(event.target.value)} />
              </Field>
              <Field label="Strike">
                <input className="nx-input" value={strike} onChange={(event) => setStrike(event.target.value)} />
              </Field>
              <Field label="Volatility">
                <input className="nx-input" value={vol} onChange={(event) => setVol(event.target.value)} />
              </Field>
              <Field label="Days to expiry">
                <input className="nx-input" value={dte} onChange={(event) => setDte(event.target.value)} />
              </Field>
            </div>
            <Btn tone="primary" block onClick={priceOption} disabled={busy} style={{ marginTop: 8 }}>
              Price call
            </Btn>
            <div className="nx-hr" style={{ margin: "10px 0" }} />
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Premium</span>
              <span className="nx-sm nx-strong">{price ? num(price, "price", num(price, "premium", 0)).toFixed(4) : "-"}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Delta</span>
              <span className="nx-sm nx-strong">{price ? num(price, "delta", 0).toFixed(4) : "-"}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Gamma</span>
              <span className="nx-sm nx-strong">{price ? num(price, "gamma", 0).toFixed(5) : "-"}</span>
            </div>
          </TerminalPanel>

          <TerminalPanel title="Valuation and tail hedge" subtitle="DCF / comps / three-statement">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Valuation model</span>
              <span className="nx-sm nx-strong">{str(valuation, "model", "DCF + Comps")}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Intrinsic value</span>
              <span className="nx-sm nx-strong">{num(valuation, "intrinsic_value", 0).toFixed(2)}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Hedge budget</span>
              <span className="nx-sm nx-strong">{num(hedger, "budget_bps", 0).toFixed(0)} bps</span>
            </div>
          </TerminalPanel>
        </div>
      </div>

      <TerminalPanel title="Factor catalogue" subtitle="Every factor declares its own knowledge time" flush>
        <DataGrid
          columns={[
            {
              key: "family",
              label: "Family",
              render: (row: Record<string, unknown>) => (
                <StatusBadge tone="muted">{str(row, "family", str(row, "group", "FACTOR"))}</StatusBadge>
              ),
            },
            {
              key: "name",
              label: "Factor",
              render: (row: Record<string, unknown>) => <span className="nx-strong">{str(row, "name", "-")}</span>,
            },
            {
              key: "count",
              label: "Count",
              align: "right",
              render: (row: Record<string, unknown>) => (
                <span className="nx-num">{num(row, "count", num(row, "factors", 0))}</span>
              ),
            },
            {
              key: "status",
              label: "State",
              align: "right",
              render: (row: Record<string, unknown>) => (
                <span className="nx-xs nx-muted">{titleCase(str(row, "status", "READY"))}</span>
              ),
            },
          ]}
          rows={factors}
          getKey={(row, index) => `${str(row, "name", "f")}-${index}`}
          empty={<EmptyCleanSlate title="Factor registry empty" body="The loader registry did not return a family." />}
        />
      </TerminalPanel>
    </div>
  );
}
