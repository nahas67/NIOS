/**
 * Alpha Forge and progressive-disclosure skills.
 *
 * Champion versus challenger, the Deflated Sharpe promotion gate and the
 * SKILL.md inspector. The forge never promotes on a hunch: thirty shadow fills
 * and DSR >= 1.50 are hard prerequisites.
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

export function ForgePage(): ReactNode {
  const page = useApi(() => api.forge(), []);
  const [name, setName] = useState("momentum_v2");
  const [sharpe, setSharpe] = useState("1.62");
  const [fills, setFills] = useState("30");
  const [busy, setBusy] = useState(false);
  const [evaluation, setEvaluation] = useState<Record<string, unknown> | null>(null);
  const [skill, setSkill] = useState<Record<string, unknown> | null>(null);

  const leaderboard = rows(page.data, "leaderboard");
  const plugins = rows(page.data, "plugins");
  const skills = rows(page.data, "skills");
  const thresholds = page.data?.thresholds;
  const champion = leaderboard.find((row) => str(row, "role", "").toUpperCase() === "CHAMPION");
  const promoted = str(evaluation, "verdict", str(evaluation, "decision", "")).toUpperCase();

  const evaluate = async () => {
    setBusy(true);
    try {
      const next = await api.evaluateChallenger({
        plugin_name: name,
        candidate_sharpe: Number(sharpe) || 0,
        shadow_fills: Number(fills) || 0,
        silo: "INDIA",
      });
      setEvaluation(next);
      toast("Challenger evaluated against the promotion gate", "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "evaluation rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const openSkill = async (skillName: string) => {
    try {
      const next = await api.skillMarkdown(skillName);
      setSkill(next);
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "skill not found", "error");
    }
  };

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 2 - EVOLUTION"
        title="Alpha Forge & Skills"
        sub="Champions are demoted below +0.50 bps; a challenger is promoted only with Deflated Sharpe at or above 1.50 across thirty or more shadow fills."
        actions={
          <>
            <StatusBadge tone="muted">NIGHTLY 00:00 UTC</StatusBadge>
            <Btn size="sm" tone="ghost" onClick={page.reload}>
              Refresh leaderboard
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile
          label="Champion"
          value={champion ? str(champion, "plugin_name", str(champion, "name", "-")) : "-"}
          sub={champion ? `DSR ${num(champion, "deflated_sharpe", 0).toFixed(2)}` : "no champion promoted"}
          badge={<StatusBadge tone={champion ? "bull" : "muted"}>INCUMBENT</StatusBadge>}
        />
        <MetricTile label="Challengers" value={String(Math.max(0, leaderboard.length - 1))} sub="in the nursery" />
        <MetricTile
          label="Plugins"
          value={String(plugins.length)}
          sub="dynamic plugins stay deferred until Main Core"
          badge={<StatusBadge tone="muted">SEALED</StatusBadge>}
        />
        <MetricTile label="Skills" value={String(skills.length)} sub="progressive disclosure only" />
      </div>

      <div className="nx-grid nx-grid--2-1">
        <TerminalPanel title="Leaderboard" subtitle="Deflated Sharpe, not raw Sharpe" flush>
          <DataGrid
            columns={[
              {
                key: "name",
                label: "Candidate",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-strong">{str(row, "plugin_name", str(row, "name", "-"))}</span>
                ),
              },
              {
                key: "role",
                label: "Role",
                render: (row: Record<string, unknown>) => {
                  const role = str(row, "role", "CHALLENGER").toUpperCase();
                  return <StatusBadge tone={role === "CHAMPION" ? "bull" : "muted"}>{role}</StatusBadge>;
                },
              },
              {
                key: "dsr",
                label: "DSR",
                align: "right",
                render: (row: Record<string, unknown>) => {
                  const value = num(row, "deflated_sharpe", num(row, "dsr", 0));
                  return <span className={value >= 1.5 ? "nx-bull nx-num" : "nx-num nx-dim"}>{value.toFixed(2)}</span>;
                },
              },
              {
                key: "fills",
                label: "Shadow fills",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-num">{num(row, "shadow_fills", num(row, "fills", 0))}</span>
                ),
              },
            ]}
            rows={leaderboard}
            getKey={(row, index) => str(row, "plugin_name", String(index))}
            empty={
              <EmptyCleanSlate
                title="No champion yet"
                body="The forge starts with an empty leaderboard. Evaluate a challenger to open the promotion path."
                hint="DSR >= 1.50 and 30 shadow fills"
              />
            }
          />
        </TerminalPanel>

        <TerminalPanel title="Evaluate challenger" subtitle="POST /api/v2/control/forge/evaluate-challenger">
          <Field label="Plugin name">
            <input className="nx-input" value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          <Field label="Candidate DSR">
            <input className="nx-input" value={sharpe} onChange={(event) => setSharpe(event.target.value)} />
          </Field>
          <Field label="Shadow fills">
            <input className="nx-input" value={fills} onChange={(event) => setFills(event.target.value)} />
          </Field>
          <Btn tone="primary" block onClick={evaluate} disabled={busy} style={{ marginTop: 8 }}>
            {busy ? "Evaluating..." : "Evaluate against gate"}
          </Btn>
          <div className="nx-hr" style={{ margin: "10px 0" }} />
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Verdict</span>
            <StatusBadge tone={promoted === "PROMOTED" ? "bull" : "muted"}>
              {promoted ? titleCase(promoted) : "PENDING"}
            </StatusBadge>
          </div>
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Demotion floor</span>
            <span className="nx-sm nx-strong">{num(thresholds, "demotion_bps", 0.5).toFixed(2)} bps</span>
          </div>
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted">Promotion DSR</span>
            <span className="nx-sm nx-strong">{num(thresholds, "promotion_dsr", 1.5).toFixed(2)}</span>
          </div>
        </TerminalPanel>
      </div>

      <div className="nx-grid nx-grid--2">
        <TerminalPanel title="Skills" subtitle="No skill runtime is authorised before Main Core" flush>
          <DataGrid
            columns={[
              {
                key: "name",
                label: "Skill",
                render: (row: Record<string, unknown>) => (
                  <button
                    type="button"
                    className="nx-nav-item"
                    style={{ padding: 0, color: "var(--nx-text)" }}
                    onClick={() => void openSkill(str(row, "name", str(row, "id", "skill")))}
                  >
                    {str(row, "name", str(row, "id", "-"))}
                  </button>
                ),
              },
              {
                key: "state",
                label: "State",
                align: "right",
                render: (row: Record<string, unknown>) => (
                  <StatusBadge tone="muted">{str(row, "state", str(row, "status", "SEALED")).toUpperCase()}</StatusBadge>
                ),
              },
            ]}
            rows={skills}
            getKey={(row, index) => str(row, "name", String(index))}
            empty={<EmptyCleanSlate title="Skill registry sealed" body="No skill package is mounted in this build." />}
          />
        </TerminalPanel>

        <TerminalPanel title="SKILL.md inspector" subtitle="Progressive disclosure source">
          {skill ? (
            <pre
              className="nx-mono nx-scroll nx-scroll--sm"
              style={{ margin: 0, whiteSpace: "pre-wrap", color: "var(--nx-text-2)" }}
            >
              {str(skill, "markdown", str(skill, "content", "empty skill document"))}
            </pre>
          ) : (
            <EmptyCleanSlate
              title="No skill opened"
              body="Select a skill from the registry to read its progressive-disclosure document."
              hint="GET /api/v2/cockpit/skills/{name}"
            />
          )}
        </TerminalPanel>
      </div>
    </div>
  );
}
