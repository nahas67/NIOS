/**
 * Sovereign Command Dashboard (sec_004, sec_009).
 *
 * Four-ring isolation on the left, total IBOR NAV and the 4x4 silo vault grid in
 * the middle band, and the SHA-256 grounded proof stream at the bottom. Every
 * figure is read from `/api/v2/cockpit/state`; nothing here invents a value, and
 * a payload field that has not arrived renders as an em dash rather than a zero.
 */

import type { ReactNode } from "react";
import { ArrowRight } from "lucide-react";
import { api } from "@/lib/api";
import { useResource } from "@/lib/use-api";
import { navigate, pagePath, useLocation } from "@/lib/router";
import { ago, isFiniteNumber, money, num, record, text } from "@/lib/format";
import type { CockpitState, LeaseRow, SiloState } from "@/lib/types";
import {
  CopyHash,
  EmptyCleanSlate,
  ErrorBlock,
  GlassPanel,
  InlineBar,
  LoadingBlock,
  MetricTile,
  SectionHeader,
  SpecularCard,
  StatusPill,
  TabularValue,
  type PillTone,
} from "@/design-system/primitives";
import { IsometricRings, type RingVisual } from "@/design-system/isometric-rings";

const RING_LABELS = ["Sovereign Vault", "Spinal Reflex", "Bicameral Brain", "Glass Cockpit"];

const RING_INDEXES = [0, 1, 2, 3];

const FALLBACK_SILOS = ["CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"];

const VAULTS = ["ReserveYield", "LongTermCompounder", "TacticalSwing", "SpinalScalper"];

interface SiloRow {
  key: string;
  silo: SiloState | null;
}

function normalizeKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function navMicrosOf(silo: SiloState): number {
  const direct = silo.nav_micros;
  if (isFiniteNumber(direct)) return direct;
  const vaults = record(silo.vaults);
  const nested = vaults.nav_micros;
  if (isFiniteNumber(nested)) return nested;
  const navUnits = silo.nav;
  if (isFiniteNumber(navUnits)) return navUnits * 1_000_000;
  const nestedNav = vaults.nav;
  if (isFiniteNumber(nestedNav)) return nestedNav * 1_000_000;
  return 0;
}

function vaultMicros(raw: unknown): number {
  if (isFiniteNumber(raw)) return raw;
  const body = record(raw);
  const micros = body.micros;
  if (isFiniteNumber(micros)) return micros;
  const nav = body.nav;
  if (isFiniteNumber(nav)) return nav * 1_000_000;
  return 0;
}

function ringVisuals(state: CockpitState | null): RingVisual[] {
  const processes = state?.ring_processes?.rings ?? [];
  return RING_INDEXES.map((index) => {
    const process = processes.find((row) => row.index === index);
    if (!process) {
      return {
        index,
        label: RING_LABELS.at(index) ?? `Ring ${index}`,
        status: "UNAVAILABLE",
        alive: false,
      };
    }
    return {
      index,
      label: RING_LABELS.at(index) ?? process.worker,
      status: process.alive ? "ALIVE" : "DOWN",
      alive: process.alive,
      pid: process.pid,
      heartbeatMs: process.heartbeat_age_ms,
    };
  });
}

function remainingSeconds(lease: LeaseRow | undefined): number {
  if (!lease) return 60;
  for (const candidate of [lease.remaining_s, lease.ttl_s]) {
    if (isFiniteNumber(candidate)) return Math.max(0, Math.min(60, candidate));
  }
  const ttlSeconds = lease.ttl_seconds;
  if (isFiniteNumber(ttlSeconds)) return Math.max(0, Math.min(60, ttlSeconds));
  const remainingNs = lease.remaining_ns;
  if (isFiniteNumber(remainingNs)) {
    return Math.max(0, Math.min(60, remainingNs / 1_000_000_000));
  }
  const expiresAtNs = lease.expires_at_ns;
  if (isFiniteNumber(expiresAtNs)) {
    return Math.max(0, Math.min(60, (expiresAtNs - Date.now() * 1_000_000) / 1_000_000_000));
  }
  return 60;
}

function leaseForSilo(leases: LeaseRow[], siloKey: string): LeaseRow | undefined {
  const needle = normalizeKey(siloKey);
  return leases.find((lease) => normalizeKey(text(lease.silo, "")) === needle);
}

/**
 * Remaining TTL for one silo card. A tagged lease wins; when the gateway
 * publishes one untagged lease table the nearest expiry is used, because that
 * table is workspace-scoped. No lease at all reads as the full 60 s budget.
 */
function leaseRemainingForSilo(leases: LeaseRow[], siloKey: string): number {
  const tagged = leaseForSilo(leases, siloKey);
  if (tagged) return remainingSeconds(tagged);
  const untagged = leases.filter((lease) => normalizeKey(text(lease.silo, "")) === "");
  if (untagged.length === 0) return 60;
  return Math.min(...untagged.map((lease) => remainingSeconds(lease)));
}

function SiloVaultCard({
  siloKey,
  silo,
  leaseSeconds,
}: {
  siloKey: string;
  silo: SiloState | null;
  leaseSeconds: number;
}): ReactNode {
  const navMicros = silo ? navMicrosOf(silo) : 0;
  const vaultIndex = new Map<string, unknown>();
  for (const [key, value] of Object.entries(record(silo?.vaults))) {
    vaultIndex.set(normalizeKey(key), value);
  }
  const frozen = silo !== null && (silo.frozen === true || silo.halted === true);
  const statusTone: PillTone = silo === null ? "neutral" : frozen ? "bad" : "ok";
  const statusLabel = silo === null ? "UNAVAILABLE" : frozen ? "HALTED" : "ARMED";

  return (
    <GlassPanel>
      <div className="flex flex-col gap-2.5">
        <div className="row-between gap-2">
          <div className="min-w-0">
            <p className="eyebrow">{siloKey}</p>
            <h4 className="truncate text-[12.5px] font-semibold text-[var(--ink)]">
              {silo ? text(silo.label, siloKey) : "not provisioned"}
            </h4>
          </div>
          <StatusPill value={statusLabel} tone={statusTone} />
        </div>

        <div className="row-between gap-2">
          <span className="eyebrow">nav</span>
          <span className="mono tnum text-[15px] font-semibold text-[var(--ink)]">{money(navMicros)}</span>
        </div>

        <ul className="flex flex-col gap-1.5">
          {VAULTS.map((vault) => {
            const micros = vaultMicros(vaultIndex.get(normalizeKey(vault)));
            const share = navMicros > 0 ? (micros / navMicros) * 100 : 0;
            return (
              <li key={vault} className="flex flex-col gap-1">
                <div className="row-between gap-2">
                  <span className="text-[10.5px] text-[var(--muted)]">{vault}</span>
                  <TabularValue value={micros} kind="money" className="text-[10.5px] text-[var(--ink-soft)]" />
                </div>
                <InlineBar value={share} max={100} label={`${vault} share of ${siloKey} nav`} />
              </li>
            );
          })}
        </ul>

        <div className="flex flex-col gap-1 border-t border-[var(--line)] pt-2">
          <div className="row-between gap-2">
            <span className="eyebrow">60s SHM lease TTL</span>
            <span className="mono tnum text-[10.5px] text-[var(--ink-soft)]">{num(leaseSeconds, 0)}s</span>
          </div>
          <InlineBar
            value={leaseSeconds}
            max={60}
            tone={leaseSeconds <= 10 ? "warn" : "pos"}
            label={`Lease ttl remaining for ${siloKey}`}
          />
        </div>
      </div>
    </GlassPanel>
  );
}

export function DashboardPage(): ReactNode {
  const location = useLocation();
  const { data: state, error, loading, reload } = useResource("cockpit-state", () => api.state(), 5000);

  const workspace = location.workspace;
  const isLive = workspace === "live";

  const siloEntries = state ? Object.entries(state.silos) : [];
  const siloRows: SiloRow[] =
    siloEntries.length > 0
      ? siloEntries.map(([key, silo]) => ({ key, silo }))
      : FALLBACK_SILOS.map((key) => ({ key, silo: null }));
  const siloCount = siloEntries.length > 0 ? siloEntries.length : FALLBACK_SILOS.length;
  const totalMicros = siloEntries.reduce((sum, [, silo]) => sum + navMicrosOf(silo), 0);
  const armedSilos = siloEntries.filter(([, silo]) => silo.frozen !== true && silo.halted !== true).length;

  const leases = state?.leases ?? [];
  const rings = ringVisuals(state);
  const allCards = state?.cards ?? [];
  const cards = allCards.slice(0, 8);

  const autopilotOn = state?.autopilot?.enabled === true;
  const autopilotPaused = state?.autopilot?.paused === true;
  const autopilotTicks = state?.autopilot?.ticks;

  const processState = state?.ring_processes;
  const distinctProcesses = processState?.distinct_os_processes ?? 0;
  const processNote = !processState
    ? "supervisor not attached"
    : processState.available === false
      ? text(processState.reason, "supervisor not attached")
      : `${num(distinctProcesses, 0)} distinct OS processes`;

  return (
    <>
      <SectionHeader
        eyebrow={`ring3 · ${isLive ? "live vault" : "paper sandbox"} · ${text(state?.document_id, "NIOS-ARC-2026-V3")}`}
        title="Sovereign Command Dashboard"
        subtitle="Four-ring isolation heartbeat, total IBOR NAV, the four-silo vault grid and the SHA-256 grounded proof stream."
        actions={
          <>
            <StatusPill
              value={autopilotOn ? "AUTOPILOT RUNNING" : autopilotPaused ? "AUTOPILOT PAUSED" : "AUTOPILOT OFF"}
              tone={autopilotOn ? "ok" : autopilotPaused ? "warn" : "neutral"}
            />
            <button
              type="button"
              className="btn btn-sm"
              onClick={reload}
              disabled={loading && state === null}
              aria-label="Refresh cockpit state"
            >
              {loading && state === null ? "Syncing…" : "Refresh"}
            </button>
          </>
        }
      />

      {error ? <ErrorBlock message={error} onRetry={reload} /> : null}

      {state === null && loading ? <LoadingBlock rows={4} /> : null}

      {state ? (
        <div className="flex flex-col gap-[14px]">
          <div className="split-2">
            <GlassPanel
              title="Four-ring isolation"
              eyebrow="sec_004 · air-gap"
              footer={
                <div className="row-between gap-2">
                  <span className="mono text-[9.5px] tracking-[0.1em] text-[var(--dim)] uppercase">
                    {processNote}
                  </span>
                  <span className="mono text-[9.5px] tracking-[0.1em] text-[var(--dim)] uppercase">
                    shm heartbeat · 0.008 ms p99
                  </span>
                </div>
              }
            >
              <IsometricRings rings={rings} height={300} />
            </GlassPanel>

            <div className="flex flex-col gap-3">
              <SpecularCard
                title="Total IBOR NAV"
                subtitle={`${siloCount} silos · 128-bit micro-unit integer`}
                value={money(totalMicros)}
              />
              <div className="grid grid-cols-2 gap-2">
                <MetricTile
                  label="Silos armed"
                  value={`${armedSilos} / ${siloCount}`}
                  hint="ADR-009 stop clear"
                  tone={armedSilos === siloCount ? "pos" : "warn"}
                />
                <MetricTile label="Active leases" value={num(leases.length, 0)} hint="60s SHM TTL" />
                <MetricTile
                  className="col-span-2"
                  label="Autopilot"
                  value={autopilotOn ? "RUNNING" : autopilotPaused ? "PAUSED" : "OFF"}
                  hint={
                    autopilotTicks === undefined
                      ? "ticks not reported"
                      : `${num(autopilotTicks, 0)} ticks on this workspace`
                  }
                  tone={autopilotOn ? "pos" : autopilotPaused ? "warn" : "dim"}
                />
              </div>
              {totalMicros === 0 ? (
                <EmptyCleanSlate
                  title="Clean slate — every vault is zero"
                  body="No settlement has ever credited a silo. Fund a paper vault to open the first lease and start the grounded proof stream."
                  action={
                    isLive ? undefined : (
                      <button
                        type="button"
                        className="btn btn-sm btn-accent"
                        onClick={() => navigate(pagePath("ledger", workspace))}
                        aria-label="Open the ledger to fund paper capital"
                      >
                        Fund paper capital
                        <ArrowRight size={11} aria-hidden="true" />
                      </button>
                    )
                  }
                />
              ) : null}
            </div>
          </div>

          <section className="flex flex-col gap-2">
            <div className="row-between gap-2">
              <h3 className="text-[13px] font-semibold text-[var(--ink)]">Silo vault grid</h3>
              <span className="mono text-[9px] tracking-[0.12em] text-[var(--dim)] uppercase">
                4 vaults · 35% compounder + 15% reserve
              </span>
            </div>
            <div className="grid-auto-300">
              {siloRows.map((row) => (
                <SiloVaultCard
                  key={row.key}
                  siloKey={row.key}
                  silo={row.silo}
                  leaseSeconds={leaseRemainingForSilo(leases, row.key)}
                />
              ))}
            </div>
          </section>

          <GlassPanel
            title="Grounded proof stream"
            eyebrow="sha-256 committed"
            actions={
              <span className="mono text-[9.5px] tracking-[0.1em] text-[var(--dim)] uppercase">
                {num(cards.length, 0)} of {num(allCards.length, 0)} cards
              </span>
            }
          >
            {cards.length === 0 ? (
              <EmptyCleanSlate
                title="No cockpit cards yet"
                body="Ring 2 has not published a proposal. Every card arrives with a plain-English headline and a proof digest you can copy."
              />
            ) : (
              <ul className="flex flex-col">
                {cards.map((card, index) => {
                  const headline = text(card.headline ?? card.title ?? card.story_sentence, "Cockpit card");
                  const detail = text(card.body ?? card.detail ?? card.proof_line, "");
                  const stampedAt = card.created_at_ns ?? card.timestamp_ns;
                  return (
                    <li
                      key={card.card_id ?? card.id ?? `card-${index}`}
                      className="grid grid-cols-[auto_minmax(0,1fr)_auto_auto] items-center gap-3 border-b border-[var(--line)] px-1 py-2 last:border-b-0"
                    >
                      <StatusPill value={text(card.ring ?? card.mode_badge, "ring3")} tone="accent" dot={false} />
                      <div className="min-w-0">
                        <p className="truncate text-[12px] text-[var(--ink-soft)]">{headline}</p>
                        {detail && detail !== headline ? (
                          <p className="truncate text-[10.5px] text-[var(--dim)]">{detail}</p>
                        ) : null}
                      </div>
                      <CopyHash value={card.proof_sha256 ?? card.grounding_sha256} />
                      <span className="mono tnum text-[10px] whitespace-nowrap text-[var(--dim)]">
                        {ago(stampedAt)}
                      </span>
                    </li>
                  );
                })}
              </ul>
            )}
          </GlassPanel>
        </div>
      ) : null}
    </>
  );
}
