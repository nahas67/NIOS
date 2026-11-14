/**
 * Alpha Forge & Skills (`/forge`).
 *
 * The nightly champion/challenger audit, the cross-process forge + tail-hedge
 * cycle, and the progressive-disclosure SKILL.md inspector. The leaderboard is
 * a read model; the two mutating controls return their own receipts, and the
 * skill bodies load only when an operator asks for one.
 */

import { useId, useState, type ReactNode } from "react";
import { ChevronRight, FileText, Hammer } from "lucide-react";
import {
  CopyHash,
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  LoadingBlock,
  SectionHeader,
  StatusPill,
  TabularValue,
  passTone,
} from "@/design-system/primitives";
import { api } from "@/lib/api";
import { bool, duration, int, num, record, text, titleize, utc } from "@/lib/format";
import { useLocation } from "@/lib/router";
import { useAction, useNow, useResource } from "@/lib/use-api";
import type { ChallengerEvalReceipt, JsonRecord, SkillMarkdownReceipt } from "@/lib/types";

const SILOS = ["INDIA_ZERODHA_DHAN", "CRYPTO_BINANCE", "US_IBKR", "FX_MT5"] as const;

const SNAPSHOT_KEYS = ["count", "active_count", "graduated_count", "shadow_count", "plugins", "required_shadow_fills", "history"];
const SKILL_META_KEYS = [
  "count",
  "skills",
  "bodies_loaded",
  "loaded_tokens_estimate",
  "token_overhead",
  "progressive",
  "load_order",
  "enabled_count",
];

interface MarkdownModule {
  heading: string;
  body: string;
}

function workspaceLabel(workspace: "base" | "paper" | "live"): string {
  if (workspace === "live") return "LIVE VAULT";
  if (workspace === "paper") return "PAPER SANDBOX";
  return "GATEWAY";
}

function thresholdNumber(source: Record<string, number>, keys: string[], fallback: number): number {
  for (const key of keys) {
    const value = source[key];
    if (typeof value === "number" && Number.isFinite(value)) return value;
  }
  return fallback;
}

function namedEntries(source: unknown, containerKey: string, metaKeys: string[]): string[] {
  const wrapped = record(source);
  const list: unknown = Array.isArray(source) ? source : wrapped[containerKey];
  if (Array.isArray(list)) {
    const names = list
      .map((item) => (typeof item === "string" ? item : text(record(item).name, "")))
      .filter((name) => name !== "");
    if (names.length > 0) return Array.from(new Set(names));
  }
  return Object.keys(wrapped).filter((key) => !metaKeys.includes(key));
}

function stripFrontmatter(markdown: string): { frontmatter: string; body: string } {
  const match = /^---\r?\n([\s\S]*?)\r?\n---\r?\n?/.exec(markdown);
  if (!match) return { frontmatter: "", body: markdown };
  return { frontmatter: match[1] ?? "", body: markdown.slice(match[0].length) };
}

function frontmatterRows(receipt: SkillMarkdownReceipt): Array<[string, string]> {
  const declared = record(receipt.frontmatter);
  const entries = Object.entries(declared).map(
    ([key, value]) =>
      [
        key,
        Array.isArray(value)
          ? value.map((item) => text(item, "")).filter((item) => item !== "").join(", ")
          : text(value, ""),
      ] as [string, string],
  );
  if (entries.length > 0) return entries;
  return stripFrontmatter(text(receipt.markdown, ""))
    .frontmatter.split(/\r?\n/)
    .map((line) => {
      const colon = line.indexOf(":");
      if (colon <= 0) return null;
      return [line.slice(0, colon).trim(), line.slice(colon + 1).trim()] as [string, string];
    })
    .filter((entry): entry is [string, string] => entry !== null);
}

function markdownModules(markdown: string): MarkdownModule[] {
  const { body } = stripFrontmatter(markdown);
  const chunks = body.split(/^##\s+/m);
  const modules: MarkdownModule[] = [];
  const intro = (chunks[0] ?? "").trim();
  if (intro !== "") modules.push({ heading: "Overview", body: intro });
  for (const chunk of chunks.slice(1)) {
    const newline = chunk.indexOf("\n");
    const heading = (newline === -1 ? chunk : chunk.slice(0, newline)).trim();
    const sectionBody = newline === -1 ? "" : chunk.slice(newline + 1).trim();
    modules.push({ heading: heading === "" ? "Section" : heading, body: sectionBody });
  }
  return modules;
}

function renderGateValue(value: number | boolean): ReactNode {
  if (typeof value === "boolean") {
    return <span className="mono text-[10.5px]">{text(value)}</span>;
  }
  return <TabularValue value={value} digits={3} />;
}

export function ForgePage(): ReactNode {
  const { workspace } = useLocation();
  const forge = useResource("forge", () => api.forge(), 30_000);
  const now = useNow(1_000);
  const cycle = useAction<JsonRecord>();
  const challenger = useAction<ChallengerEvalReceipt>();
  const skill = useAction<SkillMarkdownReceipt>();
  const pluginListId = useId();

  const [pluginName, setPluginName] = useState("");
  const [candidateSharpe, setCandidateSharpe] = useState("1.62");
  const [shadowFills, setShadowFills] = useState("30");
  const [challengerSilo, setChallengerSilo] = useState<string>(SILOS[0]);
  const [challengerFormError, setChallengerFormError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const data = forge.data;

  const runForge = async () => {
    const receipt = await cycle.run(() => api.forgeHedge());
    if (receipt !== null) forge.reload();
  };

  const runChallenger = async () => {
    const sharpe = Number(candidateSharpe);
    const fills = Number(shadowFills);
    if (pluginName.trim() === "") {
      setChallengerFormError("Name the plugin or challenger strategy to evaluate.");
      return;
    }
    if (!Number.isFinite(sharpe)) {
      setChallengerFormError("Candidate Sharpe must be a number.");
      return;
    }
    if (!Number.isFinite(fills) || fills < 0 || !Number.isInteger(fills)) {
      setChallengerFormError("Shadow fills must be a whole number of at least 0.");
      return;
    }
    setChallengerFormError(null);
    await challenger.run(() =>
      api.evaluateChallenger({
        plugin_name: pluginName.trim(),
        candidate_sharpe: sharpe,
        shadow_fills: fills,
        silo: challengerSilo,
      }),
    );
  };

  const chooseSkill = (name: string) => {
    setSelected(name);
    skill.reset();
    void skill.run(() => api.skillMarkdown(name));
  };

  if (!data) {
    return (
      <>
        <SectionHeader
          title="Alpha Forge & Skills"
          eyebrow="ring2_brain · nightly 00:00 UTC audit"
          subtitle="Champion/challenger promotion on the deflated Sharpe floor, the cross-process forge + tail-hedge receipt, and the SKILL.md inspector."
          actions={<StatusPill value={workspaceLabel(workspace)} tone={workspace === "live" ? "bad" : "accent"} />}
        />
        <div className="flex flex-col gap-[14px]">
          <GlassPanel title="Alpha Forge telemetry" eyebrow="ring2_brain.forge">
            {forge.loading ? (
              <LoadingBlock rows={6} label="Reading the forge leaderboard" />
            ) : forge.error ? (
              <ErrorBlock message={forge.error} onRetry={forge.reload} />
            ) : (
              <EmptyCleanSlate
                title="No forge snapshot published"
                body="The nightly audit snapshot, threshold table and skill catalog arrive in one read model."
                action={
                  <button
                    type="button"
                    className="btn btn-sm"
                    onClick={forge.reload}
                    aria-label="Refresh forge telemetry"
                  >
                    Refresh forge
                  </button>
                }
              />
            )}
          </GlassPanel>
        </div>
      </>
    );
  }

  const snapshot = record(data.forge);
  const signature = record(record(cycle.receipt).ring0_signature);
  const rejection = record(record(cycle.receipt).rejection);
  const dsrGate = thresholdNumber(
    data.thresholds,
    ["promote_deflated_sharpe", "promotion_dsr_floor"],
    1.5,
  );
  const floorBps = thresholdNumber(
    data.thresholds,
    ["demote_net_edge_bps", "retirement_floor_bps"],
    0.5,
  );
  const generatedAtMs = data.generated_at_ns / 1_000_000;
  const elapsedSeconds = Math.max(0, (now - generatedAtMs) / 1_000);
  const remainingSeconds = Math.max(0, data.next_run.seconds_until_next_run - elapsedSeconds);
  const plugins = namedEntries(data.plugins, "plugins", SNAPSHOT_KEYS);
  const skills = namedEntries(data.skills, "skills", SKILL_META_KEYS);
  const championRows = data.leaderboard.filter((row) => row.role === "CHAMPION").length;
  const readyRows = data.leaderboard.filter((row) => row.promotion_ready).length;
  const bodiesLoaded = record(data.skills).bodies_loaded;
  const tailHedge = bool(record(cycle.receipt).tail_hedge_triggered);
  const signed = bool(record(cycle.receipt).signed) && bool(signature.verified);
  const cycleReceipt = cycle.receipt;
  const challengerReceipt = challenger.receipt;
  const skillReceipt = skill.receipt;
  const skillModules = skillReceipt ? markdownModules(text(skillReceipt.markdown, "")) : [];
  const skillFrontmatter = skillReceipt ? frontmatterRows(skillReceipt) : [];

  return (
    <>
      <SectionHeader
        title="Alpha Forge & Skills"
        eyebrow="ring2_brain · nightly 00:00 UTC audit"
        subtitle="Champion/challenger promotion on the deflated Sharpe floor, the cross-process forge + tail-hedge cycle, and the progressive-disclosure SKILL.md inspector."
        actions={
          <div className="row gap-1.5">
            <StatusPill value={workspaceLabel(workspace)} tone={workspace === "live" ? "bad" : "accent"} />
            <button
              type="button"
              className="btn btn-sm"
              onClick={forge.reload}
              disabled={forge.loading}
              aria-label="Refresh forge telemetry"
            >
              Refresh
            </button>
          </div>
        }
      />

      <div className="flex flex-col gap-[14px]">
        {forge.error ? <ErrorBlock message={forge.error} onRetry={forge.reload} /> : null}

        <GlassPanel
          title="Nightly forge schedule"
          eyebrow={`${text(data.next_run.schedule_utc, "00:00")} UTC · purged K-fold · FDR-gated`}
          actions={
            <button
              type="button"
              className="btn btn-accent"
              onClick={() => void runForge()}
              disabled={cycle.busy}
              aria-label="Run the cross-process forge and tail-hedge cycle"
            >
              <Hammer size={12} aria-hidden="true" />
              {cycle.busy ? "Forging…" : "Run Cross-Process Forge & Tail-Hedge"}
            </button>
          }
          footer={
            <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--faint)] uppercase">
              sec_018 Law 5 — compound inside the silo, evolve every midnight, Ring 2 proposes and Ring 0 signs
            </span>
          }
        >
          <div className="grid gap-[14px] md:grid-cols-2 xl:grid-cols-4">
            <div className="tile">
              <p className="kpi-label">next run in</p>
              <p className="mono tnum mt-1 text-[22px] font-semibold text-[var(--ink)]">
                {duration(remainingSeconds)}
              </p>
              <p className="kpi-hint">schedule {text(data.next_run.schedule_utc, "00:00")} UTC</p>
            </div>
            <div className="tile">
              <p className="kpi-label">next audit (UTC)</p>
              <p className="mono tnum mt-1 text-[13px] text-[var(--ink-soft)]">
                {utc(data.next_run.next_run_ns)}
              </p>
              <p className="mono tnum text-[10.5px] text-[var(--faint)]">
                last run {utc(snapshot.last_run_ns)}
              </p>
            </div>
            <div className="tile">
              <p className="kpi-label">promotion gates</p>
              <div className="chip-row mt-1.5">
                <StatusPill value={`DSR ≥ ${num(dsrGate, 2)}`} tone="accent" dot={false} />
                <StatusPill value={`FLOOR +${num(floorBps, 2)} bps`} tone="accent2" dot={false} />
              </div>
              <p className="kpi-hint">champion demotes below the floor</p>
            </div>
            <div className="tile">
              <p className="kpi-label">registered genomes</p>
              <p className="mono tnum mt-1 text-[22px] font-semibold text-[var(--ink)]">
                <TabularValue value={snapshot.genome_count} kind="int" />
              </p>
              <p className="mono tnum text-[10.5px] text-[var(--faint)]">
                <TabularValue value={snapshot.champion_count} kind="int" /> champions ·{" "}
                <TabularValue value={snapshot.challenger_count} kind="int" /> challengers ·{" "}
                <TabularValue value={snapshot.retired_count} kind="int" /> retired
              </p>
            </div>
          </div>

          {cycleReceipt ? (
            <div className="mt-3 rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--panel-sunken)] p-3">
              <div className="row-between flex-wrap gap-2">
                <div className="row flex-wrap gap-1.5">
                  <StatusPill
                    value={titleize(text(cycleReceipt.mode, "single process"))}
                    tone="info"
                    dot={false}
                  />
                  <StatusPill
                    value={tailHedge ? "TAIL HEDGE TRIGGERED" : "NO TAIL HEDGE"}
                    tone={tailHedge ? "accent" : "neutral"}
                  />
                  <StatusPill
                    value={signed ? "SIGNED · ED25519 VERIFIED" : "UNSIGNED"}
                    tone={signed ? "ok" : "warn"}
                  />
                  <StatusPill
                    value={`${text(cycleReceipt.symbol, "—")} · ${text(cycleReceipt.silo, "—")}`}
                    tone="neutral"
                    dot={false}
                  />
                </div>
                <CopyHash value={signature.grounding_hash} label="copy hedge grounding hash" />
              </div>
              <div className="mt-2 grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
                <div>
                  <p className="eyebrow">retired champions</p>
                  <p className="mono tnum text-[13px] text-[var(--ink-soft)]">
                    <TabularValue value={cycleReceipt.retired_champions} kind="int" />
                  </p>
                </div>
                <div>
                  <p className="eyebrow">disarmed leases</p>
                  <p className="mono tnum text-[13px] text-[var(--ink-soft)]">
                    <TabularValue value={cycleReceipt.disarmed_leases} kind="int" />
                  </p>
                </div>
                <div>
                  <p className="eyebrow">genomes evaluated</p>
                  <p className="mono tnum text-[13px] text-[var(--ink-soft)]">
                    <TabularValue value={cycleReceipt.genomes_evaluated} kind="int" />
                  </p>
                </div>
                <div>
                  <p className="eyebrow">cascade fragility</p>
                  <p className="mono tnum text-[13px] text-[var(--ink-soft)]">
                    <TabularValue value={cycleReceipt.cascade_fragility_index} digits={3} />
                  </p>
                </div>
                <div>
                  <p className="eyebrow">promoted</p>
                  <p className="mono tnum text-[13px] text-[var(--ink-soft)]">
                    <TabularValue value={cycleReceipt.promoted_champions} kind="int" />
                  </p>
                </div>
                <div>
                  <p className="eyebrow">elapsed</p>
                  <p className="mono tnum text-[13px] text-[var(--ink-soft)]">
                    <TabularValue value={cycleReceipt.elapsed_ms} digits={1} /> ms
                  </p>
                </div>
              </div>
              {text(rejection.reason, "") !== "" ? (
                <p className="mono mt-2 rounded-[var(--r-sm)] border border-[rgba(251,191,36,0.35)] bg-[var(--warn-soft)] px-2 py-1.5 text-[10.5px] text-[var(--warn)]">
                  Ring 0 rejected the hedge: {text(rejection.reason)}
                </p>
              ) : null}
            </div>
          ) : null}

          {cycle.error ? (
            <div className="mt-3">
              <ErrorBlock message={cycle.error} onRetry={() => void runForge()} />
            </div>
          ) : null}
        </GlassPanel>

        <GlassPanel
          title="Champion / challenger leaderboard"
          eyebrow={`${int(data.leaderboard.length)} audited genomes · ${int(championRows)} champions`}
          actions={
            <div className="row gap-1.5">
              <StatusPill
                value={`${int(readyRows)} promotion ready`}
                tone={readyRows > 0 ? "ok" : "neutral"}
                dot={false}
              />
              <StatusPill value={`${int(plugins.length)} plugins`} tone="info" dot={false} />
            </div>
          }
        >
          {data.leaderboard.length === 0 ? (
            <EmptyCleanSlate
              title="No champions or challengers registered"
              body="The nightly audit registers a genome only after its shadow-fill window closes. Run the forge cycle to populate the leaderboard."
              action={
                <button
                  type="button"
                  className="btn btn-sm btn-accent"
                  onClick={() => void runForge()}
                  disabled={cycle.busy}
                  aria-label="Run the forge cycle to populate the leaderboard"
                >
                  Run forge cycle
                </button>
              }
            />
          ) : (
            <div className="scroll-x">
              <table className="table table-compact">
                <caption className="sr-only">
                  Champion and challenger genomes ranked with deflated Sharpe, net edge, promotion and floor
                  gates against the live thresholds.
                </caption>
                <thead>
                  <tr>
                    <th scope="col">Strategy</th>
                    <th scope="col">Role</th>
                    <th scope="col">
                      Deflated Sharpe ≥ <TabularValue value={dsrGate} digits={2} />
                    </th>
                    <th scope="col">Net edge</th>
                    <th scope="col">Promotion</th>
                    <th scope="col">
                      Floor + <TabularValue value={floorBps} digits={2} /> bps
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {data.leaderboard.map((row) => {
                    const dsrPass = row.deflated_sharpe >= dsrGate;
                    return (
                      <tr key={`${row.role}-${row.strategy_id}`}>
                        <td>
                          <span className="mono text-[10.5px] text-[var(--ink-soft)]">{row.strategy_id}</span>
                        </td>
                        <td>
                          <StatusPill
                            value={row.role}
                            tone={row.role === "CHAMPION" ? "accent" : "accent2"}
                          />
                        </td>
                        <td className="num">
                          <span className="row justify-end gap-1.5">
                            <TabularValue value={row.deflated_sharpe} digits={2} />
                            <StatusPill value={dsrPass ? "PASS" : "FAIL"} tone={passTone(dsrPass)} dot={false} />
                          </span>
                        </td>
                        <td className="num">
                          <TabularValue value={row.net_edge_bps} kind="bps" signTone />
                        </td>
                        <td>
                          <StatusPill
                            value={row.promotion_ready ? "READY" : "HOLD"}
                            tone={passTone(row.promotion_ready)}
                          />
                        </td>
                        <td>
                          <StatusPill
                            value={row.above_floor ? "ABOVE FLOOR" : "BELOW FLOOR"}
                            tone={passTone(row.above_floor)}
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </GlassPanel>

        <GlassPanel
          title="Challenger evaluator"
          eyebrow="shadow-fill gate · DSR floor before any live graduation"
          actions={
            <StatusPill
              value={`${int(record(data.plugins).count)} plugin nursery`}
              tone="info"
              dot={false}
            />
          }
        >
          <form
            className="grid gap-2 md:grid-cols-2 xl:grid-cols-4"
            onSubmit={(event) => {
              event.preventDefault();
              void runChallenger();
            }}
            aria-label="Challenger evaluation parameters"
          >
            <label className="field">
              <span className="field-label">Plugin / strategy id</span>
              <input
                className="input"
                list={pluginListId}
                value={pluginName}
                onChange={(event) => setPluginName(event.target.value)}
                placeholder={plugins[0] ?? "plugin:india_fno_stt_gamma"}
                aria-label="Plugin or strategy identifier to evaluate"
                autoComplete="off"
              />
              <datalist id={pluginListId} aria-label="Registered plugins">
                {plugins.map((name) => (
                  <option key={name} value={name} />
                ))}
              </datalist>
            </label>
            <label className="field">
              <span className="field-label">Candidate Sharpe</span>
              <input
                className="input tnum"
                inputMode="decimal"
                value={candidateSharpe}
                onChange={(event) => setCandidateSharpe(event.target.value)}
                aria-label="Candidate Sharpe ratio"
                autoComplete="off"
              />
            </label>
            <label className="field">
              <span className="field-label">Shadow fills</span>
              <input
                className="input tnum"
                inputMode="numeric"
                value={shadowFills}
                onChange={(event) => setShadowFills(event.target.value)}
                aria-label="Completed shadow fills"
                autoComplete="off"
              />
            </label>
            <label className="field">
              <span className="field-label">Silo</span>
              <select
                className="select"
                value={challengerSilo}
                onChange={(event) => setChallengerSilo(event.target.value)}
                aria-label="Challenger silo"
              >
                {SILOS.map((silo) => (
                  <option key={silo} value={silo}>
                    {silo}
                  </option>
                ))}
              </select>
            </label>
            <div className="flex items-end xl:col-span-4">
              <button
                type="submit"
                className="btn btn-accent"
                disabled={challenger.busy}
                aria-label="Evaluate the challenger against the promotion gates"
              >
                {challenger.busy ? "Auditing…" : "Evaluate challenger"}
              </button>
            </div>
            {challengerFormError ? (
              <p role="alert" className="mono text-[10.5px] text-[var(--neg)] xl:col-span-4">
                {challengerFormError}
              </p>
            ) : null}
          </form>

          {challenger.error ? (
            <div className="mt-2">
              <ErrorBlock message={challenger.error} onRetry={() => void runChallenger()} />
            </div>
          ) : null}

          {challengerReceipt ? (
            <div className="mt-3 flex flex-col gap-2">
              <div className="row-between flex-wrap gap-2">
                <div className="row flex-wrap gap-2">
                  <StatusPill
                    value={challengerReceipt.status}
                    tone={challengerReceipt.status === "PROMOTED" ? "ok" : "bad"}
                  />
                  <span className="mono tnum text-[10.5px] text-[var(--dim)]">
                    {challengerReceipt.plugin_name} · sharpe{" "}
                    <TabularValue value={challengerReceipt.candidate_sharpe} digits={3} /> ·{" "}
                    <TabularValue value={challengerReceipt.shadow_fills} kind="int" /> shadow fills
                  </span>
                </div>
                <StatusPill
                  value={challengerReceipt.promotion_ready ? "PROMOTION READY" : "HOLD"}
                  tone={passTone(challengerReceipt.promotion_ready)}
                />
              </div>

              <table className="table table-compact">
                <caption className="sr-only">Challenger promotion gates: measured, required and result.</caption>
                <thead>
                  <tr>
                    <th scope="col">Gate</th>
                    <th scope="col">Measured</th>
                    <th scope="col">Required</th>
                    <th scope="col">Result</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(challengerReceipt.gates).map(([gate, detail]) => (
                    <tr key={gate}>
                      <td>{titleize(gate)}</td>
                      <td className="num">{renderGateValue(detail.measured)}</td>
                      <td className="num">{renderGateValue(detail.required)}</td>
                      <td>
                        <StatusPill
                          value={detail.passed ? "PASS" : "FAIL"}
                          tone={passTone(detail.passed)}
                        />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {challengerReceipt.rejection_reasons.length > 0 ? (
                <ul className="flex flex-col gap-1" aria-label="Challenger rejection reasons">
                  {challengerReceipt.rejection_reasons.map((reason) => (
                    <li key={reason} className="row gap-1.5 text-[11.5px] text-[var(--neg)]">
                      <ChevronRight size={11} aria-hidden="true" />
                      {reason}
                    </li>
                  ))}
                </ul>
              ) : null}

              <p className="text-[11.5px] leading-relaxed text-[var(--dim)]">
                {text(challengerReceipt.note, "No evaluator note returned.")}
              </p>
            </div>
          ) : (
            <div className="mt-3">
              <EmptyCleanSlate
                title="No challenger audited yet"
                body="A challenger must clear the deflated Sharpe floor and the 30-fill shadow window before it can be promoted. The receipt lists every gate it was measured against."
              />
            </div>
          )}
        </GlassPanel>

        <GlassPanel
          title="Progressive-disclosure SKILL.md inspector"
          eyebrow="frontmatter in context · bodies load only on request"
          actions={
            <div className="row gap-1.5">
              <StatusPill value={`${int(record(data.skills).count)} skills`} tone="accent2" dot={false} />
              <StatusPill
                value={`${int(bodiesLoaded)} bodies loaded`}
                tone="info"
                dot={false}
                title="Only frontmatter is in context until a body is requested"
              />
            </div>
          }
        >
          <div className="split-2">
            <div className="min-w-0">
              <p className="eyebrow">skill catalog</p>
              {skills.length === 0 ? (
                <div className="mt-2">
                  <EmptyCleanSlate
                    title="No skills registered"
                    body="The Ring 2 skill registry published no SKILL.md files for this silo set."
                  />
                </div>
              ) : (
                <div className="mt-1.5 grid gap-1 sm:grid-cols-2">
                  {skills.map((name) => (
                    <button
                      key={name}
                      type="button"
                      className="palette-item"
                      data-active={selected === name}
                      aria-pressed={selected === name}
                      onClick={() => chooseSkill(name)}
                      aria-label={`Inspect the ${name} SKILL.md`}
                    >
                      <FileText size={12} className="flex-none text-[var(--dim)]" aria-hidden="true" />
                      <span className="mono truncate text-[11px]">{name}</span>
                      <ChevronRight
                        size={11}
                        className="ml-auto flex-none text-[var(--faint)]"
                        aria-hidden="true"
                      />
                    </button>
                  ))}
                </div>
              )}
            </div>

            <div className="min-w-0">
              <div className="row-between">
                <p className="eyebrow">loaded body</p>
                {selected ? (
                  <span className="mono truncate text-[9.5px] text-[var(--dim)]">
                    requested {selected}
                  </span>
                ) : null}
              </div>

              {skill.error ? (
                <div className="mt-2">
                  <ErrorBlock
                    message={skill.error}
                    onRetry={selected ? () => chooseSkill(selected) : undefined}
                  />
                </div>
              ) : null}

              {!selected ? (
                <div className="mt-2">
                  <EmptyCleanSlate
                    title="No skill body loaded"
                    body="Pick a skill from the catalog. Only its frontmatter is mirrored into context until you request the body."
                  />
                </div>
              ) : skill.busy ? (
                <div className="mt-2">
                  <LoadingBlock rows={4} label="Loading the SKILL.md body" />
                </div>
              ) : skillReceipt ? (
                <div className="mt-2 flex flex-col gap-2">
                  <div className="row-between flex-wrap gap-2">
                    <div className="min-w-0">
                      <p className="truncate text-[12px] font-medium text-[var(--ink)]">
                        {text(skillReceipt.name, selected)}
                      </p>
                      <p className="mono truncate text-[9.5px] text-[var(--dim)]" title={text(skillReceipt.path, "")}>
                        {text(skillReceipt.path, "—")}
                      </p>
                    </div>
                    <div className="row gap-2">
                      <StatusPill
                        value={`${int(skillReceipt.bytes)} bytes`}
                        tone="neutral"
                        dot={false}
                      />
                      <CopyHash value={skillReceipt.sha256_digest} label="copy SKILL.md digest" />
                    </div>
                  </div>

                  <section className="tile">
                    <p className="eyebrow">module 01 · frontmatter</p>
                    {skillFrontmatter.length === 0 ? (
                      <p className="mt-1 mono text-[10.5px] text-[var(--dim)]">No frontmatter declared.</p>
                    ) : (
                      <dl className="mt-1.5 grid gap-1 md:grid-cols-2">
                        {skillFrontmatter.map(([key, value]) => (
                          <div key={key} className="flex flex-col gap-0.5 border-b border-[var(--line)] pb-1">
                            <dt className="eyebrow">{key}</dt>
                            <dd className="mono text-[10.5px] break-words text-[var(--ink-soft)]">{value}</dd>
                          </div>
                        ))}
                      </dl>
                    )}
                  </section>

                  {skillModules.length === 0 ? (
                    <EmptyCleanSlate
                      title="Body has no headings"
                      body="The SKILL.md returned a frontmatter block without a Markdown body to disclose."
                    />
                  ) : (
                    skillModules.map((section, index) => (
                      <section key={`${section.heading}-${index}`} className="tile">
                        <div className="row-between gap-2">
                          <p className="eyebrow">
                            module {index + 2 < 10 ? `0${index + 2}` : index + 2}
                          </p>
                          <StatusPill value={text(section.heading)} tone="neutral" dot={false} />
                        </div>
                        <div className="mono mt-1.5 max-h-[260px] overflow-auto text-[10.5px] leading-relaxed whitespace-pre-wrap text-[var(--ink-soft)]">
                          {section.body === "" ? "No body text after this heading." : section.body}
                        </div>
                      </section>
                    ))
                  )}
                </div>
              ) : (
                <div className="mt-2">
                  <EmptyCleanSlate
                    title="No body returned"
                    body="The registry answered without a markdown body. Try another skill or refresh the registry."
                  />
                </div>
              )}
            </div>
          </div>
        </GlassPanel>
      </div>
    </>
  );
}
