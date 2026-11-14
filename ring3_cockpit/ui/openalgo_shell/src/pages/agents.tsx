import { Bot, ClipboardList, Cpu, Gauge, MessageSquare, Search, Trash2, Waves } from 'lucide-react';
import { useState } from 'react';
import { ActionFeedback, ActionPanel, ProofRow } from '@/components/operator-action';
import { DataState, PageShell, StatTile } from '@/components/page-shell';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import { useAction } from '@/hooks/use-action';
import { usePageData } from '@/hooks/use-page-data';
import { fetchAgentsPage, postAgentBatchWatchlist, postAgentFollowup, postAgentTask, postCancelAgentTasks } from '@/lib/api';
import { cell, num, rowKey } from '@/lib/rows';
import type {
  AgentAssignReceipt,
  AgentBatchWatchlistReceipt,
  AgentCancelReceipt,
  AgentFollowupReceipt,
  AgentsPageData,
} from '@/types';

const SILO_IDS = ['CRYPTO_BINANCE', 'INDIA_ZERODHA_DHAN', 'US_IBKR', 'FX_MT5'] as const;
const PRIORITIES = ['LOW', 'NORMAL', 'HIGH', 'CRITICAL'] as const;

const AGENT_IDS = [
  'daily_decision_agent',
  'technical_strategy_agent',
  'fundamental_valuation_agent',
  'contagion_stress_agent',
  'alpha_forge_agent',
  'backtest_audit_agent',
] as const;

const STATUS_ACCENT: Record<string, string> = {
  COMPLETED: 'border-emerald-500/40 text-emerald-300',
  PROCESSING: 'border-sky-500/40 text-sky-300',
  PENDING: 'border-slate-500/40 text-slate-300',
  FAILED: 'border-rose-500/40 text-rose-300',
  CANCELLED: 'border-amber-500/40 text-amber-300',
};

const ADVICE_ACCENT: Record<string, string> = {
  STRONG_BUY: 'border-emerald-500/40 text-emerald-300',
  BUY: 'border-teal-500/40 text-teal-300',
  HOLD: 'border-slate-500/40 text-slate-300',
  WAIT: 'border-amber-500/40 text-amber-300',
  AVOID: 'border-rose-500/40 text-rose-300',
};

function fmt(value: unknown, digits = 2): string {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '—';
}

function asRows(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (entry): entry is Record<string, unknown> => typeof entry === 'object' && entry !== null,
  );
}

function asRow(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null) return null;
  return value as Record<string, unknown>;
}

function asStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((entry) => String(entry));
}

export function AgentsPage() {
  const page = usePageData(fetchAgentsPage);
  const data: AgentsPageData | null = page.data;

  const [agentId, setAgentId] = useState<string>('daily_decision_agent');
  const [silo, setSilo] = useState<string>('INDIA_ZERODHA_DHAN');
  const [symbols, setSymbols] = useState<string>('RELIANCE, INFY');
  const [strategyId, setStrategyId] = useState<string>('ma_golden_cross');
  const [priority, setPriority] = useState<string>('NORMAL');
  const [prompt, setPrompt] = useState<string>(
    'Summarise the decision dashboard and flag the single largest risk.',
  );
  const [receipt, setReceipt] = useState<AgentAssignReceipt | null>(null);
  const [batchSymbols, setBatchSymbols] = useState<string>('SPY, QQQ, IWM');
  const [batchStrategy, setBatchStrategy] = useState<string>('ma_golden_cross');
  const [batchReceipt, setBatchReceipt] = useState<AgentBatchWatchlistReceipt | null>(null);
  const [followupTaskId, setFollowupTaskId] = useState<string>('');
  const [followupQuestion, setFollowupQuestion] = useState<string>(
    'What are the target and stop levels, and the main risk?',
  );
  const [followupReceipt, setFollowupReceipt] = useState<AgentFollowupReceipt | null>(null);

  const assign = useAction<AgentAssignReceipt>();
  const cancel = useAction<AgentCancelReceipt>();
  const batch = useAction<AgentBatchWatchlistReceipt>();
  const followup = useAction<AgentFollowupReceipt>();

  const agents = data?.agents ?? [];
  const strategies = data?.strategies ?? [];
  const tasks = data?.tasks ?? [];
  const selectedAgent = agents.find((entry) => entry.agent_id === agentId) ?? null;
  const requiresStrategy = selectedAgent?.requires_strategy ?? false;

  const report = asRow(asRows(receipt?.structured_result?.reports)[0]);
  const signalScore = typeof report?.signal_score === 'number' ? report.signal_score : null;
  const advice = typeof report?.operation_advice === 'string' ? report.operation_advice : null;
  const gaugePct = signalScore === null ? 0 : Math.max(0, Math.min(100, signalScore));
  const batchRows = asRows(batchReceipt?.structured_result?.ranking ?? []);
  const conversation = followupReceipt?.conversation_history ?? [];

  async function submitTask() {
    const parsed = symbols
      .split(/[,\s;|]+/)
      .map((entry) => entry.trim().toUpperCase())
      .filter((entry) => entry.length > 0);
    const result = await assign.run(() =>
      postAgentTask({
        agent_id: agentId,
        silo,
        symbols: parsed,
        strategy_id: requiresStrategy ? strategyId : null,
        prompt,
        priority,
      }),
    );
    if (result) {
      setReceipt(result);
      page.reload();
    }
  }

  async function cancelTasks() {
    const result = await cancel.run(() => postCancelAgentTasks('ALL'));
    if (result) {
      setReceipt(null);
      page.reload();
    }
  }

  async function submitBatch() {
    const parsed = batchSymbols
      .split(/[,\s;|]+/)
      .map((entry) => entry.trim().toUpperCase())
      .filter((entry) => entry.length > 0);
    if (parsed.length === 0) return;
    const result = await batch.run(() =>
      postAgentBatchWatchlist({
        silo,
        symbols: parsed,
        strategy_id: batchStrategy.trim() || null,
      }),
    );
    if (result) {
      setBatchReceipt(result);
      setFollowupTaskId(result.task_id);
      setFollowupReceipt(null);
      page.reload();
    }
  }

  async function askFollowup() {
    const taskId = followupTaskId.trim();
    if (taskId.length === 0 || followupQuestion.trim().length === 0) return;
    const result = await followup.run(() =>
      postAgentFollowup({ task_id: taskId, question: followupQuestion }),
    );
    if (result) {
      setFollowupReceipt(result);
    }
  }

  return (
    <PageShell
      title="Agent Task Hub"
      ring="RING 2"
      capability="PAPER CONTROL"
      subtitle="Assign structured research work to the six specialist Ring 2 agents, read the grounded decision dashboard, and watch the token-budget ledger. Agents emit research only — no agent can sign an order or read a Ring 0 broker key."
      actions={
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="font-mono text-[10px]">
            RING 2 ONLY
          </Badge>
          <Button
            type="button"
            size="sm"
            variant="outline"
            disabled={cancel.pending || tasks.length === 0}
            onClick={cancelTasks}
          >
            <Trash2 className="mr-1 size-3.5" />
            Clear Tasks
          </Button>
        </div>
      }
    >
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Specialist Agents"
          value={String(data?.agent_count ?? 0)}
          hint="Ring 2 research lanes"
          icon={Bot}
        />
        <StatTile
          label="Strategy Templates"
          value={String(data?.strategy_count ?? 0)}
          hint="Harvested donor catalog"
          icon={Cpu}
        />
        <StatTile
          label="Tasks"
          value={String(data?.task_count ?? 0)}
          hint={`Completed ${data?.completed_tasks ?? 0} · Failed ${data?.failed_tasks ?? 0}`}
          icon={ClipboardList}
        />
        <StatTile
          label="Token Spend"
          value={`$${fmt(data?.token.spend_usd)}`}
          hint={`Cap $${fmt(data?.token.cap_usd)} · ${fmt(data?.token.utilization_pct)}% used`}
          icon={Waves}
        />
      </div>

      <ActionPanel
        title="Run Watchlist Batch Scan"
        description="Runs the harvested decision dashboard across the whole watchlist, ranks every symbol by signal score and operation advice, and seals the batch ranking into one SHA-256 grounded packet."
      >
        <div className="grid gap-3 md:grid-cols-[minmax(0,18rem)_minmax(0,14rem)_minmax(0,1fr)_auto]">
          <div className="grid gap-1.5">
            <Label htmlFor="batch-silo">Silo</Label>
            <Select value={silo} onValueChange={setSilo}>
              <SelectTrigger id="batch-silo" className="h-8 font-mono text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SILO_IDS.map((entry) => (
                  <SelectItem key={entry} value={entry} className="font-mono text-xs">
                    {entry}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="batch-strategy">Strategy Focus</Label>
            <Select value={batchStrategy} onValueChange={setBatchStrategy}>
              <SelectTrigger id="batch-strategy" className="h-8 font-mono text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(strategies.length > 0
                  ? strategies.map((entry) => entry.strategy_id)
                  : [...Array(0)].map(() => '')
                ).map((entry) => (
                  <SelectItem key={entry} value={entry} className="font-mono text-xs">
                    {entry}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="batch-symbols">Comma-separated symbols</Label>
            <Input
              id="batch-symbols"
              className="h-8 font-mono text-xs"
              value={batchSymbols}
              onChange={(event) => setBatchSymbols(event.target.value)}
              placeholder="SPY, QQQ, IWM"
            />
          </div>
          <div className="flex items-end">
            <Button type="button" size="sm" disabled={batch.pending} onClick={submitBatch}>
              <Search className="mr-1 size-3.5" />
              {batch.pending ? 'Scanning…' : 'Run Watchlist Batch Scan'}
            </Button>
          </div>
        </div>
        <ActionFeedback pending={batch.pending} error={batch.error} />
        {batchReceipt ? (
          <div className="grid gap-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge
                variant="outline"
                className={`font-mono text-[10px] ${
                  batchReceipt.verified
                    ? 'border-emerald-500/40 text-emerald-300'
                    : 'border-rose-500/40 text-rose-300'
                }`}
              >
                {batchReceipt.verified ? 'BATCH GROUNDED' : 'BATCH UNGROUNDED'}
              </Badge>
              <span className="text-[11px] text-muted-foreground">
                {cell(batchReceipt, 'task_id')} · {batchRows.length} symbols ranked · top{' '}
                {String(batchReceipt.structured_result?.top_symbol ?? '—')}
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[46rem] border-collapse text-left font-mono text-[11px]">
                <thead>
                  <tr className="border-b border-border/60 text-muted-foreground">
                    <th className="py-1.5 pr-3 text-right font-normal">rank</th>
                    <th className="py-1.5 pr-3 font-normal">symbol</th>
                    <th className="py-1.5 pr-3 text-right font-normal">signal_score</th>
                    <th className="py-1.5 pr-3 font-normal">operation_advice</th>
                    <th className="py-1.5 pr-3 text-right font-normal">close</th>
                    <th className="py-1.5 pr-3 text-right font-normal">target</th>
                    <th className="py-1.5 pr-3 text-right font-normal">stop</th>
                    <th className="py-1.5 pr-3 text-right font-normal">reward_risk</th>
                    <th className="py-1.5 font-normal">grounding</th>
                  </tr>
                </thead>
                <tbody>
                  {batchRows.length === 0 ? (
                    <tr>
                      <td className="py-3 text-muted-foreground" colSpan={9}>
                        No batch ranking returned.
                      </td>
                    </tr>
                  ) : (
                    batchRows.map((row, index) => (
                      <tr key={rowKey(row, index)} className="border-b border-border/30">
                        <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'rank'), 0)}</td>
                        <td className="py-1.5 pr-3">{cell(row, 'symbol')}</td>
                        <td className="py-1.5 pr-3 text-right">
                          {fmt(num(row, 'signal_score'))}
                        </td>
                        <td className="py-1.5 pr-3">
                          <Badge
                            variant="outline"
                            className={`font-mono text-[10px] ${
                              ADVICE_ACCENT[cell(row, 'operation_advice')] ??
                              'border-border text-muted-foreground'
                            }`}
                          >
                            {cell(row, 'operation_advice')}
                          </Badge>
                        </td>
                        <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'close_price'))}</td>
                        <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'target_price'))}</td>
                        <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'stop_loss'))}</td>
                        <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'reward_risk'))}</td>
                        <td className="py-1.5">
                          {cell(row, 'grounding_sha256').slice(0, 12) || '—'}
                        </td>
                      </tr>
                    ))
                  )}
                </tbody>
              </table>
            </div>
            <ProofRow
              label="batch_grounding_sha256"
              value={batchReceipt.grounding_sha256 || '—'}
            />
          </div>
        ) : null}
      </ActionPanel>

      <DataState loading={page.loading} error={page.error}>
        <div className="grid gap-4 xl:grid-cols-[minmax(0,26rem)_minmax(0,1fr)]">
          <ActionPanel
            title="Assign Agent Task"
            description="Every task is deterministic, grounded, and budget-checked before it runs."
          >
            <div className="grid gap-3">
              <div className="grid gap-1.5">
                <Label htmlFor="agent-id">Specialist Agent</Label>
                <Select value={agentId} onValueChange={setAgentId}>
                  <SelectTrigger id="agent-id" className="h-8 font-mono text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {AGENT_IDS.map((entry) => (
                      <SelectItem key={entry} value={entry} className="font-mono text-xs">
                        {entry}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-[11px] leading-snug text-muted-foreground">
                  {selectedAgent?.description ?? 'Select an agent.'}
                </p>
              </div>

              <div className="grid gap-3 sm:grid-cols-2">
                <div className="grid gap-1.5">
                  <Label htmlFor="agent-silo">Silo</Label>
                  <Select value={silo} onValueChange={setSilo}>
                    <SelectTrigger id="agent-silo" className="h-8 font-mono text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {SILO_IDS.map((entry) => (
                        <SelectItem key={entry} value={entry} className="font-mono text-xs">
                          {entry}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div className="grid gap-1.5">
                  <Label htmlFor="agent-priority">Priority</Label>
                  <Select value={priority} onValueChange={setPriority}>
                    <SelectTrigger id="agent-priority" className="h-8 font-mono text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {PRIORITIES.map((entry) => (
                        <SelectItem key={entry} value={entry} className="font-mono text-xs">
                          {entry}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>

              <div className="grid gap-1.5">
                <Label htmlFor="agent-symbols">Symbols / Watchlist</Label>
                <Input
                  id="agent-symbols"
                  className="h-8 font-mono text-xs"
                  value={symbols}
                  onChange={(event) => setSymbols(event.target.value)}
                  placeholder="RELIANCE, INFY, TCS"
                />
              </div>

              <div className="grid gap-1.5">
                <Label htmlFor="agent-strategy">Strategy Template</Label>
                <Select value={strategyId} onValueChange={setStrategyId}>
                  <SelectTrigger id="agent-strategy" className="h-8 font-mono text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {(strategies.length > 0
                      ? strategies.map((entry) => entry.strategy_id)
                      : [...Array(0)].map(() => '')
                    ).map((entry) => (
                      <SelectItem key={entry} value={entry} className="font-mono text-xs">
                        {entry}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-[11px] leading-snug text-muted-foreground">
                  {requiresStrategy
                    ? 'This agent scores a single harvested strategy.'
                    : 'Optional — this agent ignores the template selector.'}
                </p>
              </div>

              <div className="grid gap-1.5">
                <Label htmlFor="agent-prompt">Custom Prompt / Question</Label>
                <Textarea
                  id="agent-prompt"
                  className="min-h-20 font-mono text-xs"
                  value={prompt}
                  onChange={(event) => setPrompt(event.target.value)}
                />
              </div>

              <Button
                type="button"
                size="sm"
                disabled={assign.pending || symbols.trim().length === 0}
                onClick={submitTask}
              >
                <Gauge className="mr-1 size-3.5" />
                {assign.pending ? 'Dispatching…' : 'Assign Agent Task'}
              </Button>

              <ActionFeedback pending={assign.pending} error={assign.error} />
              {receipt ? (
                <div className="grid gap-1 rounded-md border border-border/60 p-2">
                  <ProofRow label="task_id" value={receipt.task_id} />
                  <ProofRow label="status" value={receipt.status} />
                  <ProofRow label="latency_ms" value={fmt(receipt.latency_ms, 2)} />
                  <ProofRow label="token_cost_usd" value={`$${fmt(receipt.token_cost_usd, 6)}`} />
                  <ProofRow label="grounding_sha256" value={receipt.grounding_sha256 || '—'} />
                </div>
              ) : null}
            </div>
          </ActionPanel>

          <div className="grid gap-4">
            <Card>
              <CardHeader className="pb-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <CardTitle className="text-sm">Decision Dashboard</CardTitle>
                  <div className="flex items-center gap-1.5">
                    {advice ? (
                      <Badge
                        variant="outline"
                        className={`font-mono text-[10px] ${
                          ADVICE_ACCENT[advice] ?? 'border-border text-muted-foreground'
                        }`}
                      >
                        {advice}
                      </Badge>
                    ) : null}
                    {receipt?.verified ? (
                      <Badge
                        variant="outline"
                        className="border-emerald-500/40 font-mono text-[10px] text-emerald-300"
                      >
                        GROUNDED
                      </Badge>
                    ) : null}
                  </div>
                </div>
              </CardHeader>
              <CardContent className="grid gap-3">
                {report === null ? (
                  <p className="text-xs text-muted-foreground">
                    Assign the daily decision agent to render a grounded dashboard card.
                  </p>
                ) : (
                  <>
                    <div className="grid gap-1">
                      <div className="flex items-baseline justify-between">
                        <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
                          Signal Score
                        </span>
                        <span className="font-mono text-lg">{fmt(signalScore, 2)}</span>
                      </div>
                      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                        <div
                          className="h-full rounded-full bg-gradient-to-r from-sky-500 to-emerald-400"
                          style={{ width: `${gaugePct}%` }}
                        />
                      </div>
                    </div>

                    <div className="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
                      <ProofRow label="ma5" value={fmt(report.ma5)} />
                      <ProofRow label="ma10" value={fmt(report.ma10)} />
                      <ProofRow label="ma20" value={fmt(report.ma20)} />
                      <ProofRow label="bias_ma5_pct" value={fmt(report.bias_ma5_pct)} />
                      <ProofRow label="macd_status" value={cell(report, 'macd_status')} />
                      <ProofRow label="rsi_14" value={fmt(report.rsi_14)} />
                      <ProofRow label="target_price" value={fmt(report.target_price)} />
                      <ProofRow label="stop_loss" value={fmt(report.stop_loss)} />
                    </div>

                    <div className="grid gap-2 sm:grid-cols-3">
                      {[
                        { key: 'signal_reasons', title: 'Signal Reasons' },
                        { key: 'risk_factors', title: 'Risk Factors' },
                        { key: 'catalysts', title: 'Catalysts' },
                      ].map((group) => (
                        <div key={group.key} className="rounded-md border border-border/60 p-2">
                          <p className="mb-1 text-[10px] uppercase tracking-wide text-muted-foreground">
                            {group.title}
                          </p>
                          <ul className="grid gap-1">
                            {asStrings(report[group.key]).length === 0 ? (
                              <li className="text-[11px] text-muted-foreground">None recorded.</li>
                            ) : (
                              asStrings(report[group.key]).map((entry) => (
                                <li key={entry} className="text-[11px] leading-snug">
                                  {entry}
                                </li>
                              ))
                            )}
                          </ul>
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardHeader className="pb-2">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <CardTitle className="text-sm">Grounded Markdown Report</CardTitle>
                  <Badge variant="outline" className="font-mono text-[10px]">
                    {receipt?.verification ? 'SHA-256 VERIFIED' : 'AWAITING TASK'}
                  </Badge>
                </div>
              </CardHeader>
              <CardContent>
                <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md border border-border/60 bg-muted/30 p-3 font-mono text-[11px] leading-relaxed">
                  {receipt?.markdown_report || 'No report generated yet.'}
                </pre>
              </CardContent>
            </Card>
          </div>
        </div>

        <Card>
          <CardHeader className="pb-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="text-sm">Task Queue</CardTitle>
              <div className="flex items-center gap-1.5">
                {['PENDING', 'PROCESSING', 'COMPLETED', 'FAILED', 'CANCELLED'].map((status) => (
                  <Badge
                    key={status}
                    variant="outline"
                    className={`font-mono text-[10px] ${
                      STATUS_ACCENT[status] ?? 'border-border text-muted-foreground'
                    }`}
                  >
                    {status} {data?.counts?.[status] ?? 0}
                  </Badge>
                ))}
              </div>
            </div>
          </CardHeader>
          <CardContent className="overflow-x-auto">
            <table className="w-full min-w-[52rem] border-collapse text-left font-mono text-[11px]">
              <thead>
                <tr className="border-b border-border/60 text-muted-foreground">
                  <th className="py-1.5 pr-3 font-normal">task_id</th>
                  <th className="py-1.5 pr-3 font-normal">agent_id</th>
                  <th className="py-1.5 pr-3 font-normal">silo</th>
                  <th className="py-1.5 pr-3 font-normal">symbols</th>
                  <th className="py-1.5 pr-3 font-normal">priority</th>
                  <th className="py-1.5 pr-3 font-normal">status</th>
                  <th className="py-1.5 pr-3 text-right font-normal">latency_ms</th>
                  <th className="py-1.5 pr-3 text-right font-normal">token_usd</th>
                  <th className="py-1.5 pr-3 font-normal">follow-up</th>
                  <th className="py-1.5 font-normal">grounding</th>
                </tr>
              </thead>
              <tbody>
                {tasks.length === 0 ? (
                  <tr>
                    <td className="py-3 text-muted-foreground" colSpan={10}>
                      Clean slate — no agent tasks have been assigned in this workspace.
                    </td>
                  </tr>
                ) : (
                  tasks.map((row, index) => (
                    <tr key={rowKey(row, index)} className="border-b border-border/30">
                      <td className="py-1.5 pr-3">{cell(row, 'task_id')}</td>
                      <td className="py-1.5 pr-3">{cell(row, 'agent_id')}</td>
                      <td className="py-1.5 pr-3">{cell(row, 'silo')}</td>
                      <td className="py-1.5 pr-3">{cell(row, 'symbols')}</td>
                      <td className="py-1.5 pr-3">{cell(row, 'priority')}</td>
                      <td className="py-1.5 pr-3">
                        <span
                          className={
                            row.status === 'COMPLETED'
                              ? 'text-emerald-300'
                              : row.status === 'FAILED'
                                ? 'text-rose-300'
                                : row.status === 'CANCELLED'
                                  ? 'text-amber-300'
                                  : 'text-sky-300'
                          }
                        >
                          {cell(row, 'status')}
                        </span>
                      </td>
                      <td className="py-1.5 pr-3 text-right">{fmt(num(row, 'latency_ms'))}</td>
                      <td className="py-1.5 pr-3 text-right">
                        {fmt(num(row, 'token_cost_usd'), 6)}
                      </td>
                      <td className="py-1.5 pr-3">
                        <Button
                          type="button"
                          size="sm"
                          variant="outline"
                          className="h-6 px-2 text-[10px]"
                          disabled={row.status !== 'COMPLETED'}
                          onClick={() => {
                            setFollowupTaskId(String(row.task_id));
                            setFollowupReceipt(null);
                          }}
                        >
                          <MessageSquare className="mr-1 size-3" />
                          Ask
                        </Button>
                      </td>
                      <td className="py-1.5">
                        {cell(row, 'grounding_sha256').slice(0, 12) || '—'}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
            <ActionFeedback pending={cancel.pending} error={cancel.error} />
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <CardTitle className="text-sm">Ask Follow-Up Question</CardTitle>
              <Badge
                variant="outline"
                className={`font-mono text-[10px] ${
                  followupReceipt?.verified
                    ? 'border-emerald-500/40 text-emerald-300'
                    : 'border-border text-muted-foreground'
                }`}
              >
                {followupReceipt?.verified ? 'GROUNDED Q&A' : 'AWAITING QUESTION'}
              </Badge>
            </div>
          </CardHeader>
          <CardContent className="grid gap-3">
            <div className="grid gap-3 md:grid-cols-[minmax(0,16rem)_minmax(0,1fr)_auto]">
              <div className="grid gap-1.5">
                <Label htmlFor="followup-task">Completed task_id</Label>
                <Input
                  id="followup-task"
                  className="h-8 font-mono text-xs"
                  value={followupTaskId}
                  onChange={(event) => setFollowupTaskId(event.target.value)}
                  placeholder="task_…"
                />
              </div>
              <div className="grid gap-1.5">
                <Label htmlFor="followup-question">Question</Label>
                <Input
                  id="followup-question"
                  className="h-8 font-mono text-xs"
                  value={followupQuestion}
                  onChange={(event) => setFollowupQuestion(event.target.value)}
                  placeholder="What is the target and stop, and the main risk?"
                />
              </div>
              <div className="flex items-end">
                <Button
                  type="button"
                  size="sm"
                  disabled={
                    followup.pending ||
                    followupTaskId.trim().length === 0 ||
                    followupQuestion.trim().length === 0
                  }
                  onClick={askFollowup}
                >
                  <MessageSquare className="mr-1 size-3.5" />
                  {followup.pending ? 'Answering…' : 'Ask Follow-Up'}
                </Button>
              </div>
            </div>
            <p className="text-[11px] leading-snug text-muted-foreground">
              The answer may only cite indicators, entry/target/stop levels and risk factors
              already committed in that task's GroundedFactPacket.
            </p>
            <ActionFeedback pending={followup.pending} error={followup.error} />
            {conversation.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                Pick a completed task with the Ask button, or paste its task_id, then ask a
                question. Every answer is re-verified before it is returned.
              </p>
            ) : (
              <div className="grid gap-2">
                {conversation.map((turn, index) => (
                  <div
                    key={rowKey(turn, index)}
                    className={`rounded-md border p-2 ${
                      turn.role === 'operator'
                        ? 'border-sky-500/30 bg-sky-500/5'
                        : 'border-border/60 bg-muted/20'
                    }`}
                  >
                    <div className="mb-1 flex flex-wrap items-center gap-2">
                      <Badge variant="outline" className="font-mono text-[10px]">
                        {turn.role}
                      </Badge>
                      {turn.grounding_sha256 ? (
                        <span className="font-mono text-[10px] text-muted-foreground">
                          {turn.grounding_sha256.slice(0, 12)}
                        </span>
                      ) : null}
                    </div>
                    <pre className="whitespace-pre-wrap font-mono text-[11px] leading-relaxed">
                      {turn.text}
                    </pre>
                  </div>
                ))}
              </div>
            )}
            {followupReceipt ? (
              <div className="grid gap-1 rounded-md border border-border/60 p-2">
                <ProofRow label="task_id" value={followupReceipt.task_id} />
                <ProofRow label="turn_index" value={String(followupReceipt.turn_index)} />
                <ProofRow
                  label="grounding_sha256"
                  value={followupReceipt.grounding_sha256}
                />
              </div>
            ) : null}
          </CardContent>
        </Card>
      </DataState>
    </PageShell>
  );
}
