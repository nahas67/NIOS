/**
 * Agent task hub.
 *
 * Six specialist Ring 2 agents, one token budget and a grounded assignment
 * queue. A task returns a report; it never returns an order.
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

export function AgentsPage(): ReactNode {
  const page = useApi(() => api.agents(), []);
  const [agentId, setAgentId] = useState("research_analyst");
  const [silo, setSilo] = useState("INDIA");
  const [symbols, setSymbols] = useState("RELIANCE,INFY");
  const [prompt, setPrompt] = useState("Summarise the tape and name the single strongest risk.");
  const [busy, setBusy] = useState(false);
  const [report, setReport] = useState<Record<string, unknown> | null>(null);

  const agents = rows(page.data, "agents");
  const tasks = rows(page.data, "tasks");
  const token = page.data?.token_usage;
  const used = num(token, "used", num(token, "total_tokens", 0));
  const budget = num(token, "budget", num(token, "cap", 0));
  const decision = num(page.data, "decision_score", num(page.data, "score", 0));

  const assign = async () => {
    setBusy(true);
    try {
      const next = await api.assignAgent({
        agent_id: agentId,
        silo,
        symbols: symbols
          .split(",")
          .map((item) => item.trim().toUpperCase())
          .filter(Boolean),
        prompt,
        priority: "NORMAL",
      });
      setReport(next);
      toast(`${titleCase(agentId)} task dispatched`, "success");
      page.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "assignment rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    try {
      await api.cancelAgentTasks("ALL");
      toast("All queued tasks cancelled", "success");
      page.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "cancel rejected", "error");
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 2 - COGNITION"
        title="Agent Task Hub"
        sub="Grounded research, not execution. An agent report cites its sources or it is rejected before it reaches a thesis."
        actions={
          <>
            <StatusBadge tone="accent">{agents.length || 6} AGENTS</StatusBadge>
            <Btn size="sm" tone="ghost" onClick={page.reload}>
              Refresh queue
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile label="Agents online" value={String(agents.length || 6)} sub="specialist Ring 2 lanes" />
        <MetricTile label="Tasks queued" value={String(tasks.length)} sub="grounded assignments" />
        <MetricTile
          label="Token spend"
          value={budget > 0 ? `${used} / ${budget}` : String(used)}
          sub="daily cap enforced by the router"
          badge={<StatusBadge tone="muted">BUDGET</StatusBadge>}
        />
        <MetricTile
          label="Decision score"
          value={decision ? decision.toFixed(1) : "-"}
          sub="0..100 confidence in the current thesis"
          tone={decision >= 60 ? "bull" : "neutral"}
        />
      </div>

      <div className="nx-grid nx-grid--1-2">
        <TerminalPanel title="Assign a task" subtitle="POST /api/v2/control/agents/assign">
          <Field label="Agent">
            <select className="nx-select" value={agentId} onChange={(event) => setAgentId(event.target.value)}>
              {(agents.length > 0
                ? agents.map((row) => str(row, "key", str(row, "id", "agent")))
                : ["research_analyst", "fundamental_analyst", "news_analyst", "sentiment_analyst", "trader", "risk_manager"]
              ).map((key) => (
                <option key={key} value={key}>
                  {titleCase(key)}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Silo">
            <select className="nx-select" value={silo} onChange={(event) => setSilo(event.target.value)}>
              <option value="INDIA">INDIA</option>
              <option value="US">US</option>
              <option value="FX">FX</option>
              <option value="CRYPTO">CRYPTO</option>
            </select>
          </Field>
          <Field label="Symbols" hint="Comma separated, whitelisted instruments only.">
            <input className="nx-input" value={symbols} onChange={(event) => setSymbols(event.target.value)} />
          </Field>
          <Field label="Question">
            <input className="nx-input" value={prompt} onChange={(event) => setPrompt(event.target.value)} />
          </Field>
          <div className="nx-row" style={{ marginTop: 8 }}>
            <Btn tone="primary" onClick={assign} disabled={busy}>
              {busy ? "Dispatching..." : "Dispatch grounded task"}
            </Btn>
            <Btn tone="ghost" onClick={cancel}>
              Cancel all
            </Btn>
          </div>
        </TerminalPanel>

        <TerminalPanel title="Latest report" subtitle="Every numeric claim carries a hash" flush>
          {report ? (
            <div className="nx-table-wrap">
              <table className="nx-table">
                <thead>
                  <tr>
                    <th scope="col">Field</th>
                    <th scope="col">Value</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(report)
                    .filter(([, value]) => ["string", "number", "boolean"].includes(typeof value))
                    .slice(0, 14)
                    .map(([key, value]) => (
                      <tr key={key}>
                        <td className="nx-td-dim">{titleCase(key)}</td>
                        <td className="nx-truncate">{String(value)}</td>
                      </tr>
                    ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={{ padding: 10 }}>
              <EmptyCleanSlate
                title="No task dispatched"
                body="A clean slate has no agent report. Dispatch a task to populate the queue and the grounded report panel."
                hint="agent_runs are persisted per task id"
              />
            </div>
          )}
        </TerminalPanel>
      </div>

      <TerminalPanel title="Task queue" subtitle="Every assignment is replayable" flush>
        <DataGrid
          columns={[
            {
              key: "id",
              label: "Task",
              render: (row: Record<string, unknown>) => (
                <span className="nx-mono">{str(row, "task_id", str(row, "id", "-")).slice(0, 12)}</span>
              ),
            },
            {
              key: "agent",
              label: "Agent",
              render: (row: Record<string, unknown>) => (
                <span className="nx-strong">{titleCase(str(row, "agent_id", str(row, "agent", "-")))}</span>
              ),
            },
            {
              key: "silo",
              label: "Silo",
              render: (row: Record<string, unknown>) => <span className="nx-dim">{str(row, "silo", "-")}</span>,
            },
            {
              key: "status",
              label: "Status",
              align: "right",
              render: (row: Record<string, unknown>) => {
                const status = str(row, "status", "QUEUED").toUpperCase();
                return (
                  <StatusBadge tone={status === "COMPLETE" || status === "DONE" ? "bull" : status === "FAILED" ? "bear" : "accent"}>
                    {status}
                  </StatusBadge>
                );
              },
            },
            {
              key: "tokens",
              label: "Tokens",
              align: "right",
              render: (row: Record<string, unknown>) => (
                <span className="nx-num nx-muted">{num(row, "tokens", num(row, "total_tokens", 0))}</span>
              ),
            },
          ]}
          rows={tasks}
          getKey={(row, index) => str(row, "task_id", String(index))}
          empty={
            <EmptyCleanSlate
              title="Queue empty"
              body="No agent has been asked a question in this workspace yet."
              hint="clean slate"
            />
          }
        />
      </TerminalPanel>
    </div>
  );
}
