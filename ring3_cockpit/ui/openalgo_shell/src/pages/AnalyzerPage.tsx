import { useCallback, useEffect, useMemo, useState } from "react";
import { LineChart } from "../charts";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  EmptyState,
  PageHeader,
  ProgressBar,
  Row,
  Spinner,
  Stat,
  StatGrid,
  Table,
  cx,
} from "../components/ui";
import { api, errorMessage } from "../lib/api";
import { asRecord, num, pct, safeBool, safeNumber, signed, text, timeUtc } from "../lib/format";
import { rowsOf } from "../lib/payload";
import { useTicker } from "../state/session";
import type { ApiNote, PageProps } from "./types";

const GRADUATION_TARGET = 30;

interface FillRow {
  when: string;
  symbol: string;
  side: string;
  qty: number;
  shadowPx: number;
  livePx: number;
  slipBps: number;
  netBps: number;
}

interface GateLine {
  id: string;
  label: string;
  detail: string;
  passed: boolean;
}

function whenText(value: unknown): string {
  if (typeof value === "number" && Number.isFinite(value) && value > 0) {
    const ms = value > 1e17 ? value / 1e6 : value > 1e11 ? value : value * 1000;
    const date = new Date(ms);
    return Number.isNaN(date.getTime()) ? "—" : date.toISOString().slice(11, 19);
  }
  return timeUtc(value);
}

function fillsOf(payload: unknown): FillRow[] {
  return rowsOf(payload, ["shadow_fills", "fills", "ledger", "rows", "items", "entries"])
    .map((row) => ({
      when: whenText(row.ts ?? row.when ?? row.filled_at ?? row.created_at ?? row.timestamp),
      symbol: text(row.symbol ?? row.instrument ?? row.ticker, "—"),
      side: text(row.side ?? row.direction, "—").toUpperCase(),
      qty: safeNumber(row.qty ?? row.quantity ?? row.filled_qty, 0),
      shadowPx: safeNumber(row.shadow_px ?? row.shadow_price ?? row.shadow_fill_px, 0),
      livePx: safeNumber(row.live_px ?? row.live_price ?? row.market_px, 0),
      slipBps: safeNumber(row.slip_bps ?? row.slippage_bps ?? row.slip, 0),
      netBps: safeNumber(row.net_bps ?? row.net ?? row.net_edge_bps, 0),
    }));
}
export default function AnalyzerPage({ mode }: PageProps) {
  const now = useTicker(1000);
  const [payload, setPayload] = useState<unknown>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<ApiNote | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const next = await api.get<unknown>("/cockpit/analyzer");
      setPayload(next);
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => {
      void load();
    }, 10000);
    return () => clearInterval(timer);
  }, [load]);

  const record = asRecord(payload);
  const fills = useMemo(() => fillsOf(payload), [payload]);
  const settled = safeNumber(record.settled_fills ?? record.settled ?? record.shadow_fills, fills.length);
  const coverage = safeNumber(record.coverage_pct ?? record.coverage, Number.NaN);
  const meanNetBackend = safeNumber(record.mean_net_bps ?? record.mean_net, Number.NaN);
  const slippageBackend = safeNumber(record.slippage_bps ?? record.mean_slippage_bps, Number.NaN);
  const meanNet =
    Number.isFinite(meanNetBackend) || fills.length === 0
      ? meanNetBackend
      : fills.reduce((total, fill) => total + fill.netBps, 0) / fills.length;
  const slippage =
    Number.isFinite(slippageBackend) || fills.length === 0
      ? slippageBackend
      : fills.reduce((total, fill) => total + fill.slipBps, 0) / fills.length;
  const dsr = safeNumber(record.dsr ?? record.deflated_sharpe ?? record.deflated_sharpe_ratio, Number.NaN);
  const leakageFree = safeBool(record.leakage_free ?? record.leakage_free_kfold ?? record.purged_kfold_clean, false);
  const expectancyPositive = Number.isFinite(meanNet) && meanNet > 0;

  const gates: GateLine[] = [
    {
      id: "SETTLED",
      label: `${GRADUATION_TARGET} settled shadow fills`,
      detail: `${settled} / ${GRADUATION_TARGET}`,
      passed: settled >= GRADUATION_TARGET,
    },
    {
      id: "EXPECTANCY",
      label: "Positive net expectancy",
      detail: Number.isFinite(meanNet) ? `${signed(meanNet, 2)} bps mean` : "not reported",
      passed: expectancyPositive,
    },
    {
      id: "DSR",
      label: "Deflated Sharpe ≥ 1.50",
      detail: Number.isFinite(dsr) ? num(dsr, 2) : "not reported",
      passed: Number.isFinite(dsr) && dsr >= 1.5,
    },
    {
      id: "KFOLD",
      label: "Leakage-free purged K-fold",
      detail: leakageFree ? "verified" : "unverified",
      passed: leakageFree,
    },
  ];
  const passedGates = gates.filter((gate) => gate.passed).length;

  const chronological = useMemo(() => [...fills].reverse(), [fills]);
  const cumulative = useMemo(() => {
    let running = 0;
    return chronological.map((fill) => {
      running += fill.netBps;
      return Number(running.toFixed(2));
    });
  }, [chronological]);

  async function burst() {
    if (mode === "LIVE" || busy) return;
    setBusy(true);
    try {
      const result = await api.post<unknown>("/control/analyzer/shadow-burst", { count: 10 });
      const body = asRecord(result);
      const added = safeNumber(body.count ?? body.added ?? body.fills_added, 10);
      const status = text(body.status ?? body.result, "accepted").toUpperCase();
      setNote({ tone: "bull", message: `Shadow burst ${status} · +${num(added, 0)} fills queued` });
      await load();
    } catch (err) {
      setNote({ tone: "bear", message: errorMessage(err) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="ANALYZER"
        subtitle="Shadow execution twin · graduation nursery · 30-trade L2 ladder"
        badges={
          <div className="flex items-center gap-1">
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone="muted" mono>
              {`${passedGates}/4 GATES`}
            </Badge>
          </div>
        }
        actions={
          <div className="flex items-center gap-1.5">
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3 tabular-nums">
              {new Date(now).toISOString().slice(11, 19)} UTC
            </span>
            <Button icon="refresh" variant="subtle" size="sm" onClick={() => void load()}>
              Refresh
            </Button>
            <Button
              variant="primary"
              size="sm"
              icon="zap"
              disabled={mode === "LIVE" || busy}
              title={mode === "LIVE" ? "Shadow burst is PAPER-only" : "Queue 10 synthetic shadow fills"}
              onClick={() => void burst()}
            >
              +10 Shadow Fills Burst
            </Button>
          </div>
        }
      />
      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}
      {mode === "LIVE" ? (
        <Banner tone="neutral" icon="lock">
          Shadow burst is PAPER-only. The LIVE workspace never injects synthetic fills into the graduation ladder.
        </Banner>
      ) : null}

      <StatGrid columns={4}>
        <Stat label="Shadow Fills" value={num(settled, 0)} hint="settled L2 observations" tone="accent" />
        <Stat
          label="Coverage"
          value={Number.isFinite(coverage) ? pct(coverage, 1) : "—"}
          hint="conformal coverage of the twin"
          tone={Number.isFinite(coverage) && coverage >= 98 ? "bull" : "neutral"}
        />
        <Stat
          label="Mean Net"
          value={Number.isFinite(meanNet) ? `${signed(meanNet, 2)} bps` : "—"}
          hint="after statutory cost floor"
          tone={expectancyPositive ? "bull" : Number.isFinite(meanNet) ? "bear" : "muted"}
        />
        <Stat
          label="Slippage"
          value={Number.isFinite(slippage) ? `${num(slippage, 2)} bps` : "—"}
          hint="shadow vs live mark"
          tone={Number.isFinite(slippage) && slippage > 3 ? "bear" : "neutral"}
        />
      </StatGrid>

      <div className="grid min-w-0 grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)]">
        <Card
          title="Graduation Counter"
          subtitle="30 settled L2 shadow fills before live graduation"
          actions={<Badge tone={settled >= GRADUATION_TARGET ? "bull" : "warn"}>{settled >= GRADUATION_TARGET ? "ELIGIBLE" : "NURSERY"}</Badge>}
        >
          <div className="flex items-baseline gap-2">
            <span className={cx("text-[34px] font-semibold leading-none tabular-nums", settled >= GRADUATION_TARGET ? "text-bull" : "text-ink")}>
              {num(Math.min(settled, GRADUATION_TARGET), 0)}
            </span>
            <span className="text-[16px] text-ink-3 tabular-nums">/ {GRADUATION_TARGET}</span>
            <span className="ml-auto text-[10px] uppercase tracking-[0.1em] text-ink-3">
              {num((Math.min(settled, GRADUATION_TARGET) / GRADUATION_TARGET) * 100, 0)}% complete
            </span>
          </div>
          <div className="mt-2">
            <ProgressBar
              value={Math.min(settled, GRADUATION_TARGET)}
              max={GRADUATION_TARGET}
              height={6}
              tone={settled >= GRADUATION_TARGET ? "bull" : "accent"}
              label={`${num(settled, 0)} settled · ${num(Math.max(0, GRADUATION_TARGET - settled), 0)} remaining`}
            />
          </div>
          <div className="mt-3 flex flex-col gap-1">
            {gates.map((gate) => (
              <div key={gate.id} className="flex items-center gap-2 border-b border-hairline/60 pb-1 last:border-b-0">
                <Badge tone={gate.passed ? "bull" : "bear"}>{gate.passed ? "PASS" : "FAIL"}</Badge>
                <span className="min-w-0 flex-1 truncate text-[11px] text-ink-2">{gate.label}</span>
                <span className={cx("shrink-0 text-[10px] tabular-nums", gate.passed ? "text-bull" : "text-ink-3")}>{gate.detail}</span>
              </div>
            ))}
          </div>
        </Card>

        <Card
          title="Cumulative Shadow Edge"
          subtitle="running net bps across the nursery ledger"
          actions={<Badge tone="muted">{`${fills.length} PTS`}</Badge>}
        >
          {cumulative.length === 0 ? (
            <EmptyState title="No shadow fills" hint="Run a shadow burst in PAPER to seed the curve." />
          ) : (
            <>
              <LineChart
                series={[{ name: "cum net bps", points: cumulative }]}
                baseline={0}
                height={188}
                labels={[chronological.length > 0 ? chronological[0].when : ""]}
              />
              <div className="mt-1 flex items-center justify-between text-[10px] uppercase tracking-[0.1em] text-ink-3">
                <span>last {cumulative.length} fills</span>
                <span className={cx("tabular-nums", (cumulative[cumulative.length - 1] ?? 0) >= 0 ? "text-bull" : "text-bear")}>
                  {signed(cumulative[cumulative.length - 1] ?? 0, 2)} bps cum
                </span>
              </div>
            </>
          )}
        </Card>
      </div>

      <Card
        title="Shadow Fill Ledger"
        subtitle="shadow price vs live mark · signed net edge"
        actions={
          <Button
            variant="ghost"
            size="sm"
            icon="play"
            disabled={mode === "LIVE" || busy}
            title={mode === "LIVE" ? "PAPER-only" : "Queue 10 shadow fills"}
            onClick={() => void burst()}
          >
            Burst
          </Button>
        }
        dense
      >
        {loading ? (
          <div className="flex items-center gap-2 p-3 text-[11px] text-ink-3">
            <Spinner /> loading shadow ledger
          </div>
        ) : fills.length === 0 ? (
          <EmptyState title="Ledger empty" hint="Settled shadow fills appear here with slippage and net edge." />
        ) : (
          <Table columns={["When", "Symbol", "Side", "Qty", "Shadow Px", "Live Px", "Slip bps", "Net bps"]}>
            {fills.slice(0, 80).map((fill, index) => (
              <Row key={`${fill.symbol}-${fill.when}-${index}`}>
                <Cell mono tone="muted">{fill.when}</Cell>
                <Cell mono tone="accent">{fill.symbol}</Cell>
                <Cell>
                  <Badge tone={fill.side === "BUY" ? "bull" : fill.side === "SELL" ? "bear" : "muted"}>{fill.side}</Badge>
                </Cell>
                <Cell align="right">{num(fill.qty, 0)}</Cell>
                <Cell align="right">{num(fill.shadowPx, 2)}</Cell>
                <Cell align="right" tone="muted">{num(fill.livePx, 2)}</Cell>
                <Cell align="right" tone={fill.slipBps > 0 ? "bear" : "neutral"}>{signed(fill.slipBps, 2)}</Cell>
                <Cell align="right" tone={fill.netBps >= 0 ? "bull" : "bear"}>{signed(fill.netBps, 2)}</Cell>
              </Row>
            ))}
          </Table>
        )}
      </Card>
    </div>
  );
}
