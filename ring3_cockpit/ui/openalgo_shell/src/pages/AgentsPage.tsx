import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { Icon } from "../components/Icon";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  EmptyState,
  Field,
  PageHeader,
  ProgressBar,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  StatusDot,
  Table,
  TextArea,
  TextInput,
  Toolbar,
} from "../components/ui";
import { SILOS } from "../config/nav";
import { api, errorMessage } from "../lib/api";
import {
  asArray,
  asRecord,
  duration,
  int,
  num,
  pct,
  safeNumber,
  shortHash,
  text,
  titleize,
  upper,
} from "../lib/format";
import { rowsOf } from "../lib/payload";
import { useTicker } from "../state/session";
import type { PageProps } from "./types";

type Note = { tone: "neutral" | "bull" | "bear"; message: string };
type Turn = { id: string; role: "operator" | "agent"; text: string; hash: string; claims: number };

const KNOWN_AGENTS: Array<[string, string, string, string]> = [
  ["fundamental_analyst", "Fundamental Analyst", "SYSTEM 2 · DEEP", "VALUATION_AUDIT"],
  ["technical_analyst", "Technical Analyst", "SYSTEM 2 · FAST", "STRATEGY_EVALUATION"],
  ["news_analyst", "News Analyst", "SYSTEM 2 · FAST", "NEWS_SCAN"],
  ["sentiment_analyst", "Sentiment Analyst", "SYSTEM 2 · FAST", "SENTIMENT_SCAN"],
  ["risk_manager", "Risk Manager", "SYSTEM 2 · DEEP", "RISK_AUDIT"],
  ["portfolio_manager", "Portfolio Manager", "SYSTEM 2 · DEEP", "ALLOCATION_REVIEW"],
];

/** Catalog rows shaped like the backend AgentSpec so one renderer serves both. */
const knownAgentRows = (): Record<string, unknown>[] =>
  KNOWN_AGENTS.map(([agentId, title, tier, taskType]) => ({
    agent_id: agentId,
    title,
    model_tier: tier,
    task_type: taskType,
  }));

const PRIORITIES = ["LOW", "NORMAL", "HIGH", "CRITICAL"].map((value) => ({ value, label: value }));
const SILO_OPTIONS = SILOS.map((silo) => ({
  value: silo.id,
  label: `${silo.short} · ${silo.floorBps.toFixed(2)} bps`,
}));
const STRATEGIES = [
  "ma_golden_cross",
  "bullish_trend_alignment",
  "chanlun_pivot_breakout",
  "elliott_wave_impulse",
  "event_catalyst_momentum",
  "growth_quality_compounder",
  "expectation_rerating",
  "cross_asset_etf_rotation",
].map((value) => ({ value, label: titleize(value) }));

function stampNs(value: unknown): string {
  const ns = safeNumber(value, Number.NaN);
  if (!Number.isFinite(ns) || ns <= 0) return "—";
  return new Date(ns / 1e6).toISOString().replace("T", " ").slice(0, 19);
}

function ScoreDial({ score }: { score: number }) {
  const known = Number.isFinite(score);
  const clamped = known ? Math.max(0, Math.min(100, score)) : 0;
  return (
    <div className="flex flex-col items-center gap-1">
      <svg viewBox="0 0 120 70" className="w-[176px]" role="img" aria-label="decision score dial">
        <path
          d="M10 60 A 50 50 0 0 1 110 60"
          fill="none"
          pathLength={100}
          strokeWidth={6}
          className="stroke-hairline"
        />
        {known ? (
          <path
            d="M10 60 A 50 50 0 0 1 110 60"
            fill="none"
            pathLength={100}
            strokeWidth={6}
            strokeDasharray={`${clamped.toFixed(2)} 100`}
            className="stroke-accent"
          />
        ) : null}
        <text x="60" y="46" textAnchor="middle" className="fill-ink" fontSize="21" fontWeight="600">
          {known ? num(clamped, 0) : "—"}
        </text>
        <text x="60" y="59" textAnchor="middle" className="fill-ink-3" fontSize="8">
          0 — 100
        </text>
      </svg>
    </div>
  );
}

export function AgentsPage({ mode }: PageProps) {
  const [roster, setRoster] = useState<unknown>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);

  const [silo, setSilo] = useState("US_IBKR");
  const [agentId, setAgentId] = useState("daily_decision_agent");
  const [symbol, setSymbol] = useState("SPY");
  const [strategy, setStrategy] = useState("ma_golden_cross");
  const [prompt, setPrompt] = useState("Summarise positioning, Pine verdict and macro risk.");
  const [priority, setPriority] = useState("NORMAL");
  const [watchlist, setWatchlist] = useState("SPY, QQQ, NVDA");

  const [task, setTask] = useState<Record<string, unknown> | null>(null);
  const [dashboardSymbol, setDashboardSymbol] = useState("");
  const [question, setQuestion] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);

  const [memory, setMemory] = useState<Record<string, unknown>[]>([]);
  const [memoryLoading, setMemoryLoading] = useState(true);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState("");

  const now = useTicker(1000);

  const loadRoster = useCallback(async () => {
    try {
      const payload = await api.get<unknown>("/cockpit/agents");
      setRoster(payload);
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  const loadMemory = useCallback(async () => {
    setMemoryLoading(true);
    try {
      const payload = await api.get<unknown>("/memory/entities", { limit: 25 });
      setMemory(rowsOf(payload, ["entities"]));
      setMemoryError(null);
    } catch (caught) {
      setMemoryError(errorMessage(caught));
      setMemory([]);
    } finally {
      setMemoryLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadRoster();
    void loadMemory();
    const timer = globalThis.setInterval(() => void loadRoster(), 10_000);
    return () => globalThis.clearInterval(timer);
  }, [loadRoster, loadMemory]);

  const tasks = useMemo(() => rowsOf(roster, ["tasks"]), [roster]);
  const token = useMemo(() => asRecord(asRecord(roster).token), [roster]);

  const agentCards = useMemo(() => {
    const rows = rowsOf(roster, ["agents"]);
    const source = rows.length > 0 ? rows : knownAgentRows();
    return source.slice(0, 6).map((row) => {
      const id = text(row.agent_id ?? row.id, "AGENT");
      const mine = tasks.filter((row_) => text(row_.agent_id, "") === id);
      const failed = mine.filter((row_) => upper(row_.status) === "FAILED").length;
      const active = mine.filter((row_) => ["PENDING", "PROCESSING"].includes(upper(row_.status))).length;
      const done = mine.filter((row_) => upper(row_.status) === "COMPLETED").length;
      return {
        id,
        title: text(row.title ?? row.role, titleize(id)),
        model: text(row.model_tier ?? row.model, "SYSTEM 2"),
        ring: upper(row.ring ?? "RING 2"),
        taskType: text(row.task_type, "—"),
        done,
        active,
        failed,
        tone:
          failed > 0
            ? ("bear" as const)
            : active > 0
              ? ("accent" as const)
              : done > 0
                ? ("bull" as const)
                : ("muted" as const),
      };
    });
  }, [roster, tasks]);

  const spend = safeNumber(token.spend_usd, 0);
  const cap = Math.max(0.01, safeNumber(token.cap_usd, 3.1));
  const remaining = safeNumber(token.remaining_usd, Math.max(0, cap - spend));
  const utilization = safeNumber(token.utilization_pct, cap > 0 ? (spend / cap) * 100 : 0);

  const structured = useMemo(() => asRecord(task?.structured_result), [task]);
  const reports = useMemo(() => rowsOf(structured, ["reports", "ranking"]), [structured]);
  const activeSymbol = dashboardSymbol || text(reports[0]?.symbol, "") || upper(symbol);
  const report = reports.find((row) => upper(row.symbol) === upper(activeSymbol)) ?? reports[0];
  const score = safeNumber(report?.signal_score, Number.NaN);
  const verdict = upper(report?.operation_advice ?? structured.pine_consensus ?? structured.top_symbol);
  const pine = asRecord(asRecord(structured.pine_by_symbol)[activeSymbol]);
  const pineStudies = rowsOf(pine, ["pine_studies"]);
  const macro = asRecord(asRecord(structured.macro_risk_by_symbol)[activeSymbol]);
  const macroEvents = rowsOf(macro, ["macro_events"]);
  const macroScore = safeNumber(macro.macro_risk_score, Number.NaN);
  const selectedTaskId = text(task?.task_id, "");

  const assign = useCallback(async () => {
    const token_ = upper(symbol.trim());
    if (!token_) {
      setNote({ tone: "bear", message: "A symbol is required before dispatch." });
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const payload = await api.post<unknown>("/control/agents/assign", {
        agent_id: agentId,
        silo,
        symbols: [token_],
        strategy_id: strategy,
        prompt,
        priority,
      });
      const record = asRecord(payload);
      setTask(record);
      setDashboardSymbol(token_);
      setTurns([]);
      setNote({
        tone: "bull",
        message: `TASK ${text(record.task_id, "—")} · ${upper(record.status)} · ${upper(record.agent_id)}`,
      });
      void loadRoster();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, [agentId, silo, strategy, prompt, priority, symbol, loadRoster]);

  const runBatch = useCallback(async () => {
    const symbols = Array.from(
      new Set(
        watchlist
          .split(/[\s,;]+/)
          .map((entry) => upper(entry.trim()))
          .filter(Boolean),
      ),
    ).slice(0, 40);
    if (symbols.length === 0) {
      setNote({ tone: "bear", message: "The watchlist is empty; paste comma or newline separated symbols." });
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const payload = await api.post<unknown>("/control/agents/batch-watchlist", {
        silo,
        symbols,
        strategy_id: strategy,
        priority,
      });
      const record = asRecord(payload);
      setTask(record);
      setDashboardSymbol(symbols[0]);
      setNote({
        tone: "bull",
        message: `BATCH SEALED · ${symbols.length} SYMBOLS · ${upper(record.status)}`,
      });
      void loadRoster();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, [watchlist, silo, strategy, priority, loadRoster]);

  const inspectTask = useCallback(async (taskId: string) => {
    if (!taskId) return;
    setBusy(true);
    try {
      const payload = await api.get<unknown>(`/control/agents/tasks/${encodeURIComponent(taskId)}`);
      const record = asRecord(payload);
      setTask(record);
      setDashboardSymbol(
        asArray(record.symbols)
          .map((entry) => text(entry, ""))
          .filter(Boolean)[0] ?? "",
      );
      setNote({ tone: "neutral", message: `LOADED ${upper(taskId)} · ${upper(record.status)}` });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, []);

  const cancel = useCallback(
    async (taskId: string) => {
      try {
        await api.post<unknown>("/control/agents/cancel", { task_id: taskId });
        setNote({
          tone: "bull",
          message: taskId === "ALL" ? "TASK QUEUE CLEARED" : `CANCELLED ${upper(taskId)}`,
        });
        void loadRoster();
      } catch (caught) {
        setNote({ tone: "bear", message: errorMessage(caught) });
      }
    },
    [loadRoster],
  );

  const followup = useCallback(async () => {
    if (!selectedTaskId) {
      setNote({ tone: "bear", message: "Select a completed task before asking a grounded question." });
      return;
    }
    const asked = question.trim();
    if (asked.length === 0) {
      setNote({ tone: "bear", message: "A question is required." });
      return;
    }
    setBusy(true);
    try {
      const payload = await api.post<unknown>("/control/agents/followup", {
        task_id: selectedTaskId,
        question: asked,
      });
      const record = asRecord(payload);
      const history = rowsOf(record, ["conversation_history"]);
      const last = history[history.length - 1];
      setTurns((prev) => [
        ...prev,
        { id: `${Date.now()}-q`, role: "operator", text: asked, hash: "", claims: 0 },
        {
          id: `${Date.now()}-a`,
          role: "agent",
          text: text(record.answer, "—"),
          hash: text(record.grounding_sha256 ?? last?.grounding_sha256, ""),
          claims: safeNumber(last?.claims_checked, 0),
        },
      ]);
      setQuestion("");
      setNote({ tone: "bull", message: `GROUNDED ANSWER · ${upper(record.agent_id)}` });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, [question, selectedTaskId]);

  const glyph = mode === "LIVE" ? "live" : "paper";

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="AGENTS"
        subtitle="6 Ring 2 specialists, grounded decision dashboard, post-mortem memory and strategy Q&A"
        badges={
          <>
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone="muted" mono>
              RING 2 · AIR-GAPPED
            </Badge>
          </>
        }
        actions={
          <>
            <Button icon="refresh" variant="subtle" size="sm" onClick={() => void loadRoster()}>
              Refresh
            </Button>
            <Button icon="agents" variant="ghost" size="sm" onClick={() => void loadMemory()}>
              Memory
            </Button>
          </>
        }
      />

      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={4}>
        <Stat label="Specialists" value={int(agentCards.length)} hint="ring 2 · no signing tools" />
        <Stat
          label="Task queue"
          value={int(tasks.length)}
          hint={`${int(tasks.filter((row) => ["PENDING", "PROCESSING"].includes(upper(row.status))).length)} active`}
        />
        <Stat
          label="Completed"
          value={int(tasks.filter((row) => upper(row.status) === "COMPLETED").length)}
          tone="bull"
        />
        <Stat
          label="Token spend"
          value={`$${num(spend, 4)}`}
          hint={`cap $${num(cap, 2)}/day`}
          tone={utilization > 80 ? "bear" : "bull"}
        />
      </StatGrid>

      <Card
        title="Token budget"
        subtitle="Hard daily cap on every Ring 2 inference call"
        icon="shield"
        actions={
          <Badge tone={remaining <= 0 ? "bear" : "bull"} mono>
            {remaining <= 0 ? "LOCKED" : "HEADROOM"}
          </Badge>
        }
      >
        <ProgressBar value={spend} max={cap} tone={utilization > 80 ? "bear" : "accent"} height={6} />
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] tabular-nums text-ink-2">
          <span>SPEND ${num(spend, 4)}</span>
          <span>CAP ${num(cap, 2)}</span>
          <span>REMAINING ${num(remaining, 4)}</span>
          <span>{pct(utilization, 1)} UTILISED</span>
        </div>
        <p className="mt-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
          TOKEN BUDGET — HARD CAP · ledger locked: {text(token.locked, "false")}
        </p>
      </Card>

      <Card
        title="Agent roster"
        subtitle="id · role · ring · model tier · status · tasks completed"
        icon="agents"
      >
        {loading && agentCards.length === 0 ? (
          <div className="flex items-center gap-2 text-[11px] text-ink-3">
            <Spinner /> loading roster…
          </div>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {agentCards.map((card) => (
              <button
                key={card.id}
                type="button"
                onClick={() => setAgentId(card.id)}
                className={`flex flex-col gap-1.5 rounded-[4px] border px-2.5 py-2 text-left transition-colors ${
                  agentId === card.id
                    ? "border-accent bg-elevated"
                    : "border-hairline bg-canvas hover:bg-elevated"
                }`}
              >
                <div className="flex items-center justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-1.5">
                    <StatusDot tone={card.tone} pulse={card.active > 0} />
                    <span className="truncate text-[12px] font-semibold text-ink">{card.title}</span>
                  </div>
                  <Badge tone="muted" mono>
                    {card.ring}
                  </Badge>
                </div>
                <div className="truncate font-mono text-[10px] text-ink-3">{card.id}</div>
                <div className="flex items-center justify-between text-[10px] uppercase tracking-[0.08em] text-ink-3">
                  <span>{card.model}</span>
                  <span className="tabular-nums text-ink-2">{int(card.done)} done</span>
                </div>
                <div className="flex flex-wrap gap-1">
                  <Badge tone="neutral" mono>
                    {card.taskType}
                  </Badge>
                </div>
              </button>
            ))}
          </div>
        )}
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card title="Assign specialist" subtitle="POST /control/agents/assign" icon="target">
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label="Silo">
              <SelectInput value={silo} onChange={setSilo} options={SILO_OPTIONS} />
            </Field>
            <Field label="Agent">
              <SelectInput
                value={agentId}
                onChange={setAgentId}
                options={agentCards.map((card) => ({ value: card.id, label: card.title }))}
              />
            </Field>
            <Field label="Symbol">
              <TextInput
                value={symbol}
                mono
                onChange={(event) => setSymbol(event.target.value.toUpperCase())}
              />
            </Field>
            <Field label="Strategy">
              <SelectInput value={strategy} onChange={setStrategy} options={STRATEGIES} />
            </Field>
            <Field label="Priority">
              <SelectInput value={priority} onChange={setPriority} options={PRIORITIES} />
            </Field>
            <Field label="Prompt" className="sm:col-span-2">
              <TextInput value={prompt} onChange={(event) => setPrompt(event.target.value)} />
            </Field>
          </div>
          <Toolbar className="mt-2">
            <Button variant="primary" size="sm" icon="play" disabled={busy} onClick={() => void assign()}>
              Dispatch task
            </Button>
            {busy ? <Spinner /> : null}
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
              {glyph} · grounded report · no order signing
            </span>
          </Toolbar>
        </Card>

        <Card
          title="Multi-symbol watchlist scan"
          subtitle="POST /control/agents/batch-watchlist"
          icon="layer"
        >
          <TextArea rows={3} value={watchlist} onChange={setWatchlist} placeholder="SPY, QQQ, NVDA" />
          <Toolbar className="mt-2">
            <Button
              variant="primary"
              size="sm"
              icon="screener"
              disabled={busy}
              onClick={() => void runBatch()}
            >
              Rank watchlist
            </Button>
            <Badge tone="muted" mono>
              {int(watchlist.split(/[\s,;]+/).filter(Boolean).length)} SYMBOLS
            </Badge>
          </Toolbar>
          {reports.length > 0 && structured.batch === true ? (
            <div className="mt-2">
              <Table columns={["#", "SYMBOL", "SCORE", "ADVICE"]} dense>
                {reports.slice(0, 10).map((row, index) => (
                  <Row key={index}>
                    <Cell mono tone="muted">
                      {int(index + 1)}
                    </Cell>
                    <Cell mono>{text(row.symbol)}</Cell>
                    <Cell align="right" mono>
                      {num(row.signal_score, 1)}
                    </Cell>
                    <Cell align="right">
                      <Badge
                        tone={
                          upper(row.operation_advice).includes("BUY")
                            ? "bull"
                            : upper(row.operation_advice).includes("SELL")
                              ? "bear"
                              : "neutral"
                        }
                        mono
                      >
                        {upper(row.operation_advice)}
                      </Badge>
                    </Cell>
                  </Row>
                ))}
              </Table>
            </div>
          ) : (
            <p className="mt-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
              RANKED LIST APPEARS HERE AFTER A BATCH RUN
            </p>
          )}
        </Card>
      </div>

      <Card
        title="Task queue"
        subtitle="Cancel one task or clear the queue"
        icon="orders"
        actions={
          <Button
            variant="danger"
            size="sm"
            icon="close"
            onClick={() => void cancel("ALL")}
            disabled={tasks.length === 0}
          >
            Clear all
          </Button>
        }
        dense
      >
        {tasks.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="Queue empty"
              hint="Dispatch a specialist or rank a watchlist to create the first grounded task."
            />
          </div>
        ) : (
          <Table columns={["TASK", "AGENT", "SYMBOL", "STATE", "PRIORITY", "AGE", ""]}>
            {tasks.slice(0, 20).map((row) => {
              const id = text(row.task_id, "");
              const ageS = Math.max(0, (now - safeNumber(row.created_at_ns, now * 1e6) / 1e6) / 1000);
              return (
                <Row key={id} className={id === selectedTaskId ? "bg-elevated" : undefined}>
                  <Cell mono tone="muted">
                    {shortHash(id, 14)}
                  </Cell>
                  <Cell>{text(row.agent_id)}</Cell>
                  <Cell mono>
                    {asArray(row.symbols)
                      .map((entry) => text(entry, ""))
                      .filter(Boolean)
                      .join(" ") || "—"}
                  </Cell>
                  <Cell>
                    <Badge
                      tone={
                        upper(row.status) === "FAILED"
                          ? "bear"
                          : upper(row.status) === "COMPLETED"
                            ? "bull"
                            : "accent"
                      }
                      mono
                    >
                      {upper(row.status)}
                    </Badge>
                  </Cell>
                  <Cell mono tone={upper(row.priority) === "CRITICAL" ? "bear" : "muted"}>
                    {upper(row.priority)}
                  </Cell>
                  <Cell align="right" mono>
                    {duration(ageS)}
                  </Cell>
                  <Cell align="right">
                    <span className="inline-flex items-center gap-1">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => void inspectTask(id)}
                        title="Open this task"
                      >
                        open
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => void cancel(id)}
                        title="Cancel this task"
                      >
                        cancel
                      </Button>
                    </span>
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>

      <Card
        title="daily_stock_analysis decision dashboard"
        subtitle={`${activeSymbol} · signal score, Pine study verdicts and macro risk`}
        icon="analyzer"
        actions={
          <Badge tone={structured.verified === true ? "bull" : "muted"} mono>
            {structured.verified === true ? "GROUNDED" : "UNSEALED"}
          </Badge>
        }
      >
        <div className="grid gap-3 md:grid-cols-[176px_1fr]">
          <div className="flex flex-col items-center justify-start gap-1 rounded-[4px] border border-hairline bg-canvas py-2">
            <ScoreDial score={score} />
            <Badge
              tone={verdict.includes("BUY") ? "bull" : verdict.includes("SELL") ? "bear" : "neutral"}
              mono
            >
              {verdict || "—"}
            </Badge>
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">VERDICT</span>
          </div>
          <div className="flex flex-col gap-2">
            <div className="flex flex-wrap gap-1">
              {pineStudies.length === 0 ? (
                <Badge tone="muted" mono>
                  NO PINE STUDY IN PAYLOAD
                </Badge>
              ) : (
                pineStudies.map((study, index) => (
                  <Badge
                    key={index}
                    tone={
                      upper(study.bias).includes("BUY")
                        ? "bull"
                        : upper(study.bias).includes("SELL")
                          ? "bear"
                          : "neutral"
                    }
                    mono
                  >
                    {text(study.study_id, `STUDY ${index + 1}`)} · {upper(study.bias)}
                  </Badge>
                ))
              )}
            </div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] md:grid-cols-4">
              <div>
                <div className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">CLOSE</div>
                <div className="font-semibold tabular-nums text-ink">{num(report?.close_price, 2)}</div>
              </div>
              <div>
                <div className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">MA 5 / 20</div>
                <div className="font-semibold tabular-nums text-ink-2">
                  {num(report?.ma5, 2)} / {num(report?.ma20, 2)}
                </div>
              </div>
              <div>
                <div className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">RSI 14</div>
                <div className="font-semibold tabular-nums text-ink-2">{num(report?.rsi_14, 1)}</div>
              </div>
              <div>
                <div className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">REWARD / RISK</div>
                <div className="font-semibold tabular-nums text-ink-2">{num(report?.reward_risk, 2)}</div>
              </div>
            </div>
            <div className="rounded-[4px] border border-hairline">
              <div className="flex items-center justify-between border-b border-hairline px-2 py-1">
                <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
                  MACRO RISK · NEXT 72H
                </span>
                <span className="font-semibold tabular-nums text-ink-2">
                  score {Number.isFinite(macroScore) ? num(macroScore, 3) : "—"}
                </span>
              </div>
              {macroEvents.length === 0 ? (
                <div className="px-2 py-1.5 text-[10px] uppercase tracking-[0.1em] text-ink-3">
                  NO SCHEDULED RELEASES
                </div>
              ) : (
                macroEvents.slice(0, 5).map((event, index) => (
                  <div
                    key={index}
                    className="flex items-center justify-between gap-2 border-b border-hairline/60 px-2 py-1 text-[11px] last:border-b-0"
                  >
                    <span className="truncate text-ink-2">{text(event.name, "RELEASE")}</span>
                    <span className="shrink-0 tabular-nums text-ink-3">
                      {upper(event.impact)} · {num(event.hours_until, 1)}h
                    </span>
                  </div>
                ))
              )}
            </div>
            {selectedTaskId ? (
              <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
                TASK {selectedTaskId} · fact count {int(task?.fact_count)}
              </span>
            ) : (
              <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
                NO TASK LOADED — DISPATCH ONE TO FILL THE DASHBOARD
              </span>
            )}
          </div>
        </div>
      </Card>

      <Card
        title="Entity post-mortem memory"
        subtitle="GET /memory/entities · compiled truths and failure modes"
        icon="database"
        actions={
          <Button icon="refresh" variant="subtle" size="sm" onClick={() => void loadMemory()}>
            Reload
          </Button>
        }
        dense
      >
        {memoryError ? (
          <div className="p-3">
            <Banner tone="bear">{memoryError}</Banner>
          </div>
        ) : null}
        {memoryLoading ? (
          <div className="flex items-center gap-2 p-3 text-[11px] text-ink-3">
            <Spinner /> loading entity memory…
          </div>
        ) : memory.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="No compiled entities"
              hint="Post-mortems compile into entity truths after settled fills."
            />
          </div>
        ) : (
          <Table columns={["ENTITY", "TRUTHS", "LAST POST-MORTEM", "CONFIDENCE", ""]}>
            {memory.map((row) => {
              const id = text(row.entity_id, "ENTITY");
              const modes = asArray(row.known_failure_modes)
                .map((entry) => text(entry, ""))
                .filter(Boolean);
              const regimes = asArray(row.caution_regimes)
                .map((entry) => text(entry, ""))
                .filter(Boolean);
              const haircut = Math.min(0.4, modes.length * 0.05 + regimes.length * 0.1);
              const confidence = safeNumber(row.confidence ?? row.confidence_multiplier, Number.NaN);
              const isOpen = expanded === id;
              const declaredTruths = asArray(row.compiled_entity_truths).map((entry) => text(entry, ""));
              const truthLines =
                declaredTruths.length > 0
                  ? declaredTruths
                  : [
                      `sample count ${int(row.sample_count)}`,
                      `win rate ${num(safeNumber(row.win_rate_bps, Number.NaN), 1)} bps`,
                      `avg slippage ${num(safeNumber(row.avg_slippage_bps, Number.NaN), 2)} bps`,
                      `worst drawdown ${num(safeNumber(row.worst_drawdown_bps, Number.NaN), 2)} bps`,
                      ...modes.map((mode) => `failure mode · ${mode}`),
                      ...regimes.map((regime) => `caution regime · ${regime}`),
                    ];
              const timeline = rowsOf(row, ["post_mortem_timeline"]);
              return (
                <Fragment key={id}>
                  <Row
                    className={isOpen ? "bg-elevated" : undefined}
                    onClick={() => setExpanded(isOpen ? "" : id)}
                  >
                    <Cell mono>{id}</Cell>
                    <Cell align="right" mono>
                      {int(row.truth_count ?? row.sample_count)}
                    </Cell>
                    <Cell mono tone="muted">
                      {stampNs(row.last_post_mortem_utc_ns ?? row.last_compiled_utc)}
                    </Cell>
                    <Cell align="right" mono tone={Number.isFinite(confidence) ? "neutral" : "muted"}>
                      {Number.isFinite(confidence) ? pct(confidence * 100, 0) : pct((1 - haircut) * 100, 0)}
                    </Cell>
                    <Cell align="right" mono tone="accent">
                      <span className="ml-auto flex items-center text-ink-3">
                        <Icon name={isOpen ? "chevronDown" : "chevronRight"} size={12} />
                      </span>
                    </Cell>
                  </Row>
                  {isOpen ? (
                    <Row key={`${id}-open`} className="bg-canvas">
                      <Cell colSpan={5} className="px-3 py-2">
                        <div className="grid gap-2 md:grid-cols-2">
                          <div>
                            <div className="mb-1 text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
                              COMPILED ENTITY TRUTHS
                            </div>
                            <ul className="flex flex-col gap-1 text-[11px] text-ink-2">
                              {truthLines.map((line, index) => (
                                <li key={index} className="truncate font-mono text-[10.5px]">
                                  {line}
                                </li>
                              ))}
                            </ul>
                          </div>
                          <div>
                            <div className="mb-1 text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
                              POST-MORTEM TIMELINE
                            </div>
                            {timeline.length === 0 ? (
                              <p className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
                                NO TIMELINE IN PAYLOAD · TRUTH {shortHash(row.truth_sha256, 12)}
                              </p>
                            ) : (
                              <ul className="flex flex-col gap-1 text-[11px] text-ink-2">
                                {timeline.map((entry, index) => (
                                  <li key={index} className="truncate font-mono text-[10.5px]">
                                    {stampNs(entry.at_ns ?? entry.ts_ns)} ·{" "}
                                    {text(entry.lesson ?? entry.summary, "—")}
                                  </li>
                                ))}
                              </ul>
                            )}
                          </div>
                        </div>
                      </Cell>
                    </Row>
                  ) : null}
                </Fragment>
              );
            })}
          </Table>
        )}
        <p className="px-3 py-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
          CONFIDENCE = 1 − capped caution haircut (0.05 per failure mode, 0.10 per regime, cap 0.40)
        </p>
      </Card>

      <Card
        title="Grounded strategy Q&A"
        subtitle={`POST /control/agents/followup · task ${selectedTaskId || "—"}`}
        icon="terminal"
      >
        <div className="flex flex-col gap-2">
          {turns.length === 0 ? (
            <EmptyState
              title="No turns yet"
              hint="Ask about levels, indicators, risk or score on the selected completed task."
            />
          ) : (
            turns.map((turn) => (
              <div
                key={turn.id}
                className={`rounded-[4px] border px-2.5 py-2 ${
                  turn.role === "operator" ? "border-hairline bg-canvas" : "border-accent/40 bg-elevated"
                }`}
              >
                <div className="mb-1 flex items-center gap-2">
                  <Badge tone={turn.role === "operator" ? "muted" : "accent"} mono>
                    {turn.role}
                  </Badge>
                  {turn.hash ? (
                    <Badge tone="bull" mono>
                      SHA-256 {shortHash(turn.hash, 10)}
                    </Badge>
                  ) : null}
                  {turn.claims > 0 ? (
                    <span className="text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
                      {turn.claims} claims verified
                    </span>
                  ) : null}
                </div>
                <pre className="nios-scroll max-h-56 whitespace-pre-wrap font-mono text-[10.5px] leading-relaxed text-ink-2">
                  {turn.text}
                </pre>
              </div>
            ))
          )}
        </div>
        <Toolbar className="mt-2">
          <div className="min-w-[240px] flex-1">
            <TextInput
              value={question}
              placeholder="What are the entry, target and stop levels?"
              onChange={(event) => setQuestion(event.target.value)}
            />
          </div>
          <Button
            variant="primary"
            size="sm"
            icon="search"
            disabled={busy || !selectedTaskId}
            onClick={() => void followup()}
          >
            Ask
          </Button>
          {!selectedTaskId ? (
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">SELECT A TASK FIRST</span>
          ) : null}
        </Toolbar>
      </Card>
    </div>
  );
}

export default AgentsPage;
