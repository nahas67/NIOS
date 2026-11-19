/**
 * `/brokers` — 12-venue broker hub, SEBI egress/compliance, dual-vendor feed
 * circuit breakers, key vault custody and the adapter probe surface.
 *
 * Ring 3 never reads a broker key: every mask on this page is the `***last4`
 * form Ring 0 already produced.
 */

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
  KeyValue,
  PageHeader,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  StatusDot,
  Table,
} from "../components/ui";
import { SILOS } from "../config/nav";
import { api, errorMessage } from "../lib/api";
import { asArray, asRecord, clockNs, duration, int, num, text } from "../lib/format";
import { numberAt, rowsOf } from "../lib/payload";
import { useTicker } from "../state/session";
import type { ApiNote, PageProps } from "./types";

const LOOPBACK_ALLOWLIST = [
  { ip: "127.0.0.1", label: "dhan · IPv4 loopback" },
  { ip: "::1", label: "dhan · IPv6 loopback" },
  { ip: "127.0.0.1", label: "zerodha · IPv4 loopback" },
  { ip: "::1", label: "zerodha · IPv6 loopback" },
];
const ALGO_ID_PREFIX = "NIOS_";
const ALGO_ID_DEFAULT = "NIOS_GENERIC_ALGO";
const OPS_CAP = 10;
const TOTP_HOUR_UTC = 3;

function breakerTone(state: string): "bull" | "accent" | "bear" | "muted" {
  if (state === "CLOSED") return "bull";
  if (state === "HALF_OPEN") return "accent";
  if (state === "OPEN") return "bear";
  return "muted";
}

export function BrokersPage({ mode }: PageProps) {
  const [brokers, setBrokers] = useState<Record<string, unknown>>({});
  const [connectors, setConnectors] = useState<Record<string, unknown>>({});
  const [vault, setVault] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<ApiNote | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [venue, setVenue] = useState("binance");
  const [probe, setProbe] = useState<Record<string, unknown> | null>(null);
  const now = useTicker(1000);

  const load = useCallback(async () => {
    try {
      const [brokerPayload, connectorPayload, vaultPayload] = await Promise.all([
        api.get<unknown>("/cockpit/brokers"),
        api.get<unknown>("/connectors/status"),
        api.get<unknown>("/control/vault/status"),
      ]);
      setBrokers(asRecord(brokerPayload));
      setConnectors(asRecord(connectorPayload));
      setVault(asRecord(vaultPayload));
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

  const venues = useMemo(() => rowsOf(brokers.venues, ["venues"]), [brokers]);
  const counts = useMemo(() => asRecord(brokers.counts), [brokers]);
  const weight = useMemo(() => asRecord(connectors.weight), [connectors]);
  const venueOptions = useMemo(() => {
    const supported = asArray(brokers.venues_supported)
      .map((entry) => text(entry, ""))
      .filter(Boolean);
    const source =
      supported.length > 0 ? supported : venues.map((row) => text(row.venue, "")).filter(Boolean);
    return source.map((id) => ({ value: id, label: id }));
  }, [brokers, venues]);
  const feed = useMemo(() => asRecord(brokers.feed), [brokers]);
  const feedSilos = useMemo(() => asRecord(asRecord(feed.last_cycle).silos), [feed]);
  const connectorMode = text(connectors.connector_mode, text(feed.feed_mode, "OFFLINE_REPLAY"));
  const modeTone =
    connectorMode === "LIVE_VENUE" ? "bear" : connectorMode === "SANDBOX_TESTNET" ? "accent" : "neutral";

  const vaultKeys =
    numberAt(vault, ["broker_credentials_stored"], 0) + numberAt(vault, ["llm_keys_stored"], 0);
  const lastRotation = asRecord(vault.last_rotation);
  const rotatedAtNs = numberAt(lastRotation, ["rotated_at_ns"], 0);
  const rotationAgeSeconds = rotatedAtNs > 0 ? Math.max(0, (now - rotatedAtNs / 1e6) / 1000) : 0;

  const totpTtl = useMemo(() => {
    const next = new Date(now);
    next.setUTCHours(TOTP_HOUR_UTC, 0, 0, 0);
    if (next.getTime() <= now) next.setUTCDate(next.getUTCDate() + 1);
    return Math.max(0, Math.floor((next.getTime() - now) / 1000));
  }, [now]);

  const probeVenue = async (kind: "ping" | "handshake") => {
    setNote(null);
    setBusy(kind);
    try {
      if (kind === "handshake" && mode !== "LIVE") {
        throw new Error("LIVE-GUARDED PROBE — a LIVE-elevated session is required");
      }
      const payload =
        kind === "ping"
          ? await api.post<unknown>("/control/brokers/ping", { venue })
          : await api.post<unknown>("/control/brokers/handshake", { venue, dry_run: true });
      setProbe(asRecord(payload));
      setNote({
        tone: "bull",
        message: `${kind.toUpperCase()} · ${venue} · receipt captured (write-only credentials, no key echoed)`,
      });
      await load();
    } catch (caught) {
      setNote({ tone: "bear", message: `${kind.toUpperCase()} · ${venue} · ${errorMessage(caught)}` });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="BROKERS"
        subtitle="12 venues · static egress allowlist · dual-vendor feed breakers · zero-downtime key custody"
        badges={
          <>
            <Badge tone={mode === "LIVE" ? "bear" : "accent"}>{mode}</Badge>
            <Badge tone={modeTone} mono>
              {connectorMode}
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

      <StatGrid columns={5}>
        <Stat
          label="Venues"
          value={int(venues.length || asArray(brokers.venues_supported).length)}
          hint="broker adapters"
        />
        <Stat
          label="Configured"
          value={int(numberAt(counts, ["configured"], 0))}
          hint="credential sealed in Ring 0"
          tone="bull"
        />
        <Stat
          label="Unset"
          value={int(numberAt(counts, ["unset"], 0))}
          hint="no credential on file"
          tone="muted"
        />
        <Stat
          label="Vault Keys"
          value={int(vaultKeys)}
          hint={`${int(vault.broker_venues_stored)} venues · write-only`}
        />
        <Stat
          label="Connector Calls"
          value={int(connectors.calls)}
          hint={`${int(connectors.rejections)} rejections`}
        />
      </StatGrid>

      <Card
        title="12-Venue Adapter Matrix"
        subtitle="MASK is always the Ring 0 ***last4 form · the plaintext key never reaches Ring 3"
        icon="brokers"
        dense
        actions={
          <Badge tone="muted" mono>
            {text(connectors.money_units, "integer_micros")}
          </Badge>
        }
      >
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-6 text-[11px] text-ink-3">
            <Spinner /> LOADING VENUE MATRIX…
          </div>
        ) : venues.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="NO VENUE ROWS"
              hint="The hub reports adapters once the silo registry is attached. An offline boot still lists the 12 supported venues."
            />
          </div>
        ) : (
          <Table
            dense
            columns={["VENUE", "SILO", "MASK", "MODE", "CONNECTED", "LATENCY", "DRY RUN", "LAST ERROR"]}
          >
            {venues.map((row) => {
              const connected =
                row.adapter_importable === true && text(row.credential_status, "") === "CONFIGURED";
              const siloReport = asRecord(feedSilos[text(row.silo, "")]);
              const lastError = text(row.adapter_reason, text(siloReport.last_error, "—"));
              return (
                <Row key={text(row.venue, "venue")}>
                  <Cell>
                    <div className="font-semibold text-ink">{text(row.venue)}</div>
                    <div className="font-mono text-[9.5px] text-ink-3">{text(row.adapter_module, "—")}</div>
                  </Cell>
                  <Cell className="text-[10px]">{text(row.silo)}</Cell>
                  <Cell mono tone={text(row.api_key_masked, "").length > 0 ? undefined : "muted"}>
                    {text(row.api_key_masked, "UNSET")}
                  </Cell>
                  <Cell>
                    <Badge tone={modeTone} mono>
                      {connectorMode}
                    </Badge>
                  </Cell>
                  <Cell align="center">
                    <span className="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-[0.08em] text-ink-2">
                      <StatusDot tone={connected ? "bull" : "muted"} pulse={connected} />
                      {connected ? "UP" : "DOWN"}
                    </span>
                  </Cell>
                  <Cell align="right" tone="muted">
                    —
                  </Cell>
                  <Cell align="center">
                    <Badge tone={row.dry_run_verified === true ? "bull" : "muted"}>
                      {row.dry_run_verified === true ? "VERIFIED" : "PENDING"}
                    </Badge>
                  </Cell>
                  <Cell tone={lastError === "—" ? "muted" : "bear"} className="text-[10px]">
                    {lastError}
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
        <div className="border-t border-hairline px-2.5 py-1.5 text-[10px] text-ink-3">
          LATENCY stays blank until a venue handshake receipt exists for that adapter; a fabricated
          millisecond number would be indistinguishable from a measured one.
        </div>
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card
          title="SEBI Compliance"
          subtitle="Static egress allowlist · exchange algo id · 10 OPS ceiling · 03:00 UTC TOTP renewal"
          icon="shield"
          dense
        >
          <Table dense columns={["IP", "LABEL", "STATE"]}>
            {LOOPBACK_ALLOWLIST.map((row, index) => (
              <Row key={`${row.ip}-${row.label}-${index}`}>
                <Cell mono>{row.ip}</Cell>
                <Cell className="text-[10px]">{row.label}</Cell>
                <Cell align="right">
                  <Badge tone="bull">WHITELISTED</Badge>
                </Cell>
              </Row>
            ))}
          </Table>
          <div className="border-t border-hairline px-2.5 py-2">
            <KeyValue
              columns={2}
              items={[
                { label: "Algo ID Prefix", value: ALGO_ID_PREFIX, tone: "accent" },
                { label: "Algo ID Default", value: ALGO_ID_DEFAULT },
                { label: "OPS Ceiling", value: `${int(OPS_CAP)} OPS`, tone: "bull" },
                { label: "TOTP Renewal", value: "03:00 UTC", tone: "accent" },
                { label: "TOTP TTL", value: duration(totpTtl), tone: totpTtl > 3600 ? "bull" : "bear" },
                { label: "Silo Scope", value: SILOS[0].id },
              ]}
            />
            <div className="mt-2 flex items-center gap-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
              <StatusDot tone={totpTtl > 3600 ? "bull" : "accent"} pulse />
              next renewal in {duration(totpTtl)} · allowlist above mirrors the Ring 0 loopback default until
              a static IP is registered.
            </div>
          </div>
        </Card>

        <Card
          title="Dual-Vendor Feed Circuit Breakers"
          subtitle="Law 1 · no single venue is trusted · divergence > 1.50% must trip the breaker"
          icon="wifi"
          dense
          actions={
            <Badge tone="muted" mono>
              {text(feed.feed_mode, "PAPER_AUTOPILOT")}
            </Badge>
          }
        >
          <Table dense columns={["ASSET CLASS", "PRIMARY", "SECONDARY", "DIVERGENCE", "LAG", "BREAKER"]}>
            {SILOS.map((silo) => {
              const report = asRecord(feedSilos[silo.id]);
              const vendors = asArray(report.venues).map((entry) => text(entry, ""));
              const divergencePct = numberAt(report, ["max_divergence_pct"], 0);
              const rejected = numberAt(report, ["rejected"], 0);
              const live = numberAt(report, ["live"], 0);
              const feedMode = text(report.mode, text(asRecord(feed.silo_modes)[silo.id], ""));
              const hasReport = Object.keys(report).length > 0;
              const breaker = !hasReport
                ? "NO TELEMETRY"
                : rejected > 0 && live === 0
                  ? "OPEN"
                  : rejected > 0 || feedMode === "SYNTHETIC_FALLBACK"
                    ? "HALF_OPEN"
                    : "CLOSED";
              return (
                <Row key={silo.id} tone={breaker === "OPEN" ? "bear" : undefined}>
                  <Cell>
                    <div className="font-semibold text-ink">{silo.short}</div>
                    <div className="text-[9.5px] text-ink-3">{silo.id}</div>
                  </Cell>
                  <Cell mono className="text-[10px]">
                    {vendors[0] ?? "—"}
                  </Cell>
                  <Cell mono className="text-[10px]">
                    {vendors[1] ?? "—"}
                  </Cell>
                  <Cell align="right" tone={divergencePct * 100 > 150 ? "bear" : "bull"}>
                    {hasReport ? `${num(divergencePct * 100, 2)} bps` : "—"}
                  </Cell>
                  <Cell align="right" tone="muted">
                    —
                  </Cell>
                  <Cell align="center">
                    <Badge tone={breakerTone(breaker)}>{breaker}</Badge>
                  </Cell>
                </Row>
              );
            })}
          </Table>
          <div className="border-t border-hairline px-2.5 py-1.5 text-[10px] text-ink-3">
            feed mode {text(feed.feed_mode, "PAPER_AUTOPILOT")} · silos{" "}
            {int(asArray(connectors.silos).length)} · weight {int(numberAt(weight, ["BINANCE"], 0))}/
            {int(connectors.weight_budget)}
          </div>
        </Card>
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card
          title="Auth Vault Custody"
          subtitle="Fernet at rest · Ed25519 for signing · rotation is zero-downtime and zero-loss"
          icon="key"
        >
          <KeyValue
            columns={2}
            items={[
              {
                label: "State",
                value: text(vault.status, "UNAVAILABLE"),
                tone: vault.status === "READY" ? "bull" : "bear",
              },
              { label: "Algorithm", value: "Fernet / Ed25519" },
              { label: "Key Version", value: int(vault.key_version) },
              { label: "Signing Version", value: int(vault.signing_key_version) },
              { label: "Broker Keys", value: int(vault.broker_credentials_stored) },
              { label: "LLM Keys", value: int(vault.llm_keys_stored) },
              {
                label: "Rotation Age",
                value: rotatedAtNs > 0 ? duration(rotationAgeSeconds) : "NEVER ROTATED",
              },
              {
                label: "Rotation Pending",
                value: text(vault.rotation_pending_recovery, "false"),
                tone: vault.rotation_pending_recovery === true ? "bear" : "bull",
              },
            ]}
          />
          <div className="mt-2 font-mono text-[9.5px] text-ink-3">
            fingerprint {text(vault.ed25519_public_fingerprint, "—")} · raw secrets exposed{" "}
            {text(vault.raw_secrets_exposed, "false")}
          </div>
        </Card>

        <Card
          title="Adapter Probe"
          subtitle="Ping is read-only; the handshake proves the signing path without moving capital"
          icon="server"
        >
          <div className="flex flex-wrap items-end gap-2">
            <Field label="Venue" className="w-56">
              <SelectInput value={venue} onChange={setVenue} options={venueOptions} />
            </Field>
            <Button
              variant="subtle"
              icon="activity"
              disabled={busy !== null}
              onClick={() => void probeVenue("ping")}
            >
              {busy === "ping" ? "PINGING…" : "Ping"}
            </Button>
            <Button
              variant="primary"
              icon="shield"
              disabled={busy !== null || mode !== "LIVE"}
              onClick={() => void probeVenue("handshake")}
            >
              {busy === "handshake" ? "HANDSHAKING…" : "Handshake"}
            </Button>
          </div>
          {mode === "LIVE" ? null : (
            <div className="mt-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
              LIVE-GUARDED PROBE · the handshake needs a LIVE-elevated session; ping is read-only and reports
              the guard status when the tier is missing.
            </div>
          )}
          <div className="mt-2">
            {probe === null ? (
              <EmptyState
                title="NO PROBE RECEIPT"
                hint="Every probe returns a masked receipt; no credential is ever echoed."
              />
            ) : (
              <JsonBlock value={probe} />
            )}
          </div>
        </Card>
      </div>

      <Card title="Signing Dry Run" subtitle="Ed25519 + Fernet warm-up receipt from Ring 0" icon="key">
        <div className="grid gap-3 md:grid-cols-2">
          <KeyValue
            columns={1}
            items={[
              {
                label: "Verified",
                value: text(asRecord(brokers.signing_dry_run).verified, "—"),
                tone: asRecord(brokers.signing_dry_run).verified === true ? "bull" : "bear",
              },
              { label: "Algorithm", value: text(asRecord(brokers.signing_dry_run).algorithm, "—") },
              {
                label: "Fingerprint",
                value: text(asRecord(brokers.signing_dry_run).public_fingerprint, "—"),
              },
              { label: "Checked", value: clockNs(brokers.generated_at_ns) },
            ]}
          />
          <JsonBlock value={asRecord(brokers.signing_dry_run)} />
        </div>
      </Card>
    </div>
  );
}

export default BrokersPage;
