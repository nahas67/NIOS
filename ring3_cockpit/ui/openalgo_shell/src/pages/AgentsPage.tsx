/**
 * Ring 2 agent task hub.
 *
 * Six specialist agents reason over a symbol or a watchlist; none of them can
 * sign an order or read a broker key. Every assignment, batch scan and follow-up
 * returns a SHA-256 grounding digest and is charged against the daily token
 * budget. The decision dashboard is assembled from the freshest grounded result
 * the operator has in hand.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import { api } from "@/lib/api";
import type {
  AgentAssignReceipt,
  AgentBatchWatchlistReceipt,
  AgentFollowupReceipt,
  AgentSpec,
  AgentTaskRow,
  JsonRecord,
  TokenUsage,
} from "@/lib/types";
import { ago, clock, int, latency, num, record, rows, shortId, strings, text, titleize, utc } from "@/lib/format";
import { useAction, useNow, useResource } from "@/lib/use-api";
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
  type PillTone,
} from "@/design-system/primitives";
import { RadialGauge } from "@/charts/RadialGauge";

function parseSymbols(raw: string): string[] {
  const seen = new Set<string>();
  const symbols: string[] = [];
  for (const token of raw.split(",")) {
    const symbol = token.trim().toUpperCase();
    if (!symbol || seen.has(symbol)) continue;
    seen.add(symbol);
    symbols.push(symbol);
  }
  return symbols;
}

function adviceTone(advice: string): PillTone {
  switch (advice) {
    case "STRONG_BUY":
      return "ok";
    case "BUY":
      return "accent";
    case "HOLD":
      return "warn";
    case "WAIT":
      return "warn";
    case "SELL":
      return "bad";
    case "AVOID":
      return "bad";
    default:
      return "neutral";
  }
}

function taskTone(status: string): PillTone {
  switch (status.toUpperCase()) {
    case "COMPLETED":
      return "ok";
    case "PROCESSING":
      return "accent";
    case "PENDING":
      return "info";
    case "CANCELLED":
      return "warn";
    case "FAILED":
      return "bad";
    default:
      return "neutral";
  }
}

function InlineAlert({ message }: { message: string }): ReactNode {
  return (
    <p
      role="alert"
      className="rounded-[var(--r-sm)] border border-[rgba(251,191,36,0.35)] bg-[var(--warn-soft)] px-2.5 py-1.5 text-[11px] text-[var(--warn)]"
    >
      {message}
    </p>
  );
}

function MicroStat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }): ReactNode {
  return (
    <div className="min-w-0 rounded-[var(--r-xs)] border border-[var(--line)] px-2 py-1">
      <p className="kpi-label">{label}</p>
      <div className="mono tnum truncate text-[11px] text-[var(--ink-soft)]">{value}</div>
      {hint ? <p className="truncate text-[9px] text-[var(--dim)]">{hint}</p> : null}
    </div>
  );
}

function ReasonList({
  title,
  items,
  tone,
}: {
  title: string;
  items: string[];
  tone: "pos" | "neg";
}): ReactNode {
  return (
    <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--panel-sunken)] p-2.5">
      <p className={`eyebrow ${tone === "pos" ? "text-[var(--pos)]" : "text-[var(--neg)]"}`}>{title}</p>
      {items.length === 0 ? (
        <p className="mt-1 text-[10.5px] text-[var(--dim)]">No sealed {title.toLowerCase()} on this row.</p>
      ) : (
        <ul className="mt-1.5 flex list-none flex-col gap-1 p-0">
          {items.slice(0, 6).map((item, index) => (
            <li
              key={`${title}-${index}-${item.slice(0, 18)}`}
              className="flex gap-1.5 text-[10.5px] leading-snug text-[var(--ink-soft)]"
            >
              <span
                className={`mt-[5px] h-1 w-1 flex-none rounded-full ${
                  tone === "pos" ? "bg-[var(--pos)]" : "bg-[var(--neg)]"
                }`}
                aria-hidden="true"
              />
              <span>{item}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AgentCard({ agent }: { agent: AgentSpec }): ReactNode {
  return (
    <GlassPanel
      eyebrow={titleize(agent.task_type)}
      title={agent.title}
      actions={<StatusPill value={text(agent.ring, "ring2")} tone="accent2" />}
    >
      <p className="mono text-[10px] tracking-[0.04em] text-[var(--accent)]">{agent.agent_id}</p>
      <p className="mt-1 text-[11.5px] leading-relaxed text-[var(--muted)]">{agent.description}</p>
      <div className="chip-row mt-2">
        {agent.capabilities.map((capability) => (
          <span key={capability} className="pill" title={capability}>
            {titleize(capability)}
          </span>
        ))}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {agent.signs_orders ? <StatusPill value="SIGNS ORDERS" tone="bad" /> : null}
        {agent.reads_broker_keys ? <StatusPill value="READS KEYS" tone="bad" /> : null}
        {!agent.signs_orders && !agent.reads_broker_keys ? <StatusPill value="RESEARCH ONLY" tone="ok" /> : null}
        {agent.requires_strategy ? (
          <StatusPill value="strategy required" tone="accent2" />
        ) : (
          <StatusPill value="symbols only" tone="neutral" />
        )}
      </div>
      <p className="mono tnum mt-2 text-[9.5px] text-[var(--dim)]">~${num(agent.token_cost_usd, 6)} per call</p>
    </GlassPanel>
  );
}

function TokenBudgetCard({ token }: { token: TokenUsage }): ReactNode {
  const utilization = Number.isFinite(token.utilization_pct) ? token.utilization_pct : 0;
  const tone: "neg" | "warn" | "accent" = utilization >= 90 ? "neg" : utilization >= 70 ? "warn" : "accent";
  return (
    <GlassPanel
      eyebrow="Budget"
      title="Daily agent token spend"
      actions={
        <StatusPill
          value={token.locked ? "LOCKED" : "OPEN"}
          tone={token.locked ? "bad" : "ok"}
          title="A locked ledger refuses new agent calls"
        />
      }
    >
      <div className="row-between flex-wrap gap-2">
        <p className="mono tnum text-[16px] font-semibold tracking-tight text-[var(--ink)]">
          ${num(token.spend_usd, 4)} <span className="text-[var(--dim)]">/ ${num(token.cap_usd, 2)} per day</span>
        </p>
        <span className="mono tnum text-[10px] text-[var(--dim)]">{int(token.calls)} calls</span>
      </div>
      <div className="mt-2">
        <InlineBar
          value={utilization}
          max={100}
          tone={tone}
          label="Daily token budget utilization"
        />
      </div>
      <div className="mt-2 grid grid-cols-3 gap-2">
        <MetricTile
          label="Utilization"
          value={<TabularValue value={utilization} kind="pct" />}
          hint="of the daily cap"
          tone={utilization >= 90 ? "neg" : utilization >= 70 ? "warn" : "accent"}
        />
        <MetricTile
          label="Remaining"
          value={`$${num(token.remaining_usd, 4)}`}
          hint="before the ledger locks"
        />
        <MetricTile
          label="Tokens"
          value={`${int(token.prompt_tokens)} / ${int(token.completion_tokens)}`}
          hint="prompt / completion"
        />
      </div>
    </GlassPanel>
  );
}

function taskFocusRow(task: AgentTaskRow): Record<string, unknown> | null {
  const result = record(task.structured_result);
  return rows(result.reports)[0] ?? rows(result.ranking)[0] ?? null;
}

function latestTaskFocus(
  tasks: AgentTaskRow[],
): { row: Record<string, unknown>; at: number; source: string } | null {
  for (const task of [...tasks].reverse()) {
    if (task.status !== "COMPLETED") continue;
    const row = taskFocusRow(task);
    if (row) {
      return {
        row,
        at: task.completed_at_ns > 0 ? task.completed_at_ns : task.created_at_ns,
        source: "most recent completed task",
      };
    }
  }
  return null;
}

export function AgentsPage(): ReactNode {
  const res = useResource("agents", () => api.agents(), 12_000);
  const now = useNow(15_000);
  const assigned = useAction<AgentAssignReceipt>();
  const batch = useAction<AgentBatchWatchlistReceipt>();
  const follow = useAction<AgentFollowupReceipt>();
  const cancelAction = useAction<JsonRecord>();

  const data = res.data;
  const agents = data?.agents ?? [];
  const strategies = data?.strategies ?? [];
  const tasks = data?.tasks ?? [];
  const silos = data?.silos ?? [];
  const priorities = data?.priorities ?? [];
  const token = data?.token;

  const [agentChoice, setAgentChoice] = useState("");
  const [siloChoice, setSiloChoice] = useState("");
  const [priorityChoice, setPriorityChoice] = useState("");
  const [strategyChoice, setStrategyChoice] = useState("");
  const [symbolsText, setSymbolsText] = useState("");
  const [prompt, setPrompt] = useState("");
  const [assignError, setAssignError] = useState<string | null>(null);

  const selectedAgent = agentChoice || agents[0]?.agent_id || "";
  const selectedSilo = siloChoice || silos[0] || "";
  const selectedPriority = priorityChoice || priorities[0] || "NORMAL";

  const [batchSiloChoice, setBatchSiloChoice] = useState("");
  const [batchStrategyChoice, setBatchStrategyChoice] = useState("");
  const [batchSymbolsText, setBatchSymbolsText] = useState("");
  const [batchError, setBatchError] = useState<string | null>(null);

  const selectedBatchSilo = batchSiloChoice || silos[0] || "";

  const [followTaskInput, setFollowTaskInput] = useState("");
  const [followTaskTouched, setFollowTaskTouched] = useState(false);
  const [question, setQuestion] = useState("");
  const [followError, setFollowError] = useState<string | null>(null);

  const latestCompleted = tasks.slice().reverse().find((task) => task.status === "COMPLETED");
  const taskIdCandidates: Array<{ id: string; at: number }> = [];
  if (latestCompleted) {
    taskIdCandidates.push({ id: latestCompleted.task_id, at: latestCompleted.completed_at_ns });
  }
  if (assigned.receipt) {
    taskIdCandidates.push({ id: assigned.receipt.task_id, at: assigned.receipt.completed_at_ns });
  }
  if (batch.receipt) {
    taskIdCandidates.push({ id: batch.receipt.task_id, at: batch.receipt.completed_at_ns });
  }
  taskIdCandidates.sort((left, right) => right.at - left.at);
  const suggestedTaskId = taskIdCandidates.length > 0 ? taskIdCandidates[0].id : "";
  const followTaskId = followTaskTouched ? followTaskInput : suggestedTaskId;
  const turns = follow.receipt?.conversation_history ?? [];

  const ranking = rows(batch.receipt?.structured_result?.ranking);
  const batchStructured = record(batch.receipt?.structured_result);
  const batchWatchlistSize = batchStructured.watchlist_size;
  const batchTop = ranking[0] ?? null;
  const assignedResult = record(assigned.receipt?.structured_result);
  const assignedFocus = rows(assignedResult.reports)[0] ?? rows(assignedResult.ranking)[0] ?? null;
  const taskFocus = latestTaskFocus(tasks);

  const focusCandidates: Array<{ row: Record<string, unknown>; at: number; source: string }> = [];
  if (batchTop) {
    focusCandidates.push({
      row: batchTop,
      at: batch.receipt?.created_at_ns ?? 0,
      source: "batch watchlist top row",
    });
  }
  if (assignedFocus) {
    focusCandidates.push({
      row: assignedFocus,
      at: assigned.receipt?.completed_at_ns ?? 0,
      source: "latest assigned task",
    });
  }
  if (taskFocus) focusCandidates.push(taskFocus);
  focusCandidates.sort((left, right) => right.at - left.at);
  const focusEntry = focusCandidates.length > 0 ? focusCandidates[0] : null;
  const focus = focusEntry?.row ?? null;

  const scoreRaw = focus ? Number(focus.signal_score) : Number.NaN;
  const score = Number.isFinite(scoreRaw) ? Math.max(0, Math.min(100, scoreRaw)) : 0;
  const advice = text(focus?.operation_advice, "HOLD").toUpperCase();
  const reasons = strings(focus?.signal_reasons);
  const risks = strings(focus?.risk_factors);
  const cancelledCount = cancelAction.receipt ? int(record(cancelAction.receipt).cancelled) : null;

  const submitAssign = async (): Promise<void> => {
    setAssignError(null);
    const symbols = parseSymbols(symbolsText);
    if (!selectedAgent) {
      setAssignError("Choose an agent before dispatching.");
      return;
    }
    if (!selectedSilo) {
      setAssignError("Choose a silo before dispatching.");
      return;
    }
    if (symbols.length === 0) {
      setAssignError("Enter at least one symbol, comma separated.");
      return;
    }
    const receipt = await assigned.run(() =>
      api.assignAgent({
        agent_id: selectedAgent,
        silo: selectedSilo,
        symbols,
        strategy_id: strategyChoice || null,
        prompt: prompt.trim(),
        priority: selectedPriority,
      }),
    );
    if (receipt) res.reload();
  };

  const submitBatch = async (): Promise<void> => {
    setBatchError(null);
    const symbols = parseSymbols(batchSymbolsText);
    if (!selectedBatchSilo) {
      setBatchError("Choose a silo before scanning.");
      return;
    }
    if (symbols.length === 0) {
      setBatchError("Enter at least one symbol, comma separated.");
      return;
    }
    const receipt = await batch.run(() =>
      api.batchWatchlist({
        silo: selectedBatchSilo,
        symbols,
        strategy_id: batchStrategyChoice || null,
      }),
    );
    if (receipt) res.reload();
  };

  const askFollowup = async (): Promise<void> => {
    setFollowError(null);
    if (!followTaskId) {
      setFollowError("A completed task id is required.");
      return;
    }
    if (!question.trim()) {
      setFollowError("Enter a question for the agent.");
      return;
    }
    const receipt = await follow.run(() =>
      api.agentFollowup({ task_id: followTaskId, question: question.trim() }),
    );
    if (receipt) {
      setQuestion("");
      res.reload();
    }
  };

  const cancelTask = async (taskId: string): Promise<void> => {
    const receipt = await cancelAction.run(() => api.cancelAgentTasks(taskId));
    if (receipt) res.reload();
  };

  const clearQueue = async (): Promise<void> => {
    const receipt = await cancelAction.run(() => api.cancelAgentTasks("ALL"));
    if (receipt) res.reload();
  };

  return (
    <div className="flex flex-col gap-[14px]">
      <SectionHeader
        eyebrow="Ring 2 · agents"
        title="Agent Task Hub"
        subtitle="Six specialist agents, the token budget, grounded task assignment and the 0..100 decision dashboard. Agents reason; they never sign."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <StatusPill
              value={`${int(data?.active_tasks ?? 0)} active`}
              tone={(data?.active_tasks ?? 0) > 0 ? "accent" : "neutral"}
            />
            <StatusPill
              value={`${int(data?.completed_tasks ?? 0)} completed`}
              tone={(data?.completed_tasks ?? 0) > 0 ? "ok" : "neutral"}
            />
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={res.reload}
              aria-label="Reload agent hub telemetry"
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
          {agents.length === 0 ? (
            <GlassPanel eyebrow="Specialists" title="Agent roster">
              <EmptyCleanSlate
                title="No specialist agent is registered"
                body="The Ring 2 dispatcher advertises its catalog at boot. Reload once the process has registered."
              />
            </GlassPanel>
          ) : (
            <div className="grid-auto-300">
              {agents.map((agent) => (
                <AgentCard key={agent.agent_id} agent={agent} />
              ))}
            </div>
          )}

          {token ? <TokenBudgetCard token={token} /> : null}

          <div className="split-2">
            <div className="flex min-w-0 flex-col gap-[14px]">
              <GlassPanel
                eyebrow="Grounded dispatch"
                title="Assign agent task"
                actions={<StatusPill value={`${int(agents.length)} specialists`} tone="accent" />}
              >
                <form
                  className="flex flex-col gap-2.5"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void submitAssign();
                  }}
                >
                  <div className="grid grid-cols-2 gap-2">
                    <label className="field col-span-2">
                      <span className="field-label">Agent</span>
                      <select
                        className="select"
                        value={selectedAgent}
                        onChange={(event) => setAgentChoice(event.target.value)}
                        aria-label="Agent to assign"
                      >
                        {agents.length === 0 ? <option value="">No agent available</option> : null}
                        {agents.map((agent) => (
                          <option key={agent.agent_id} value={agent.agent_id}>
                            {agent.title} · {agent.agent_id}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field">
                      <span className="field-label">Silo</span>
                      <select
                        className="select"
                        value={selectedSilo}
                        onChange={(event) => setSiloChoice(event.target.value)}
                        aria-label="Agent task silo"
                      >
                        {silos.length === 0 ? <option value="">No silo available</option> : null}
                        {silos.map((item) => (
                          <option key={item} value={item}>
                            {item}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field">
                      <span className="field-label">Priority</span>
                      <select
                        className="select"
                        value={selectedPriority}
                        onChange={(event) => setPriorityChoice(event.target.value)}
                        aria-label="Agent task priority"
                      >
                        {priorities.length === 0 ? <option value="NORMAL">NORMAL</option> : null}
                        {priorities.map((item) => (
                          <option key={item} value={item}>
                            {item}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field col-span-2">
                      <span className="field-label">Strategy (optional)</span>
                      <select
                        className="select"
                        value={strategyChoice}
                        onChange={(event) => setStrategyChoice(event.target.value)}
                        aria-label="Strategy template (optional)"
                      >
                        <option value="">No strategy template</option>
                        {strategies.map((strategy) => (
                          <option key={strategy.strategy_id} value={strategy.strategy_id}>
                            {strategy.name} · {strategy.strategy_id}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field col-span-2">
                      <span className="field-label">Symbols</span>
                      <input
                        className="input mono"
                        value={symbolsText}
                        onChange={(event) => setSymbolsText(event.target.value)}
                        placeholder="SPY, QQQ"
                        autoComplete="off"
                        aria-label="Symbols, comma separated"
                      />
                    </label>
                  </div>
                  <label className="field">
                    <span className="field-label">Prompt</span>
                    <textarea
                      className="textarea"
                      value={prompt}
                      onChange={(event) => setPrompt(event.target.value)}
                      placeholder="Ask for a grounded decision, level set or risk read…"
                      aria-label="Agent prompt"
                    />
                  </label>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="submit"
                      className="btn btn-accent btn-sm"
                      disabled={assigned.busy}
                      aria-label="Assign the grounded agent task"
                    >
                      {assigned.busy ? "Dispatching…" : "Assign Task"}
                    </button>
                    <span className="mono text-[9.5px] text-[var(--dim)]">
                      the dispatcher returns a verified report with a grounding digest
                    </span>
                  </div>
                  {assignError ? <InlineAlert message={assignError} /> : null}
                  {assigned.error ? <ErrorBlock message={assigned.error} /> : null}
                </form>

                {assigned.receipt ? (
                  <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] px-3 py-2">
                    <span className="eyebrow text-[var(--accent)]">Task receipt</span>
                    <span className="mono text-[10px] text-[var(--ink)]" title={assigned.receipt.task_id}>
                      {assigned.receipt.task_id}
                    </span>
                    <CopyHash value={assigned.receipt.grounding_sha256} label="copy task grounding digest" />
                    <StatusPill
                      value={assigned.receipt.verified ? "VERIFIED" : "UNVERIFIED"}
                      tone={passTone(assigned.receipt.verified)}
                    />
                    <span className="mono tnum text-[9.5px] text-[var(--dim)]">
                      {latency(assigned.receipt.latency_ms)} · ${num(assigned.receipt.token_cost_usd, 6)}
                    </span>
                  </div>
                ) : null}
              </GlassPanel>

              <GlassPanel
                eyebrow="Batch scan"
                title="Batch watchlist scan"
                actions={
                  batch.receipt ? (
                    <StatusPill
                      value={`${int(batchWatchlistSize)} ranked`}
                      tone="accent"
                    />
                  ) : undefined
                }
              >
                <form
                  className="flex flex-col gap-2.5"
                  onSubmit={(event) => {
                    event.preventDefault();
                    void submitBatch();
                  }}
                >
                  <div className="grid grid-cols-2 gap-2">
                    <label className="field">
                      <span className="field-label">Silo</span>
                      <select
                        className="select"
                        value={selectedBatchSilo}
                        onChange={(event) => setBatchSiloChoice(event.target.value)}
                        aria-label="Batch scan silo"
                      >
                        {silos.length === 0 ? <option value="">No silo available</option> : null}
                        {silos.map((item) => (
                          <option key={item} value={item}>
                            {item}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field">
                      <span className="field-label">Strategy (optional)</span>
                      <select
                        className="select"
                        value={batchStrategyChoice}
                        onChange={(event) => setBatchStrategyChoice(event.target.value)}
                        aria-label="Batch scan strategy (optional)"
                      >
                        <option value="">No strategy template</option>
                        {strategies.map((strategy) => (
                          <option key={strategy.strategy_id} value={strategy.strategy_id}>
                            {strategy.name}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field col-span-2">
                      <span className="field-label">Symbols</span>
                      <input
                        className="input mono"
                        value={batchSymbolsText}
                        onChange={(event) => setBatchSymbolsText(event.target.value)}
                        placeholder="SPY, QQQ, IWM"
                        autoComplete="off"
                        aria-label="Batch scan symbols, comma separated"
                      />
                    </label>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <button
                      type="submit"
                      className="btn btn-sm"
                      disabled={batch.busy}
                      aria-label="Rank the symbol watchlist with the agent"
                    >
                      {batch.busy ? "Ranking…" : "Scan Watchlist"}
                    </button>
                    <span className="mono text-[9.5px] text-[var(--dim)]">
                      one grounded batch packet per scan
                    </span>
                  </div>
                  {batchError ? <InlineAlert message={batchError} /> : null}
                  {batch.error ? <ErrorBlock message={batch.error} /> : null}
                </form>

                {ranking.length === 0 ? (
                  <div className="mt-3">
                    <EmptyCleanSlate
                      title="No ranking yet"
                      body="Enter a watchlist to rank every symbol by signal score, advice, target and stop. The batch seals one grounded packet."
                    />
                  </div>
                ) : (
                  <div className="mt-3 flex flex-col gap-2">
                    {ranking.map((row) => {
                      const symbol = text(row.symbol, "—");
                      const rowAdvice = text(row.operation_advice, "HOLD").toUpperCase();
                      const rowScore = typeof row.signal_score === "number" ? row.signal_score : 0;
                      const riskFactors = strings(row.risk_factors);
                      return (
                        <div
                          key={`${int(row.rank)}-${symbol}`}
                          className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--panel-sunken)] p-2.5"
                        >
                          <div className="row-between gap-2">
                            <div className="row min-w-0 gap-2">
                              <span className="mono tnum text-[10px] text-[var(--dim)]">#{int(row.rank)}</span>
                              <span className="mono text-[12px] font-semibold text-[var(--ink)]">{symbol}</span>
                              <StatusPill value={rowAdvice} tone={adviceTone(rowAdvice)} />
                            </div>
                            <CopyHash value={row.grounding_sha256} label={`copy ${symbol} ranking digest`} />
                          </div>
                          <div className="mt-1.5 flex items-center gap-2">
                            <div className="w-[84px]">
                              <InlineBar
                                value={rowScore}
                                max={100}
                                tone={rowScore >= 64 ? "pos" : rowScore < 46 ? "neg" : "accent"}
                                label={`${symbol} signal score`}
                              />
                            </div>
                            <span className="mono tnum text-[10.5px] text-[var(--ink-soft)]">{num(rowScore, 1)}</span>
                            <span className="mono text-[9px] tracking-[0.08em] text-[var(--dim)] uppercase">
                              signal score
                            </span>
                          </div>
                          <div className="mt-1.5 grid grid-cols-2 gap-1.5 sm:grid-cols-4">
                            <MicroStat
                              label="Close"
                              value={<TabularValue value={row.close_price} kind="price" />}
                            />
                            <MicroStat
                              label="Target"
                              value={<TabularValue value={row.target_price} kind="price" />}
                            />
                            <MicroStat
                              label="Stop"
                              value={<TabularValue value={row.stop_loss} kind="price" />}
                            />
                            <MicroStat
                              label="R:R"
                              value={<TabularValue value={row.reward_risk} digits={2} />}
                            />
                          </div>
                          <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                            <span className="mono text-[9px] tracking-[0.08em] text-[var(--dim)] uppercase">
                              RSI <TabularValue value={row.rsi_14} digits={1} />
                            </span>
                            <span className="text-[var(--faint)]">·</span>
                            <span className="mono text-[9px] tracking-[0.08em] text-[var(--dim)] uppercase">
                              best · {text(row.best_strategy_id, "—")}
                            </span>
                          </div>
                          {riskFactors.length > 0 ? (
                            <div className="chip-row mt-1.5">
                              {riskFactors.map((risk, riskIndex) => (
                                <span key={`${riskIndex}-${risk}`} className="pill pill-warn">
                                  {risk}
                                </span>
                              ))}
                            </div>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                )}
              </GlassPanel>
            </div>

            <div className="flex min-w-0 flex-col gap-[14px]">
              <GlassPanel
                eyebrow={focus ? `Grounded result · ${text(focus.symbol, "focus")}` : "Grounded result"}
                title="Decision dashboard"
                actions={
                  focus ? (
                    <div className="flex flex-wrap items-center gap-1.5">
                      <StatusPill value={focusEntry?.source ?? "grounded"} tone="neutral" />
                      <StatusPill
                        value={focus.verified === true ? "VERIFIED" : "UNVERIFIED"}
                        tone={passTone(focus.verified === true)}
                      />
                    </div>
                  ) : undefined
                }
              >
                {focus ? (
                  <div className="flex flex-col gap-2.5">
                    <div className="flex items-start gap-3">
                      <RadialGauge
                        value={score}
                        max={100}
                        label="signal score"
                        sublabel={text(focus.symbol, "")}
                        tone={score >= 64 ? "pos" : score < 46 ? "neg" : "accent"}
                        size={126}
                      />
                      <div className="stack-6 min-w-0 flex-1">
                        <div className="row-between gap-2">
                          <StatusPill value={advice} tone={adviceTone(advice)} />
                          <span className="mono tnum text-[10px] text-[var(--dim)]">
                            R:R {num(focus.reward_risk, 2)}
                          </span>
                        </div>
                        <div className="grid grid-cols-3 gap-1.5">
                          <MicroStat label="MA5" value={<TabularValue value={focus.ma5} kind="price" />} />
                          <MicroStat label="MA10" value={<TabularValue value={focus.ma10} kind="price" />} />
                          <MicroStat label="MA20" value={<TabularValue value={focus.ma20} kind="price" />} />
                        </div>
                        <div className="grid grid-cols-2 gap-1.5">
                          <MicroStat
                            label="MACD"
                            value={<TabularValue value={focus.macd_value} digits={4} signTone />}
                            hint={text(focus.macd_status, "—")}
                          />
                          <MicroStat
                            label="RSI 14"
                            value={<TabularValue value={focus.rsi_14} digits={1} />}
                            hint={text(focus.rsi_status, "—")}
                          />
                        </div>
                      </div>
                    </div>

                    <div className="grid grid-cols-3 gap-1.5">
                      <MicroStat label="ATR 14" value={<TabularValue value={focus.atr_14} kind="price" />} />
                      <MicroStat
                        label="Target"
                        value={<TabularValue value={focus.target_price} kind="price" />}
                        hint="ATR-derived"
                      />
                      <MicroStat
                        label="Stop"
                        value={<TabularValue value={focus.stop_loss} kind="price" />}
                        hint="ATR-derived"
                      />
                    </div>

                    <p className="mono text-[9.5px] text-[var(--dim)]">
                      close <TabularValue value={focus.close_price} kind="price" /> · best strategy{" "}
                      {text(focus.best_strategy_id, "—")}
                    </p>

                    <div className="grid gap-2 sm:grid-cols-2">
                      <ReasonList title="Reasons" items={reasons} tone="pos" />
                      <ReasonList title="Risks" items={risks} tone="neg" />
                    </div>
                  </div>
                ) : (
                  <EmptyCleanSlate
                    title="No decision dashboard yet"
                    body="Assign the daily decision agent or run a batch watchlist scan; the 0..100 dashboard renders only from a grounded result."
                  />
                )}

                <div className="mt-3 border-t border-[var(--line)] pt-3">
                  <div className="row-between">
                    <p className="eyebrow">Grounded follow-up</p>
                    <StatusPill
                      value={turns.length > 0 ? `${int(turns.length)} turns` : "no thread"}
                      tone={turns.length > 0 ? "accent" : "neutral"}
                    />
                  </div>
                  <div className="mt-1.5 grid gap-1.5 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
                    <input
                      className="input mono text-[10.5px]"
                      value={followTaskId}
                      onChange={(event) => {
                        setFollowTaskTouched(true);
                        setFollowTaskInput(event.target.value);
                      }}
                      placeholder="task id"
                      autoComplete="off"
                      aria-label="Follow-up task id"
                    />
                    <input
                      className="input text-[11px]"
                      value={question}
                      onChange={(event) => setQuestion(event.target.value)}
                      placeholder="Why is the stop below the 20-day?"
                      autoComplete="off"
                      aria-label="Follow-up question"
                    />
                  </div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-2">
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={() => void askFollowup()}
                      disabled={follow.busy}
                      aria-label="Ask the agent a grounded follow-up question"
                    >
                      {follow.busy ? "Asking…" : "Ask Follow-up"}
                    </button>
                    <span className="mono text-[9px] text-[var(--dim)]">
                      answers are re-checked against the task fact packet
                    </span>
                  </div>
                  {followError ? (
                    <div className="mt-1.5">
                      <InlineAlert message={followError} />
                    </div>
                  ) : null}
                  {follow.error ? (
                    <div className="mt-1.5">
                      <ErrorBlock message={follow.error} />
                    </div>
                  ) : null}

                  {follow.receipt ? (
                    <div className="mt-2 flex flex-col gap-2">
                      <div className="rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] p-2.5">
                        <div className="row-between gap-2">
                          <p className="eyebrow">Answer</p>
                          <div className="flex flex-wrap items-center gap-1.5">
                            <StatusPill
                              value={follow.receipt.verified ? "VERIFIED" : "UNVERIFIED"}
                              tone={passTone(follow.receipt.verified)}
                            />
                            <CopyHash
                              value={follow.receipt.grounding_sha256}
                              label="copy follow-up grounding digest"
                            />
                          </div>
                        </div>
                        <p className="mt-1 text-[11.5px] leading-relaxed text-[var(--ink-soft)]">
                          {follow.receipt.answer}
                        </p>
                      </div>
                      {turns.length > 0 ? (
                        <div className="scroll-y flex max-h-[220px] flex-col gap-1.5">
                          {turns.map((turn, index) => (
                            <div
                              key={`${turn.at_ns}-${index}`}
                              className="rounded-[var(--r-xs)] border border-[var(--line)] px-2 py-1.5"
                            >
                              <div className="row-between">
                                <span className="mono text-[9px] tracking-[0.1em] text-[var(--accent)] uppercase">
                                  {text(turn.role, "turn")}
                                </span>
                                <span className="mono tnum text-[9px] text-[var(--dim)]">{clock(turn.at_ns)} UTC</span>
                              </div>
                              <p className="mt-0.5 text-[10.5px] leading-snug text-[var(--ink-soft)]">{turn.text}</p>
                            </div>
                          ))}
                        </div>
                      ) : null}
                    </div>
                  ) : null}
                </div>
              </GlassPanel>

              <GlassPanel
                eyebrow="Queue"
                title="Task Queue"
                actions={
                  <div className="flex flex-wrap items-center gap-1.5">
                    {cancelledCount !== null ? (
                      <StatusPill value={`${cancelledCount} cancelled`} tone="warn" />
                    ) : null}
                    <button
                      type="button"
                      className="btn btn-sm btn-ghost"
                      onClick={() => void clearQueue()}
                      disabled={cancelAction.busy || tasks.length === 0}
                      aria-label="Clear the agent task queue"
                    >
                      Clear queue
                    </button>
                  </div>
                }
              >
                <div className="row-between flex-wrap gap-2">
                  <p className="mono tnum text-[10px] text-[var(--ink-soft)]">
                    {int(data.active_tasks)} active · {int(data.completed_tasks)} completed ·{" "}
                    {int(data.failed_tasks)} failed · {int(data.cancelled_tasks)} cancelled
                  </p>
                  <span className="mono text-[9px] tracking-[0.08em] text-[var(--dim)] uppercase">
                    {int(tasks.length)} tracked
                  </span>
                </div>
                {cancelAction.error ? (
                  <div className="mt-2">
                    <ErrorBlock message={cancelAction.error} />
                  </div>
                ) : null}
                {tasks.length === 0 ? (
                  <EmptyCleanSlate
                    title="No agent task has been dispatched"
                    body="Assign a specialist above or run a batch watchlist scan. Every task keeps its grounding digest, latency and token cost."
                  />
                ) : (
                  <div className="scroll-x mt-2">
                    <table className="table table-compact" aria-label="Agent task queue">
                      <thead>
                        <tr>
                          <th scope="col">Task</th>
                          <th scope="col">Agent</th>
                          <th scope="col">Status</th>
                          <th scope="col">Progress</th>
                          <th scope="col" className="num">
                            Token cost
                          </th>
                          <th scope="col" className="num">
                            Latency
                          </th>
                          <th scope="col">Created</th>
                          <th scope="col">
                            <span className="sr-only">Actions</span>
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {tasks
                          .slice()
                          .reverse()
                          .map((task) => (
                            <tr key={task.task_id}>
                              <td>
                                <span className="mono text-[10px] text-[var(--ink)]" title={task.task_id}>
                                  {task.task_id}
                                </span>
                              </td>
                              <td>
                                <span className="mono text-[10px] text-[var(--ink-soft)]" title={task.agent_id}>
                                  {shortId(task.agent_id, 20)}
                                </span>
                              </td>
                              <td>
                                <StatusPill value={task.status} tone={taskTone(task.status)} />
                              </td>
                              <td>
                                <div className="flex items-center gap-1.5">
                                  <div className="w-[56px]">
                                    <InlineBar
                                      value={task.progress_pct}
                                      label={`${task.task_id} progress`}
                                      tone={task.status === "FAILED" ? "neg" : "accent"}
                                    />
                                  </div>
                                  <span className="mono tnum text-[10px] text-[var(--dim)]">
                                    {int(task.progress_pct)}%
                                  </span>
                                </div>
                              </td>
                              <td className="num">
                                <TabularValue value={task.token_cost_usd} kind="usd" digits={6} />
                              </td>
                              <td className="num">
                                <span className="mono tnum">{latency(task.latency_ms)}</span>
                              </td>
                              <td className="num">
                                <span className="mono tnum text-[10px]" title={utc(task.created_at_ns)}>
                                  {ago(task.created_at_ns, now)}
                                </span>
                              </td>
                              <td>
                                {task.status === "PENDING" || task.status === "PROCESSING" ? (
                                  <button
                                    type="button"
                                    className="btn btn-sm btn-ghost"
                                    onClick={() => void cancelTask(task.task_id)}
                                    disabled={cancelAction.busy}
                                    aria-label={`Cancel ${task.task_id}`}
                                  >
                                    Cancel
                                  </button>
                                ) : (
                                  <span className="text-[10px] text-[var(--dim)]">—</span>
                                )}
                              </td>
                            </tr>
                          ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </GlassPanel>
            </div>
          </div>
        </>
      ) : null}
    </div>
  );
}
