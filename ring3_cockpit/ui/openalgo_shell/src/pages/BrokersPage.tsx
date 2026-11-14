/**
 * Page 14 — 12-venue broker hub.
 *
 * Four silos, three venues each, one write-only credential vault. Every card
 * shows the adapter probe, the masked credential and the Ed25519 signing
 * readiness; the ping returns a proof receipt, never a secret.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import {
  CopyHash,
  EmptyCleanSlate,
  ErrorBlock,
  Freshness,
  GlassPanel,
  LoadingBlock,
  MetricTile,
  passTone,
  SectionHeader,
  SpecularCard,
  StatusPill,
  TabularValue,
} from "@/design-system/primitives";
import { api } from "@/lib/api";
import { latency, record, text, toneOfLabel, utc } from "@/lib/format";
import { navigate, pagePath, useLocation } from "@/lib/router";
import type { BrokerPingReceipt, BrokerVenueRow, BrokersPageData } from "@/lib/types";
import { useAction, useResource } from "@/lib/use-api";

/* ------------------------------- helpers ------------------------------- */

function asNumber(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function flagOrNull(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

interface SiloGroup {
  silo: string;
  market: string;
  configured: number | null;
  venues: BrokerVenueRow[];
}

/* -------------------------------- page -------------------------------- */

export function BrokersPage(): ReactNode {
  const location = useLocation();
  const brokers = useResource<BrokersPageData>("brokers", () => api.brokers(), 30000);
  const data = brokers.data;

  const venues: BrokerVenueRow[] = data?.venues ?? [];
  const declared = data?.silos ?? [];

  const groups: SiloGroup[] =
    declared.length > 0
      ? declared.map((group) => ({
          silo: text(group.silo, "SILO"),
          market: text(group.market, "—"),
          configured: asNumber(group.configured),
          venues: venues.filter((row) => row.silo === group.silo),
        }))
      : deriveGroups(venues);

  const counts = data?.counts;
  const vault = data?.vault;
  const airGap = record(data?.air_gap);

  const rawKeysExposed = asNumber(airGap.raw_keys_exposed);
  const signingTools = asNumber(airGap.order_signing_tools);
  const ring3ReadOnly = flagOrNull(airGap.ring3_read_only);
  const rawSecretsExposed = flagOrNull(record(vault).raw_secrets_exposed);

  return (
    <div>
      <SectionHeader
        eyebrow={`ring0 · sovereign · ${location.workspace} workspace`}
        title="12-Venue Broker Hub"
        subtitle="Four silos × three venues. Write-only credential masks, per-venue adapter probes, dual-vendor feed health and an Ed25519 dry-run signing self-test on demand."
        actions={
          <>
            <Freshness generatedAtNs={data?.generated_at_ns} onRefresh={brokers.reload} busy={brokers.loading} />
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => navigate(pagePath("control", location.workspace))}
              aria-label="Open the credential vault and key rotation controls"
            >
              Credential vault
            </button>
          </>
        }
      />

      <div className="flex flex-col gap-[14px]">
        {brokers.loading && !data ? <LoadingBlock rows={4} label="Probing broker adapters" /> : null}
        {brokers.error ? <ErrorBlock message={brokers.error} onRetry={brokers.reload} /> : null}

        {data ? (
          <>
            <div className="kpi-strip">
              <MetricTile label="Venues" value={<TabularValue value={counts?.venues} kind="int" />} hint="adapter matrix rows" />
              <MetricTile
                label="Configured"
                value={<TabularValue value={counts?.configured} kind="int" />}
                tone={asNumber(counts?.configured) === 0 ? "dim" : "pos"}
                hint="credential stored in Ring 0"
              />
              <MetricTile
                label="Unset"
                value={<TabularValue value={counts?.unset} kind="int" />}
                tone={asNumber(counts?.unset) === 0 ? "dim" : "warn"}
                hint="no testnet credential yet"
              />
              <MetricTile
                label="Adapters importable"
                value={<TabularValue value={counts?.adapters_importable} kind="int" />}
                hint="donor module resolves"
              />
              <MetricTile
                label="Vault key version"
                value={<TabularValue value={vault?.key_version} kind="int" />}
                hint="Fernet + Ed25519 generation"
              />
            </div>

            <div className="chip-row">
              <StatusPill
                value={`raw keys exposed ${rawKeysExposed === null ? "—" : String(rawKeysExposed)}`}
                tone={rawKeysExposed === 0 ? "ok" : rawKeysExposed === null ? "neutral" : "bad"}
                title="air_gap.raw_keys_exposed must be 0"
                className="tnum"
              />
              <StatusPill
                value={`order signing tools ${signingTools === null ? "—" : String(signingTools)}`}
                tone={signingTools === 0 ? "ok" : signingTools === null ? "neutral" : "bad"}
                title="air_gap.order_signing_tools must be 0"
                className="tnum"
              />
              <StatusPill
                value={ring3ReadOnly === true ? "Ring 3 read-only" : ring3ReadOnly === false ? "Ring 3 writable" : "Ring 3 scope unknown"}
                tone={passTone(ring3ReadOnly)}
                title="air_gap.ring3_read_only"
              />
              <StatusPill
                value={vault?.write_only === true ? "Vault write-only" : "Vault mode unknown"}
                tone={passTone(flagOrNull(vault?.write_only))}
                title="vault.write_only"
              />
            </div>

            {venues.length === 0 ? (
              <GlassPanel title="Venue matrix" eyebrow="ring0 · adapter probes">
                <EmptyCleanSlate
                  title="No venues returned by the broker hub"
                  body="The gateway builds this matrix from the silo registry and the credential vault. An empty list means the registry has not been seeded on this host."
                  action={
                    <button
                      type="button"
                      className="btn btn-sm"
                      onClick={brokers.reload}
                      aria-label="Re-probe the broker venue matrix"
                    >
                      Re-probe venues
                    </button>
                  }
                />
              </GlassPanel>
            ) : (
              groups.map((group, index) => (
                <GlassPanel
                  key={group.silo}
                  title={group.silo}
                  eyebrow={`silo ${index + 1} of ${groups.length} · ${group.market}`}
                  actions={
                    <span className="pill mono">
                      <TabularValue value={group.configured} kind="int" />/
                      <TabularValue value={group.venues.length} kind="int" /> configured
                    </span>
                  }
                >
                  {group.venues.length === 0 ? (
                    <EmptyCleanSlate
                      title="No venue rows for this silo"
                      body="The silo is declared in the registry but returned no venue rows. Check the credential inventory for this silo."
                    />
                  ) : (
                    <div className="grid-auto-300">
                      {group.venues.map((row) => (
                        <VenueCard key={`${row.silo}-${row.venue}`} row={row} />
                      ))}
                    </div>
                  )}
                </GlassPanel>
              ))
            )}

            <GlassPanel
              title="Write-only Ring 0 credential vault"
              eyebrow="sec_011 · air-gap custody"
              footer={
                <div className="chip-row">
                  <StatusPill
                    value={`raw secrets exposed ${rawSecretsExposed === true ? "YES" : rawSecretsExposed === false ? "NO" : "—"}`}
                    tone={rawSecretsExposed === false ? "ok" : rawSecretsExposed === true ? "bad" : "neutral"}
                    title="vault.raw_secrets_exposed"
                  />
                  <StatusPill
                    value="no cross-border wires"
                    tone="ok"
                    title="sec_018 Law 5 — capital compounds inside the silo"
                  />
                  <StatusPill
                    value="Ring 3 never imports a broker adapter"
                    tone="ok"
                    title="sec_004 air-gap contract"
                  />
                  <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--dim)] uppercase">
                    {text(data.page, "brokers")} · generated{" "}
                    <span className="tnum">{utc(data.generated_at_ns)}</span> UTC
                  </span>
                </div>
              }
            >
              <div className="grid-auto-240">
                <div className="tile">
                  <p className="kpi-label">Vault path</p>
                  <p className="mono mt-1 text-[11px] break-all text-[var(--ink-soft)]">{text(vault?.path)}</p>
                </div>
                <div className="tile">
                  <p className="kpi-label">Write-only</p>
                  <div className="mt-1">
                    <StatusPill
                      value={vault?.write_only === true ? "WRITE-ONLY" : "UNVERIFIED"}
                      tone={passTone(flagOrNull(vault?.write_only))}
                    />
                  </div>
                </div>
                <div className="tile">
                  <p className="kpi-label">Key version</p>
                  <p className="mono tnum mt-1 text-[16px] font-semibold">
                    <TabularValue value={vault?.key_version} kind="int" />
                  </p>
                </div>
                <div className="tile">
                  <p className="kpi-label">Ed25519 public fingerprint</p>
                  <div className="mt-1">
                    <CopyHash value={vault?.ed25519_public_fingerprint} label="copy the vault Ed25519 public fingerprint" />
                  </div>
                </div>
              </div>
            </GlassPanel>
          </>
        ) : null}

        {!data && !brokers.loading && !brokers.error ? (
          <GlassPanel title="Venue matrix" eyebrow="ring0 · adapter probes">
            <EmptyCleanSlate
              title="No broker hub payload"
              body="The gateway did not return the 12-venue matrix, silo groups or vault state for this workspace."
            />
          </GlassPanel>
        ) : null}
      </div>
    </div>
  );
}

/* ------------------------------ venue card ------------------------------ */

function VenueCard({ row }: { row: BrokerVenueRow }): ReactNode {
  const ping = useAction<BrokerPingReceipt>();
  const [showReceipt, setShowReceipt] = useState(true);

  const signing = record(row.signing);
  const feed = record(row.feed);
  const credentialStatus = text(row.credential_status, "UNSET");
  const adapterImportable = row.adapter_importable === true;
  const signingVerified = flagOrNull(signing.dry_run_verified);
  const feedDual = flagOrNull(feed.dual_vendor);
  const credentialDryRun = flagOrNull(row.dry_run_verified);

  const pingReceipt = record(ping.receipt);
  const pingAdapter = record(pingReceipt.adapter);
  const pingSigning = record(pingReceipt.signing);
  const pingCredential = record(pingReceipt.credential);
  const rawSecretReturned = flagOrNull(pingCredential.raw_secret_returned);
  const pingSigningVerified = flagOrNull(pingSigning.verified);

  const runPing = (): void => {
    setShowReceipt(true);
    void ping.run(() => api.brokerPing(row.venue));
  };

  return (
    <SpecularCard
      title={<span className="mono text-[12px] tracking-[0.06em]">{text(row.venue, "venue").toUpperCase()}</span>}
      subtitle={`${text(row.market, "—")} · ${text(row.silo, "—")}`}
    >
      <div className="mt-2 flex flex-col gap-2">
        <div className="chip-row">
          <StatusPill value={credentialStatus} tone={toneOfLabel(credentialStatus)} />
          {row.environment ? (
            <StatusPill
              value={row.environment}
              tone={row.environment.toUpperCase() === "LIVE" ? "bad" : "info"}
            />
          ) : null}
        </div>

        <div className="row-between gap-2">
          <span className="mono min-w-0 truncate text-[10px] text-[var(--dim)]" title={text(row.adapter_module)}>
            {text(row.adapter_module, "—")}
          </span>
          <StatusPill
            value={adapterImportable ? "IMPORTABLE" : "MISSING"}
            tone={passTone(adapterImportable)}
            title={text(row.adapter_reason, "adapter probe")}
          />
        </div>

        {!adapterImportable && text(row.adapter_reason, "") !== "" ? (
          <p className="mono text-[9.5px] break-words text-[var(--dim)]">{text(row.adapter_reason)}</p>
        ) : null}

        <dl className="grid grid-cols-2 gap-2">
          <div className="min-w-0">
            <dt className="field-label">api key</dt>
            <dd className="mono tnum truncate text-[10.5px] text-[var(--ink-soft)]" title={row.api_key_masked ?? "never stored"}>
              {row.api_key_masked ?? "—"}
            </dd>
          </div>
          <div className="min-w-0">
            <dt className="field-label">api secret</dt>
            <dd className="mono tnum truncate text-[10.5px] text-[var(--ink-soft)]" title={row.api_secret_masked ?? "never stored"}>
              {row.api_secret_masked ?? "—"}
            </dd>
          </div>
        </dl>

        <div className="flex flex-col gap-1 border-t border-[var(--line)] pt-2">
          <div className="row-between gap-2">
            <span className="field-label">signing</span>
            <span className="mono text-[10.5px]">{text(signing.algorithm, "—")}</span>
          </div>
          <div className="row-between gap-2">
            <span className="field-label">dry-run signature</span>
            <StatusPill
              value={signingVerified === true ? "VERIFIED" : signingVerified === false ? "UNVERIFIED" : "—"}
              tone={passTone(signingVerified)}
            />
          </div>
          <div className="row-between gap-2">
            <span className="field-label">credential dry-run</span>
            <StatusPill
              value={credentialDryRun === true ? "VERIFIED" : credentialDryRun === false ? "UNVERIFIED" : "—"}
              tone={passTone(credentialDryRun)}
            />
          </div>
          <div className="row-between gap-2">
            <span className="field-label">public fingerprint</span>
            <CopyHash value={signing.public_fingerprint} label={`copy the ${text(row.venue, "venue")} Ed25519 public fingerprint`} />
          </div>
        </div>

        <div className="flex flex-col gap-1 border-t border-[var(--line)] pt-2">
          <div className="row-between gap-2">
            <span className="field-label">feed mode</span>
            <span className="mono text-[10.5px]">{text(feed.mode, "—")}</span>
          </div>
          <div className="chip-row">
            <StatusPill
              value={feedDual === true ? "dual vendor" : feedDual === false ? "single vendor" : "vendor scope unknown"}
              tone={feedDual === true ? "ok" : feedDual === false ? "bad" : "neutral"}
            />
            <StatusPill value={text(feed.health, "—")} tone={toneOfLabel(text(feed.health, ""))} />
          </div>
        </div>

        <button
          type="button"
          className="btn btn-sm w-full"
          onClick={runPing}
          disabled={ping.busy}
          aria-label={`Ping ${text(row.venue, "venue")} and run the Ed25519 signing self-test`}
        >
          {ping.busy ? "Pinging…" : "Ping & Ed25519 Signing Self-Test"}
        </button>

        {ping.error ? <ErrorBlock message={ping.error} onRetry={runPing} /> : null}

        {ping.receipt && showReceipt ? (
          <div className="rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] p-2">
            <div className="row-between mb-1.5">
              <span className="eyebrow">self-test receipt</span>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={() => setShowReceipt(false)}
                aria-label={`Hide the ${text(row.venue, "venue")} self-test receipt`}
              >
                Hide
              </button>
            </div>
            <div className="flex flex-col gap-1">
              <div className="row-between gap-2">
                <span className="text-[10.5px] text-[var(--muted)]">adapter importable</span>
                <StatusPill
                  value={pingAdapter.importable === true ? "YES" : "NO"}
                  tone={passTone(flagOrNull(pingAdapter.importable))}
                  title={text(pingAdapter.reason, "adapter")}
                />
              </div>
              <div className="row-between gap-2">
                <span className="text-[10.5px] text-[var(--muted)]">signing verified</span>
                <StatusPill
                  value={pingSigningVerified === true ? "VERIFIED" : pingSigningVerified === false ? "FAILED" : "—"}
                  tone={passTone(pingSigningVerified)}
                />
              </div>
              <div className="row-between gap-2">
                <span className="text-[10.5px] text-[var(--muted)]">credential</span>
                <StatusPill
                  value={text(pingCredential.status, "UNSET")}
                  tone={toneOfLabel(text(pingCredential.status, "UNSET"))}
                />
              </div>
              <div className="row-between gap-2">
                <span className="text-[10.5px] text-[var(--muted)]">raw secret returned</span>
                <StatusPill
                  value={rawSecretReturned === false ? "NO RAW SECRET" : rawSecretReturned === true ? "SECRET RETURNED" : "NOT ASSERTED"}
                  tone={rawSecretReturned === false ? "ok" : rawSecretReturned === true ? "bad" : "neutral"}
                />
              </div>
              <div className="row-between gap-2">
                <span className="text-[10.5px] text-[var(--muted)]">latency budget</span>
                <span className="mono tnum text-[10.5px]">{latency(pingReceipt.latency_budget_ms)}</span>
              </div>
            </div>
          </div>
        ) : null}
      </div>
    </SpecularCard>
  );
}

/* ------------------------------ grouping ------------------------------ */

function deriveGroups(venues: BrokerVenueRow[]): SiloGroup[] {
  const bySilo = new Map<string, BrokerVenueRow[]>();
  for (const row of venues) {
    const key = text(row.silo, "UNASSIGNED");
    const list = bySilo.get(key);
    if (list) list.push(row);
    else bySilo.set(key, [row]);
  }
  return Array.from(bySilo.entries()).map(([silo, list]) => ({
    silo,
    market: text(list[0]?.market, "—"),
    configured: list.filter((row) => row.credential_status === "CONFIGURED").length,
    venues: list,
  }));
}
