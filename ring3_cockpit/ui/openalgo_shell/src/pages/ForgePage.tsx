import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  EmptyState,
  Field,
  JsonBlock,
  PageHeader,
  ProgressBar,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  Table,
  TextInput,
  Toolbar,
} from "../components/ui";
import { SILOS } from "../config/nav";
import { api, assertFinite, errorMessage } from "../lib/api";
import { asArray, asRecord, duration, int, num, safeNumber, text, upper } from "../lib/format";
import { rowsOf } from "../lib/payload";
import { adwinOf, mergeDrift } from "../lib/telemetry";
import { AdwinDriftRow } from "../components/telemetry";
import { useTicker } from "../state/session";
import type { PageProps } from "./types";

type Note = { tone: "neutral" | "bull" | "bear"; message: string };

const DSR_FLOOR = 1.5;
const FILL_FLOOR = 30;
const DRIFT_CEILING = 0.5;
const SILO_OPTIONS = SILOS.map((silo) => ({
  value: silo.id,
  label: `${silo.short} · ${silo.floorBps.toFixed(2)} bps`,
}));

const SKILL_MODULES = [
  {
    name: "factor_discovery",
    title: "Factor Discovery",
    contract: {
      module: "factor_discovery",
      ring: "ring2_brain",
      emits: "ranked factor candidates with per-row digests",
      inputs: ["factor panel", "universe size", "top_k"],
      law: "sec_018 Law 3 — every ranked number carries a SHA-256 commitment",
      never: ["order signing", "broker keys", "IBOR writes"],
    },
  },
  {
    name: "strategy_synthesis",
    title: "Strategy Synthesis",
    contract: {
      module: "strategy_synthesis",
      ring: "ring2_brain",
      emits: "StrategyGenome candidates with Deflated Sharpe",
      inputs: ["factor weights", "purged K-fold folds", "embargo pct"],
      law: "sec_018 Law 4 — DSR >= 1.50 across 30+ shadow fills",
      never: ["live deployment", "capital allocation"],
    },
  },
  {
    name: "risk_overlay",
    title: "Risk Overlay",
    contract: {
      module: "risk_overlay",
      ring: "ring2_brain",
      emits: "position cap, stop bps and fragility override",
      inputs: ["contagion fragility", "silo cost floor", "vault cap"],
      law: "sec_008 triple breaker — fragility >= 0.75 revokes long leases",
      never: ["bypass the 98% conformal gate", "override ADR-009 stops"],
    },
  },
  {
    name: "execution_policy",
    title: "Execution Policy",
    contract: {
      module: "execution_policy",
      ring: "ring2_brain",
      emits: "maker-peg / TWAP policy proposal for Ring 0 review",
      inputs: ["top-of-book depth", "spread bps", "cancel-to-fill"],
      law: "sec_018 Law 2 — no LLM inside the microsecond loop",
      never: ["sign orders", "open exchange sockets"],
    },
  },
];

function GateChip({ label, pass }: { label: string; pass: boolean | null }) {
  const tone = pass === null ? "muted" : pass ? "bull" : "bear";
  return (
    <Badge tone={tone} mono>
      {pass === null ? "—" : pass ? "✓" : "✕"} {label}
    </Badge>
  );
}

export function ForgePage({ mode }: PageProps) {
  const [forge, setForge] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<Note | null>(null);
  const [busy, setBusy] = useState(false);

  const [pluginName, setPluginName] = useState("alpha_v3");
  const [candidateSharpe, setCandidateSharpe] = useState("1.85");
  const [shadowFills, setShadowFills] = useState("30");
  const [silo, setSilo] = useState("US_IBKR");
  const [verdict, setVerdict] = useState<Record<string, unknown> | null>(null);

  const [module, setModule] = useState(SKILL_MODULES[0].name);

  const now = useTicker(1000);

  const load = useCallback(async () => {
    try {
      const payload = await api.get<unknown>("/cockpit/forge");
      setForge(asRecord(payload));
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = globalThis.setInterval(() => void load(), 15_000);
    return () => globalThis.clearInterval(timer);
  }, [load]);

  const thresholds = asRecord(forge.thresholds);
  const nextRun = asRecord(forge.next_run);
  const leaderboard = useMemo(
    () =>
      rowsOf(forge, ["leaderboard"]).sort(
        (left, right) => safeNumber(right.deflated_sharpe, 0) - safeNumber(left.deflated_sharpe, 0),
      ),
    [forge],
  );

  const champions = leaderboard.filter((row) => upper(row.role) === "CHAMPION").length;
  const challengers = leaderboard.length - champions;

  const driftRows = useMemo(
    () =>
      leaderboard.map((row) => {
        // V2.3 ships `adwin_drift` as `{state, drift_score, window_size, last_checked_utc}`.
        const drift = mergeDrift(adwinOf(row.adwin_drift), adwinOf(row.concept_drift ?? row.drift_score));
        const score = drift?.driftScore ?? Number.NaN;
        const state = drift?.state ?? null;
        const detected =
          row.drift_detected === true ||
          row.concept_drift_alert === true ||
          (state ?? "").toUpperCase().includes("HALVED") ||
          (Number.isFinite(score) && score > DRIFT_CEILING);
        return {
          id: text(row.strategy_id ?? row.genome_id, "PLUGIN"),
          score,
          state,
          drift,
          detected,
        };
      }),
    [leaderboard],
  );
  const drifted = driftRows.filter((row) => row.detected).length;

  const countdownToMidnight = useMemo(() => {
    const stamp = new Date(now);
    const secondsOfDay = stamp.getUTCHours() * 3600 + stamp.getUTCMinutes() * 60 + stamp.getUTCSeconds();
    return 86_400 - secondsOfDay;
  }, [now]);

  const evaluate = useCallback(async () => {
    setBusy(true);
    setNote(null);
    try {
      const name = pluginName.trim();
      if (!/^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(name))
        throw new Error("plugin_name must start alphanumeric (letters, digits, _ . -)");
      const sharpe = assertFinite(candidateSharpe, "candidate sharpe");
      if (sharpe < -10 || sharpe > 25) throw new Error("candidate sharpe must lie in [-10, 25]");
      const fills = Math.trunc(assertFinite(shadowFills, "shadow fills"));
      if (fills < 0 || fills > 100_000) throw new Error("shadow fills must lie in [0, 100000]");
      const payload = await api.post<unknown>("/control/forge/evaluate-challenger", {
        plugin_name: name,
        candidate_sharpe: sharpe,
        shadow_fills: fills,
        silo,
        operator: "ring3.operator",
      });
      const record = asRecord(payload);
      setVerdict(record);
      setNote({
        tone: record.status === "PROMOTED" ? "bull" : "bear",
        message: `${text(record.status, "REJECTED")} · ${name} · recorded fills ${int(record.recorded_fills)} · mean net ${num(record.mean_net_bps, 3)} bps`,
      });
      void load();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(false);
    }
  }, [candidateSharpe, pluginName, shadowFills, silo, load]);

  // SKILL.md bodies are deferred (sec_007): no body probe is issued. The declared
  // contract below is authoritative and rendered directly, without a 404 round trip.
  const selectedModule = SKILL_MODULES.find((entry) => entry.name === module) ?? SKILL_MODULES[0];

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="FORGE"
        subtitle="Nocturnal champion/challenger audit, ADWIN drift and the four-module SKILL.md contract"
        badges={
          <>
            <Badge tone={mode === "LIVE" ? "bear" : "accent"} mono>
              {mode}
            </Badge>
            <Badge tone="neutral" mono>
              00:00 UTC
            </Badge>
            <Badge tone="muted" mono>
              DSR ≥ {DSR_FLOOR.toFixed(2)} · {FILL_FLOOR} FILLS
            </Badge>
          </>
        }
        actions={
          <Button icon="refresh" variant="subtle" size="sm" onClick={() => void load()}>
            Refresh
          </Button>
        }
      />

      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}
      {drifted > 0 ? (
        <Banner tone="bear">
          CONCEPT_DRIFT_ALERT · {int(drifted)} plugin(s) above the ADWIN ceiling {num(DRIFT_CEILING, 2)} —
          size down until the next audit
        </Banner>
      ) : null}

      <StatGrid columns={4}>
        <Stat
          label="Champions"
          value={int(champions)}
          hint="live, rolling net-edge window"
          tone={champions > 0 ? "bull" : "muted"}
        />
        <Stat label="Challengers" value={int(challengers)} hint="SHADOW only · never signed" />
        <Stat
          label="Drift alerts"
          value={int(drifted)}
          hint={`ceiling ${num(DRIFT_CEILING, 2)}`}
          tone={drifted > 0 ? "bear" : "bull"}
        />
        <Stat
          label="Next audit"
          value={duration(safeNumber(nextRun.seconds_until_next_run, countdownToMidnight))}
          hint="Purged K-Fold · nightly"
        />
      </StatGrid>

      <div className="flex flex-wrap items-center justify-between gap-2 rounded-[4px] border border-hairline bg-surface px-3 py-2">
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone="accent" mono>
            00:00 UTC
          </Badge>
          <span className="text-[11px] text-ink-2">
            Nocturnal Alpha Forge · retire below {num(thresholds.retirement_floor_bps ?? 0.5, 2)} bps ·
            promote at DSR {num(thresholds.promotion_dsr_floor ?? DSR_FLOOR, 2)}
          </span>
        </div>
        <span className="text-[11px] tabular-nums text-ink-3">
          NEXT RUN IN {duration(countdownToMidnight)} · {int(leaderboard.length)} GENOMES IN SCOPE
        </span>
      </div>

      <Card
        title="Champion / challenger leaderboard"
        subtitle="GET /cockpit/forge · ranked by Deflated Sharpe"
        icon="forge"
        actions={
          <Badge tone="muted" mono>
            {forge.read_only === true ? "READ ONLY" : "TELEMETRY"}
          </Badge>
        }
        dense
      >
        {loading ? (
          <div className="flex items-center gap-2 p-3 text-[11px] text-ink-3">
            <Spinner /> reading the forge…
          </div>
        ) : leaderboard.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="No genomes yet"
              hint="The forge publishes champions and SHADOW challengers after the 00:00 UTC walk-forward."
            />
          </div>
        ) : (
          <Table
            columns={[
              "RANK",
              "PLUGIN",
              "SHARPE",
              "DSR",
              "NET bps",
              "SHADOW FILLS",
              "ADWIN DRIFT",
              "STATE",
              "GATES",
            ]}
          >
            {leaderboard.map((row, index) => {
              const isChampion = upper(row.role) === "CHAMPION";
              const dsr = safeNumber(row.deflated_sharpe, Number.NaN);
              const net = safeNumber(row.net_edge_bps ?? row.rolling_net_bps, Number.NaN);
              const sharpe = safeNumber(row.sharpe ?? row.sharpe_ratio ?? row.observed_sharpe, Number.NaN);
              const fills = safeNumber(row.shadow_fills ?? row.fills, Number.NaN);
              const drift = driftRows[index]?.score ?? Number.NaN;
              const driftState = driftRows[index]?.state ?? null;
              const driftKnown = Number.isFinite(drift);
              const driftOk = driftKnown ? drift <= DRIFT_CEILING : null;
              const fillsOk = Number.isFinite(fills) ? fills >= FILL_FLOOR : null;
              const netOk = Number.isFinite(net) ? net > 0 : null;
              const dsrOk = Number.isFinite(dsr) ? dsr >= DSR_FLOOR : null;
              return (
                <Row
                  key={`${text(row.strategy_id ?? row.genome_id, "row")}-${index}`}
                  className={isChampion ? "border-l-2 border-accent bg-elevated" : undefined}
                >
                  <Cell mono tone={isChampion ? "accent" : "muted"}>
                    {int(index + 1)}
                  </Cell>
                  <Cell>
                    <div className="flex min-w-0 flex-col">
                      <span className="truncate text-ink">
                        {text(row.strategy_id ?? row.genome_id ?? row.plugin_name)}
                      </span>
                      <span className="truncate font-mono text-[10px] text-ink-3">
                        {text(row.role ?? row.status, "—")}
                      </span>
                    </div>
                  </Cell>
                  <Cell align="right" mono>
                    {Number.isFinite(sharpe) ? num(sharpe, 2) : "—"}
                  </Cell>
                  <Cell
                    align="right"
                    mono
                    tone={Number.isFinite(dsr) ? (dsr >= DSR_FLOOR ? "bull" : "bear") : "muted"}
                  >
                    {Number.isFinite(dsr) ? num(dsr, 2) : "—"}
                  </Cell>
                  <Cell
                    align="right"
                    mono
                    tone={Number.isFinite(net) ? (net > 0 ? "bull" : "bear") : "muted"}
                  >
                    {Number.isFinite(net) ? num(net, 2) : "—"}
                  </Cell>
                  <Cell align="right" mono>
                    {Number.isFinite(fills) ? int(fills) : "—"}
                  </Cell>
                  <Cell align="right" mono tone={driftKnown ? (driftOk ? "bull" : "bear") : "muted"}>
                    {driftKnown ? num(drift, 3) : "—"}
                    {driftState === null ? null : (
                      <span className="ml-1 text-[9px] uppercase tracking-[0.06em] text-ink-3">{driftState}</span>
                    )}
                  </Cell>
                  <Cell align="right">
                    <Badge
                      tone={row.promotion_ready === true ? "bull" : isChampion ? "accent" : "neutral"}
                      mono
                    >
                      {upper(row.status ?? row.role)}
                    </Badge>
                  </Cell>
                  <Cell>
                    <div className="flex flex-wrap gap-1">
                      <GateChip label="NET EXPECTANCY > 0" pass={netOk} />
                      <GateChip label="30 SHADOW FILLS" pass={fillsOk} />
                      <GateChip label="DSR >= 1.50" pass={dsrOk} />
                      <GateChip label="ADWIN NO DRIFT" pass={driftOk} />
                    </div>
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card
          title="Evaluate challenger"
          subtitle="POST /control/forge/evaluate-challenger · admission only"
          icon="target"
        >
          <div className="grid gap-2 sm:grid-cols-2">
            <Field label="Plugin name">
              <TextInput value={pluginName} mono onChange={(event) => setPluginName(event.target.value)} />
            </Field>
            <Field label="Candidate Sharpe" hint="-10…25">
              <TextInput
                value={candidateSharpe}
                mono
                onChange={(event) => setCandidateSharpe(event.target.value)}
              />
            </Field>
            <Field label="Shadow fills" hint={`gate ≥ ${FILL_FLOOR} recorded`}>
              <TextInput value={shadowFills} mono onChange={(event) => setShadowFills(event.target.value)} />
            </Field>
            <Field label="Silo">
              <SelectInput value={silo} onChange={setSilo} options={SILO_OPTIONS} />
            </Field>
          </div>
          <Toolbar className="mt-2">
            <Button variant="primary" size="sm" icon="shield" disabled={busy} onClick={() => void evaluate()}>
              Evaluate gates
            </Button>
            {busy ? <Spinner /> : null}
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
              SHADOW ACTIVATION · NO CODE EXECUTED
            </span>
          </Toolbar>
          {verdict !== null ? (
            <div className="mt-2 flex flex-col gap-1.5">
              <Banner tone={verdict.status === "PROMOTED" ? "bull" : "bear"}>
                {text(verdict.status, "REJECTED")} · {text(verdict.plugin_name, "—")} ·{" "}
                {text(verdict.hold_reason, "no gate held this challenger")}
              </Banner>
              <div className="flex flex-wrap gap-1">
                {asArray(verdict.rejection_reasons).length === 0 ? (
                  <Badge tone="bull" mono>
                    ALL GATES PASSED
                  </Badge>
                ) : (
                  asArray(verdict.rejection_reasons).map((reason, index) => (
                    <Badge key={index} tone="bear" mono>
                      {text(reason)}
                    </Badge>
                  ))
                )}
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[10px] uppercase tracking-[0.08em] text-ink-3">
                <span>DECLARED FILLS {int(verdict.shadow_fills)}</span>
                <span>RECORDED FILLS {int(verdict.recorded_fills)}</span>
                <span>MEAN NET {num(verdict.mean_net_bps, 3)} bps</span>
                <span>ACTIVATION {upper(verdict.activation)}</span>
              </div>
            </div>
          ) : (
            <p className="mt-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
              ADMISSION ONLY · THE 00:00 UTC RUN DECIDES DEPLOYMENT
            </p>
          )}
        </Card>

        <Card
          title="ADWIN concept drift"
          subtitle="state · drift score 0…1 · window size · last checked · alert above 0.50"
          icon="activity"
        >
          {driftRows.length === 0 ? (
            <EmptyState
              title="No drift telemetry"
              hint="Drift scores arrive with the leaderboard once a challenger has SHADOW fills."
            />
          ) : (
            <div className="flex flex-col gap-2">
              {driftRows.map((row) => (
                <div key={row.id} className="flex flex-col gap-1">
                  <AdwinDriftRow drift={row.drift} label={row.id} />
                  <ProgressBar
                    value={Number.isFinite(row.score) ? row.score : 0}
                    max={1}
                    tone={row.detected ? "bear" : Number.isFinite(row.score) ? "accent" : "muted"}
                    height={4}
                  />
                </div>
              ))}
              <p className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
                DRIFT IS MEASURED ON THE SETTLED SHADOW-FILL STREAM · {int(drifted)} OF{" "}
                {int(driftRows.length)} PLUGINS DRIFTED
              </p>
            </div>
          )}
        </Card>
      </div>

      <Card
        title="SKILL.md module inspector"
        subtitle="progressive disclosure · one module body at a time"
        icon="layer"
      >
        <div className="grid gap-3 md:grid-cols-[220px_1fr]">
          <div className="flex flex-col gap-1">
            {SKILL_MODULES.map((entry) => (
              <button
                key={entry.name}
                type="button"
                onClick={() => setModule(entry.name)}
                className={`flex flex-col rounded-[4px] border px-2 py-1.5 text-left transition-colors ${
                  module === entry.name
                    ? "border-accent bg-elevated"
                    : "border-hairline bg-canvas hover:bg-elevated"
                }`}
              >
                <span className="text-[11px] font-semibold text-ink-2">{entry.title}</span>
                <span className="font-mono text-[10px] text-ink-3">{entry.name}</span>
              </button>
            ))}
            <span className="mt-1 text-[10px] uppercase tracking-[0.08em] text-ink-3">
              DEFERRED · LOCAL CONTRACT
            </span>
          </div>
          <div className="flex min-w-0 flex-col gap-1.5">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone="accent" mono>
                {selectedModule.name}
              </Badge>
              <Badge tone="muted" mono>
                RING 2
              </Badge>
              <Badge tone="neutral" mono>
                DEFERRED
              </Badge>
            </div>
            <JsonBlock value={selectedModule.contract} />
          </div>
        </div>
        <p className="mt-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
          SKILL.md BODIES ARE DEFERRED (sec_007) · THE DECLARED CONTRACT IS AUTHORITATIVE, NEVER A
          SUBSTITUTE CLAIM
        </p>
      </Card>
    </div>
  );
}

export default ForgePage;
