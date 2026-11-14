/**
 * Page 15 — Control room & air-gapped MCP console.
 *
 * The go-live certificate, the write-only credential vault, zero-downtime key
 * rotation, the LLM pool probe and the read-only MCP bridge. Every mutating
 * surface is gated by an explicit confirm modal; no panel returns a raw secret.
 */

import { useState } from "react";
import type { ReactNode } from "react";
import {
  ConfirmDangerModal,
  CopyHash,
  EmptyCleanSlate,
  ErrorBlock,
  Freshness,
  GlassPanel,
  InlineBar,
  LoadingBlock,
  MetricTile,
  passTone,
  SectionHeader,
  StatusPill,
  TabularValue,
} from "@/design-system/primitives";
import { api, authApi } from "@/lib/api";
import { int, latency, num, record, text, toneOfLabel, utc } from "@/lib/format";
import { navigate, pagePath, useLocation } from "@/lib/router";
import type { JsonRecord, McpCallResult, McpPageData, PreflightCertificate, PreflightGate } from "@/lib/types";
import { useAction, useResource } from "@/lib/use-api";

/* ------------------------------- helpers ------------------------------- */

const SILO_IDS = ["CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"] as const;
const DAILY_TOKEN_CAP_USD = 3.1;
const LLM_HARD_CAP_MS = 250;

const VENUE_SUGGESTIONS: Array<{ id: string; label: string }> = [
  { id: "binance", label: "BINANCE" },
  { id: "okx", label: "OKX" },
  { id: "bybit", label: "BYBIT" },
  { id: "zerodha", label: "ZERODHA" },
  { id: "dhan", label: "DHAN" },
  { id: "angel", label: "ANGEL" },
  { id: "upstox", label: "UPSTOX" },
  { id: "interactive_brokers", label: "IBKR" },
  { id: "vnpy_mt5", label: "MT5" },
];

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

function firstOf(source: Record<string, unknown>, keys: string[]): unknown {
  for (const key of keys) {
    const value = source[key];
    if (value !== null && value !== undefined) return value;
  }
  return null;
}

function firstOfAny(sources: Array<Record<string, unknown>>, keys: string[]): unknown {
  for (const source of sources) {
    const value = firstOf(source, keys);
    if (value !== null && value !== undefined) return value;
  }
  return null;
}

/** Accept a canonical venue id or its displayed label (`IBKR` → `interactive_brokers`). */
function canonicalVenue(raw: string): string {
  const needle = raw.trim().toLowerCase();
  if (needle === "") return "";
  const match = VENUE_SUGGESTIONS.find(
    (venue) => venue.id === needle || venue.label.toLowerCase() === needle,
  );
  return match ? match.id : needle;
}

function spendOf(source: Record<string, unknown>): number | null {
  const direct = asNumber(
    firstOf(source, ["spend_usd", "daily_spend_usd", "token_spend_usd", "total_spend_usd"]),
  );
  if (direct !== null) return direct;
  for (const key of ["ledger", "tokens", "budget", "token_ledger"]) {
    const nested = record(source[key]);
    const value = asNumber(
      firstOf(nested, ["spend_usd", "daily_spend_usd", "total_spend_usd", "spent_usd"]),
    );
    if (value !== null) return value;
  }
  return null;
}

/* --------------------------------- page --------------------------------- */

export function ControlPage(): ReactNode {
  const location = useLocation();
  const preflight = useResource<PreflightCertificate>("preflight", () => api.preflight(), 20000);
  const vault = useResource<JsonRecord>("vault-status", () => api.vaultStatus(), 60000);
  const mcp = useResource<McpPageData>("mcp", () => api.mcp(), 30000);

  return (
    <div>
      <SectionHeader
        eyebrow={`ring3 · sovereign · ${location.workspace} workspace`}
        title="Control Room & MCP"
        subtitle="The 22-gate go-live certificate, the write-only Ring 0 credential vault, zero-downtime key rotation, the local LLM pool probe and the air-gapped read-only MCP console."
        actions={
          <>
            <Freshness generatedAtNs={preflight.data?.generated_at_ns} onRefresh={preflight.reload} busy={preflight.loading} />
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => navigate(pagePath("brokers", location.workspace))}
              aria-label="Open the 12-venue broker hub"
            >
              12-venue broker hub
            </button>
          </>
        }
      />

      <div className="flex flex-col gap-[14px]">
        <PreflightCertificatePanel
          certificate={preflight.data}
          loading={preflight.loading}
          error={preflight.error}
          onReload={preflight.reload}
        />

        <div className="split-2">
          <div className="flex flex-col gap-[14px]">
            <CredentialVaultPanel />
            <KeyRotationPanel
              status={vault.data}
              loading={vault.loading}
              error={vault.error}
              onReload={vault.reload}
            />
            <LlmPoolPanel />
          </div>
          <div className="flex flex-col gap-[14px]">
            <McpConsolePanel data={mcp.data} loading={mcp.loading} error={mcp.error} onReload={mcp.reload} />
          </div>
        </div>

        <FactoryResetPanel />
      </div>
    </div>
  );
}

/* ----------------------------- pre-flight ----------------------------- */

interface PreflightPanelProps {
  certificate: PreflightCertificate | null;
  loading: boolean;
  error: string | null;
  onReload: () => void;
}

function PreflightCertificatePanel({ certificate, loading, error, onReload }: PreflightPanelProps): ReactNode {
  const run = useAction<PreflightCertificate>();
  const [showGates, setShowGates] = useState(false);

  const gates: PreflightGate[] = certificate?.gates ?? [];
  const certificateRecord = record(certificate);
  const failedIds = certificate?.failed_gate_ids ?? [];
  const warnedIds = certificate?.warned_gate_ids ?? [];
  const ready = certificate?.ready_for_live_cutover === true;
  const fullSmoke = flagOrNull(certificateRecord.full_smoke);
  const receipt = run.receipt;

  const execute = (): void => {
    void run.run(() => api.runPreflight(false)).then((fresh) => {
      if (fresh) onReload();
    });
  };

  return (
    <GlassPanel
      title="22-Gate Pre-Flight Go-Live Readiness Certificate"
      eyebrow="sec_016 · nothing reaches LIVE until every invariant is certified"
      actions={
        <button
          type="button"
          className="btn btn-sm btn-accent"
          onClick={execute}
          disabled={run.busy}
          aria-label="Run the 22-gate pre-flight go-live certificate"
        >
          {run.busy ? "Certifying 22 gates…" : "RUN 22-GATE PRE-FLIGHT"}
        </button>
      }
    >
      {loading && !certificate ? <LoadingBlock rows={3} label="Certifying the 22 go-live gates" /> : null}
      {error ? <ErrorBlock message={error} onRetry={onReload} /> : null}
      {run.error ? <ErrorBlock message={run.error} onRetry={execute} /> : null}

      {certificate ? (
        <div className="flex flex-col gap-3">
          <div className="row-between flex-wrap items-start gap-4">
            <div className="min-w-0">
              <p className="eyebrow">release</p>
              <p className="mono text-[15px] font-semibold">{text(certificate.release, "—")}</p>
              <p className="mono mt-1 text-[10px] tracking-[0.06em] text-[var(--dim)] uppercase">
                signer {text(certificateRecord.signer, "—")} · scope {text(certificateRecord.silo_scope, "ALL")} ·{" "}
                {fullSmoke === true ? "full smoke" : "fast path"}
              </p>
            </div>
            <div className="flex flex-none flex-col items-end gap-1.5">
              <StatusPill
                value={ready ? "READY FOR CUTOVER" : "NOT READY"}
                tone={ready ? "ok" : "bad"}
                title={`ready_for_live_cutover: ${text(certificate.ready_for_live_cutover)}`}
              />
              <div className="row gap-1.5">
                <span className="field-label">certificate</span>
                <CopyHash value={certificate.certificate_sha256} label="copy the pre-flight certificate sha256" />
              </div>
            </div>
          </div>

          <div className="grid-auto-180">
            <MetricTile
              label="Passed"
              value={<TabularValue value={certificate.passed_gates} kind="int" />}
              tone="pos"
              hint="gates certified"
            />
            <MetricTile
              label="Failed"
              value={<TabularValue value={certificate.failed_gates} kind="int" />}
              tone={asNumber(certificate.failed_gates) === 0 ? "dim" : "neg"}
              hint="must be 0"
            />
            <MetricTile
              label="Warned"
              value={<TabularValue value={certificate.warned_gates} kind="int" />}
              tone={asNumber(certificate.warned_gates) === 0 ? "dim" : "warn"}
              hint="non-blocking"
            />
            <MetricTile
              label="Total gates"
              value={<TabularValue value={certificate.total_gates} kind="int" />}
              hint={`${gates.length} rows returned`}
            />
          </div>

          <div className="chip-row">
            <StatusPill
              value={ready ? "confirm_live may be authorised" : "confirm_live blocked"}
              tone={ready ? "ok" : "bad"}
            />
            <span className="pill mono">
              served from {text(certificate.served_from, "—")}
            </span>
            <span className="pill mono tnum">checked {utc(certificate.generated_at_ns)} UTC</span>
            {receipt ? (
              <span className="pill pill-accent mono">
                last run · <TabularValue value={receipt.passed_gates} kind="int" /> passed ·{" "}
                <TabularValue value={receipt.failed_gates} kind="int" /> failed ·{" "}
                <TabularValue value={receipt.warned_gates} kind="int" /> warned
              </span>
            ) : null}
          </div>

          {failedIds.length > 0 ? (
            <div className="flex flex-col gap-1 rounded-[var(--r-sm)] border border-[rgba(251,113,133,0.35)] bg-[var(--neg-soft)] p-2">
              <p className="eyebrow text-[var(--neg)]">failed gates</p>
              <div className="chip-row">
                {failedIds.map((id) => (
                  <StatusPill key={id} value={id} tone="bad" />
                ))}
              </div>
            </div>
          ) : null}

          {warnedIds.length > 0 ? (
            <div className="flex flex-col gap-1">
              <p className="eyebrow">warned gates</p>
              <div className="chip-row">
                {warnedIds.map((id) => (
                  <StatusPill key={id} value={id} tone="warn" />
                ))}
              </div>
            </div>
          ) : null}

          {text(certificate.next_action, "") !== "" ? (
            <div className="rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] px-3 py-2">
              <p className="eyebrow">next action</p>
              <p className="mt-0.5 text-[11.5px] text-[var(--ink-soft)]">{text(certificate.next_action)}</p>
            </div>
          ) : null}

          <div className="row-between border-t border-[var(--line)] pt-2">
            <p className="mono text-[10px] tracking-[0.08em] text-[var(--dim)] uppercase">
              <TabularValue value={gates.length} kind="int" /> of{" "}
              <TabularValue value={certificate.total_gates} kind="int" /> gate rows in payload
            </p>
            <button
              type="button"
              className="btn btn-sm btn-ghost"
              onClick={() => setShowGates((value) => !value)}
              aria-expanded={showGates}
              aria-controls="preflight-gate-table"
              aria-label={showGates ? "Collapse the 22-gate audit table" : "Expand the 22-gate audit table"}
            >
              {showGates ? "Hide gate audit" : "Show gate audit"}
            </button>
          </div>

          {showGates ? (
            gates.length === 0 ? (
              <EmptyCleanSlate
                title="The certificate carried no gate rows"
                body="The pre-flight runner always returns one row per gate; an empty array means the certificate was replayed from an incomplete cache."
              />
            ) : (
              <div className="scroll-x" id="preflight-gate-table">
                <table className="table table-compact">
                  <caption className="sr-only">
                    All pre-flight gates with identifier, label, status, detail and duration
                  </caption>
                  <thead>
                    <tr>
                      <th scope="col">Gate</th>
                      <th scope="col">Label</th>
                      <th scope="col">Status</th>
                      <th scope="col">Detail</th>
                      <th scope="col" className="num">
                        Duration
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {gates.map((gate, index) => (
                      <tr key={`${text(gate.gate, "gate")}-${index}`}>
                        <td className="mono text-[10.5px]">{text(gate.gate, "—")}</td>
                        <td className="text-[11.5px]">{text(gate.label, "—")}</td>
                        <td>
                          <StatusPill value={text(gate.status, "UNKNOWN")} tone={toneOfLabel(text(gate.status, ""))} />
                        </td>
                        <td className="text-[10.5px] text-[var(--dim)]">{text(gate.detail, "—")}</td>
                        <td className="num">
                          <span className="mono tnum">{num(gate.duration_ms, 1)} ms</span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )
          ) : null}
        </div>
      ) : null}

      {!certificate && !loading && !error ? (
        <EmptyCleanSlate
          title="No readiness certificate"
          body="The gateway did not return a 22-gate certificate. Run the pre-flight once scripts/preflight_golive.py is available on this host."
          action={
            <button
              type="button"
              className="btn btn-sm"
              onClick={execute}
              disabled={run.busy}
              aria-label="Run the pre-flight certificate now"
            >
              Run pre-flight
            </button>
          }
        />
      ) : null}
    </GlassPanel>
  );
}

/* ------------------------- write-only credential vault ------------------------- */

function CredentialVaultPanel(): ReactNode {
  const action = useAction<JsonRecord>();
  const [silo, setSilo] = useState<string>(SILO_IDS[0]);
  const [venue, setVenue] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiSecret, setApiSecret] = useState("");

  const resolvedVenue = canonicalVenue(venue);
  const valid = resolvedVenue !== "" && apiKey.trim() !== "" && apiSecret.trim() !== "";

  const receipt = record(action.receipt);
  const maskedBlock = record(firstOf(receipt, ["masked"]));
  const maskedKey = text(
    firstOfAny([receipt, maskedBlock], ["api_key_masked", "api_key"]),
    "—",
  );
  const maskedSecret = text(
    firstOfAny([receipt, maskedBlock], ["api_secret_masked", "api_secret"]),
    "—",
  );
  const dryRun = record(firstOf(receipt, ["dry_run"]));
  const dryRunVerified = flagOrNull(receipt.dry_run_verified);
  const socketOpened = flagOrNull(dryRun.socket_opened);
  const orderPlaced = flagOrNull(dryRun.order_placed);

  const submit = (): void => {
    if (!valid) return;
    void action
      .run(() =>
        api.brokerCredentials({
          silo,
          venue: resolvedVenue,
          api_key: apiKey.trim(),
          api_secret: apiSecret.trim(),
        }),
      )
      .then((result) => {
        if (result) {
          setApiKey("");
          setApiSecret("");
        }
      });
  };

  return (
    <GlassPanel
      title="Write-Only Broker Credential Vault"
      eyebrow="sec_011 · Ring 0 custody"
      actions={
        <StatusPill value="write-only" tone="accent" title="write_only · Ring 0 only · never returned" />
      }
    >
      <div className="flex flex-col gap-3">
        <p className="text-[11.5px] text-[var(--dim)]">
          The plaintext key and secret cross the ring boundary once, are Fernet-encrypted inside
          <span className="mono"> ring0_vault/silos/key_vault</span> and are never returned to Ring 3.
        </p>

        <div className="grid grid-cols-2 gap-2">
          <label className="field">
            <span className="field-label">Silo</span>
            <select
              className="select"
              value={silo}
              onChange={(event) => setSilo(event.target.value)}
              aria-label="Credential target silo"
            >
              {SILO_IDS.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Venue</span>
            <input
              className="input mono"
              value={venue}
              onChange={(event) => setVenue(event.target.value)}
              list="credential-venue-options"
              placeholder="BINANCE"
              aria-label="Credential target venue"
              autoComplete="off"
            />
            <datalist id="credential-venue-options">
              {VENUE_SUGGESTIONS.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))}
            </datalist>
            <span className="mono text-[9px] text-[var(--dim)]">
              {resolvedVenue === "" ? "canonical venue id" : `sends ${resolvedVenue}`}
            </span>
          </label>
        </div>

        <label className="field">
          <span className="field-label">API key</span>
          <input
            className="input mono"
            value={apiKey}
            onChange={(event) => setApiKey(event.target.value)}
            autoComplete="off"
            spellCheck={false}
            aria-label="Broker API key"
          />
        </label>
        <label className="field">
          <span className="field-label">API secret</span>
          <input
            className="input mono"
            type="password"
            value={apiSecret}
            onChange={(event) => setApiSecret(event.target.value)}
            autoComplete="new-password"
            spellCheck={false}
            aria-label="Broker API secret"
          />
        </label>

        <div className="row-between">
          <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--dim)] uppercase">
            write-only · Ring 0 only · never returned
          </span>
          <button
            type="button"
            className="btn btn-sm btn-accent"
            onClick={submit}
            disabled={action.busy || !valid}
            aria-label="Store the broker credential in the write-only Ring 0 vault"
          >
            {action.busy ? "Encrypting…" : "Store credential"}
          </button>
        </div>

        {action.error ? <ErrorBlock message={action.error} onRetry={submit} /> : null}

        {action.receipt ? (
          <div className="flex flex-col gap-2 rounded-[var(--r-sm)] border border-[var(--line)] bg-[var(--panel-sunken)] p-2.5">
            <div className="chip-row">
              <StatusPill
                value={dryRunVerified === true ? "dry-run verified" : "dry-run unverified"}
                tone={passTone(dryRunVerified)}
              />
              <StatusPill
                value={socketOpened === false ? "socket opened: NO" : socketOpened === true ? "socket opened: YES" : "socket: unknown"}
                tone={socketOpened === false ? "ok" : socketOpened === true ? "bad" : "neutral"}
              />
              <StatusPill
                value={orderPlaced === false ? "order placed: NO" : orderPlaced === true ? "order placed: YES" : "order: unknown"}
                tone={orderPlaced === false ? "ok" : orderPlaced === true ? "bad" : "neutral"}
              />
            </div>
            <dl className="grid grid-cols-2 gap-2">
              <div className="min-w-0">
                <dt className="field-label">api key</dt>
                <dd className="mono tnum truncate text-[10.5px]">{maskedKey}</dd>
              </div>
              <div className="min-w-0">
                <dt className="field-label">api secret</dt>
                <dd className="mono tnum truncate text-[10.5px]">{maskedSecret}</dd>
              </div>
            </dl>
            <p className="mono text-[9.5px] tracking-[0.06em] text-[var(--dim)] uppercase">
              {text(receipt.silo, silo)} · {text(receipt.venue, resolvedVenue)} ·{" "}
              {text(receipt.environment, "SANDBOX")} · {text(receipt.vault, "ring0_vault/silos/key_vault")}
            </p>
          </div>
        ) : null}
      </div>
    </GlassPanel>
  );
}

/* ---------------------------- key rotation ---------------------------- */

interface KeyRotationPanelProps {
  status: JsonRecord | null;
  loading: boolean;
  error: string | null;
  onReload: () => void;
}

function KeyRotationPanel({ status, loading, error, onReload }: KeyRotationPanelProps): ReactNode {
  const action = useAction<JsonRecord>();
  const [open, setOpen] = useState(false);

  const vaultRecord = record(status);
  const osTuning = record(vaultRecord.os_tuning);
  const archivedKeys = Array.isArray(vaultRecord.archived_public_keys)
    ? vaultRecord.archived_public_keys.length
    : null;
  const rotation = record(action.receipt);
  const rotationArchived = Array.isArray(rotation.archived_public_keys)
    ? rotation.archived_public_keys.length
    : null;

  const rotate = (): void => {
    void action.run(() => api.rotateKeys()).then((result) => {
      setOpen(false);
      if (result) onReload();
    });
  };

  return (
    <GlassPanel
      title="Ring 0 Zero-Downtime Key Rotation"
      eyebrow="sec_011 · Fernet + Ed25519 rotation"
      actions={
        <button
          type="button"
          className="btn btn-sm btn-danger"
          onClick={() => setOpen(true)}
          disabled={action.busy}
          aria-label="Rotate the Ring 0 Fernet and Ed25519 signing keys"
        >
          Rotate keys
        </button>
      }
    >
      <div className="flex flex-col gap-3">
        {loading && !status ? <LoadingBlock rows={2} label="Reading the Ring 0 vault status" /> : null}
        {error ? <ErrorBlock message={error} onRetry={onReload} /> : null}

        <div className="grid grid-cols-2 gap-2">
          <div className="tile">
            <p className="kpi-label">Key version</p>
            <p className="mono tnum mt-1 text-[16px] font-semibold">
              <TabularValue value={vaultRecord.key_version} kind="int" />
            </p>
          </div>
          <div className="tile">
            <p className="kpi-label">Algorithm</p>
            <p className="mono mt-1 text-[12px]">{text(vaultRecord.ed25519_algorithm, "—")}</p>
          </div>
          <div className="tile">
            <p className="kpi-label">Archived public keys</p>
            <p className="mono tnum mt-1 text-[16px] font-semibold">
              {archivedKeys === null ? "—" : int(archivedKeys)}
            </p>
          </div>
          <div className="tile">
            <p className="kpi-label">OS tuning</p>
            <p className="mono mt-1 text-[10.5px] text-[var(--ink-soft)]">
              {text(osTuning.profile, "—")} · <TabularValue value={osTuning.recommended} kind="int" /> recommended
            </p>
          </div>
        </div>

        <div className="chip-row">
          <StatusPill
            value={vaultRecord.write_only === true ? "write-only" : "write scope unknown"}
            tone={passTone(flagOrNull(vaultRecord.write_only))}
          />
          <StatusPill
            value={`raw secrets exposed ${text(vaultRecord.raw_secrets_exposed, "—")}`}
            tone={flagOrNull(vaultRecord.raw_secrets_exposed) === false ? "ok" : flagOrNull(vaultRecord.raw_secrets_exposed) === true ? "bad" : "neutral"}
          />
          <StatusPill
            value={`rotation recovery ${flagOrNull(vaultRecord.rotation_pending_recovery) === true ? "PENDING" : "clean"}`}
            tone={flagOrNull(vaultRecord.rotation_pending_recovery) === true ? "warn" : "ok"}
          />
          <span className="pill mono tnum">checked {utc(vaultRecord.checked_at_ns)} UTC</span>
        </div>

        <div className="chip-row">
          <span className="pill mono">{text(osTuning.platform, "—")}</span>
          <span className="pill mono">
            applied <TabularValue value={osTuning.applied} kind="int" />
          </span>
          <span className="pill mono">
            errors <TabularValue value={osTuning.error_count} kind="int" />
          </span>
          <span className="pill mono">
            TCP_NODELAY <TabularValue value={osTuning.socket_tcp_nodelay} kind="int" />
          </span>
          <span className="pill mono">
            transports{" "}
            {flagOrNull(osTuning.transports_ok) === true ? "ok" : flagOrNull(osTuning.transports_ok) === false ? "degraded" : "—"}
          </span>
        </div>

        <p className="text-[11px] text-[var(--dim)]">
          Rotation decrypts every stored credential under the old Fernet key and re-encrypts it under the new one inside
          one locked critical section; the previous Ed25519 public key is archived so historical signatures still verify.
        </p>

        {action.error ? <ErrorBlock message={action.error} onRetry={rotate} /> : null}

        {action.receipt ? (
          <div className="flex flex-col gap-2 rounded-[var(--r-sm)] border border-[rgba(52,211,153,0.3)] bg-[var(--pos-soft)] p-2.5">
            <div className="chip-row">
              <StatusPill
                value={flagOrNull(rotation.zero_downtime) === true ? "zero downtime" : "downtime unproven"}
                tone={passTone(flagOrNull(rotation.zero_downtime))}
              />
              <StatusPill
                value={flagOrNull(rotation.zero_loss) === true ? "zero loss" : "loss unproven"}
                tone={passTone(flagOrNull(rotation.zero_loss))}
              />
              <StatusPill
                value={flagOrNull(rotation.historical_signature_verified) === true ? "history verifies" : "history unproven"}
                tone={passTone(flagOrNull(rotation.historical_signature_verified))}
              />
            </div>
            <dl className="grid grid-cols-2 gap-x-3 gap-y-1.5">
              <div className="row-between">
                <dt className="field-label">new version</dt>
                <dd className="mono tnum text-[11px]">
                  <TabularValue value={rotation.key_version} kind="int" />
                </dd>
              </div>
              <div className="row-between">
                <dt className="field-label">previous</dt>
                <dd className="mono tnum text-[11px]">
                  <TabularValue value={rotation.previous_key_version} kind="int" />
                </dd>
              </div>
              <div className="row-between">
                <dt className="field-label">archived</dt>
                <dd className="mono tnum text-[11px]">{rotationArchived === null ? "—" : int(rotationArchived)}</dd>
              </div>
              <div className="row-between">
                <dt className="field-label">re-encrypted secrets</dt>
                <dd className="mono tnum text-[11px]">
                  <TabularValue value={rotation.reencrypted_broker_credentials_count} kind="int" />
                </dd>
              </div>
              <div className="row-between">
                <dt className="field-label">llm keys</dt>
                <dd className="mono tnum text-[11px]">
                  <TabularValue value={rotation.reencrypted_llm_keys_count} kind="int" />
                </dd>
              </div>
              <div className="row-between">
                <dt className="field-label">elapsed</dt>
                <dd className="mono tnum text-[11px]">{num(rotation.elapsed_ms, 1)} ms</dd>
              </div>
            </dl>
            <div className="row-between gap-2">
              <span className="field-label">new public fingerprint</span>
              <CopyHash value={rotation.ed25519_public_fingerprint} label="copy the rotated Ed25519 public fingerprint" />
            </div>
            <p className="mono text-[9.5px] text-[var(--dim)]">
              probe {text(rotation.historical_probe_sha256, "—").slice(0, 16)}… · verifier{" "}
              {text(rotation.verifier_ring, "ring0_vault")} · no raw secret in this receipt
            </p>
          </div>
        ) : null}

        <ConfirmDangerModal
          open={open}
          title="Rotate Ring 0 signing keys"
          body={
            <>
              This replaces the Fernet master keys and the Ed25519 order-signing keypair, re-encrypts every stored
              credential and archives the previous public key. Ring 1 and the cockpit keep their vault references, so the
              rotation is zero-downtime — but the signing fingerprint changes.
            </>
          }
          confirmLabel="Rotate keys"
          requireText="ROTATE"
          busy={action.busy}
          onCancel={() => setOpen(false)}
          onConfirm={rotate}
        />
      </div>
    </GlassPanel>
  );
}

/* ------------------------------- LLM pool ------------------------------- */

function LlmPoolPanel(): ReactNode {
  const action = useAction<JsonRecord>();
  const receipt = record(action.receipt);
  const capMs = asNumber(receipt.hard_timeout_ms) ?? LLM_HARD_CAP_MS;
  const status = text(receipt.status, "");
  const fallbackActive = flagOrNull(receipt.fallback_active);
  const spend = spendOf(receipt);

  const endpoints = [record(receipt.system1), record(receipt.system2)]
    .map((row, index) => ({ row, index }))
    .filter((entry) => Object.keys(entry.row).length > 0);

  const probe = (): void => {
    void action.run(() => api.llmProbe());
  };

  return (
    <GlassPanel
      title="LLM Pool & Ollama Probe"
      eyebrow="sec_018 Law 2 · System 1 lease off the hot path"
      actions={
        <button
          type="button"
          className="btn btn-sm"
          onClick={probe}
          disabled={action.busy}
          aria-label="Probe the local LLM endpoints under the 250 millisecond hard cap"
        >
          {action.busy ? "Probing…" : "Probe LLM endpoints"}
        </button>
      }
    >
      <div className="flex flex-col gap-3">
        <div className="chip-row">
          <span className="pill pill-accent2 mono">
            $<TabularValue value={DAILY_TOKEN_CAP_USD} digits={2} />/day cap
          </span>
          <span className="pill pill-info mono">
            &lt; <TabularValue value={capMs} kind="int" /> ms hard cap
          </span>
          <span className="pill mono">zero tokens on fallback</span>
        </div>

        {action.error ? <ErrorBlock message={action.error} onRetry={probe} /> : null}

        {action.receipt ? (
          <div className="flex flex-col gap-2">
            <div className="chip-row">
              <StatusPill value={status} tone={status === "LOCAL_OLLAMA_LIVE" ? "ok" : "warn"} />
              <StatusPill
                value={fallbackActive === true ? "fallback active" : fallbackActive === false ? "no fallback" : "fallback unknown"}
                tone={fallbackActive === false ? "ok" : fallbackActive === true ? "warn" : "neutral"}
              />
              <span className="pill mono">
                <TabularValue value={receipt.model_count} kind="int" /> models
              </span>
              <span className="pill mono">
                <TabularValue value={receipt.elapsed_ms} digits={1} /> ms probe
              </span>
            </div>

            {spend !== null ? (
              <div className="flex flex-col gap-1">
                <div className="row-between">
                  <span className="field-label">token spend today</span>
                  <span className="mono tnum text-[10.5px]">
                    <TabularValue value={spend} kind="usd" /> / ${num(DAILY_TOKEN_CAP_USD, 2)}
                  </span>
                </div>
                <InlineBar
                  value={spend}
                  max={DAILY_TOKEN_CAP_USD}
                  tone={spend >= DAILY_TOKEN_CAP_USD ? "neg" : "accent"}
                  label="token spend against the daily cap"
                />
              </div>
            ) : null}

            {endpoints.length === 0 ? (
              <EmptyCleanSlate
                title="No LLM endpoint reported"
                body="The probe returns a system1 and system2 block when an endpoint is configured. Neither was present in this receipt, so Ring 1 stays on the deterministic abstention path."
              />
            ) : (
              <div className="scroll-x">
                <table className="table table-compact">
                  <caption className="sr-only">LLM endpoints with provider, model, status and latency budget</caption>
                  <thead>
                    <tr>
                      <th scope="col">Endpoint</th>
                      <th scope="col">Provider</th>
                      <th scope="col">Model</th>
                      <th scope="col">Base URL</th>
                      <th scope="col">Status</th>
                      <th scope="col" className="num">
                        Latency
                      </th>
                      <th scope="col">Verdict</th>
                    </tr>
                  </thead>
                  <tbody>
                    {endpoints.map(({ row, index }) => {
                      const ms = asNumber(row.latency_ms);
                      const within = flagOrNull(row.within_budget) ?? (ms === null ? null : ms <= capMs);
                      return (
                        <tr key={`system-${index}`}>
                          <td className="mono text-[10.5px]">
                            System <TabularValue value={index + 1} kind="int" />
                          </td>
                          <td className="mono text-[10.5px]">{text(row.provider, "—")}</td>
                          <td className="mono text-[10.5px]">{text(row.model, "—")}</td>
                          <td className="mono text-[10px] break-all text-[var(--dim)]">{text(row.base_url, "—")}</td>
                          <td>
                            <StatusPill
                              value={
                                row.reachable === true
                                  ? "REACHABLE"
                                  : row.reachable === false
                                    ? "UNREACHABLE"
                                    : "UNKNOWN"
                              }
                              tone={passTone(flagOrNull(row.reachable))}
                            />
                          </td>
                          <td className="num">
                            <span className="mono tnum">{latency(ms)}</span>
                          </td>
                          <td>
                            <StatusPill
                              value={within === true ? "within cap" : within === false ? "over cap" : "—"}
                              tone={passTone(within)}
                            />
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        ) : (
          <p className="text-[11.5px] text-[var(--dim)]">
            Pings System 1 (local Ollama) and System 2 inside {num(LLM_HARD_CAP_MS, 0)} ms. A slow or absent local model
            engages the deterministic instinct fallback: Ring 1 abstains on a dead-man lease and burns zero tokens.
          </p>
        )}
      </div>
    </GlassPanel>
  );
}

/* ------------------------------ MCP console ------------------------------ */

interface McpPanelProps {
  data: McpPageData | null;
  loading: boolean;
  error: string | null;
  onReload: () => void;
}

function McpConsolePanel({ data, loading, error, onReload }: McpPanelProps): ReactNode {
  const action = useAction<McpCallResult>();
  const [toolName, setToolName] = useState("");
  const [argsText, setArgsText] = useState("{}");
  const [parseError, setParseError] = useState<string | null>(null);

  const tools = data?.tools ?? [];
  const toolNames = tools.map((tool) => text(tool.name, "")).filter((name) => name !== "");
  const selected = toolNames.includes(toolName) ? toolName : (toolNames[0] ?? "");
  const invariants = record(data?.invariants);
  const airGap = record(data?.air_gap);
  const readOnlyCount = tools.filter((tool) => tool.read_only === true).length;
  const signingTools = asNumber(invariants.order_signing_tools);
  const keyReadingTools = asNumber(invariants.key_reading_tools);
  const ring3ReadOnly = flagOrNull(airGap.ring3_read_only);

  const receipt = record(action.receipt);
  const callOk = flagOrNull(receipt.ok);
  const callError = receipt.error;
  const result = receipt.result;

  const invoke = (): void => {
    setParseError(null);
    if (selected === "") {
      setParseError("select a tool first");
      return;
    }
    let parsed: unknown;
    try {
      parsed = JSON.parse(argsText) as unknown;
    } catch {
      setParseError("arguments must be valid JSON");
      return;
    }
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      setParseError("arguments must be a JSON object, for example {}");
      return;
    }
    const args = parsed as Record<string, unknown>;
    void action.run(() => api.mcpCall(selected, args));
  };

  return (
    <GlassPanel
      title="Air-Gapped MCP Console"
      eyebrow="sec_016 · read-only bridge"
      actions={
        <button
          type="button"
          className="btn btn-sm btn-ghost"
          onClick={onReload}
          disabled={loading}
          aria-label="Reload the MCP tool inventory"
        >
          Reload tools
        </button>
      }
    >
      <div className="flex flex-col gap-3">
        {loading && !data ? <LoadingBlock rows={3} label="Listing the read-only MCP tools" /> : null}
        {error ? <ErrorBlock message={error} onRetry={onReload} /> : null}

        {data ? (
          <>
            <div className="chip-row">
              <StatusPill
                value={`${int(readOnlyCount)} read-only tools`}
                tone={readOnlyCount > 0 ? "ok" : "warn"}
                title="tools with read_only: true"
                className="tnum"
              />
              <StatusPill
                value={`order signing tools ${signingTools === null ? "—" : int(signingTools)}`}
                tone={signingTools === 0 ? "ok" : signingTools === null ? "neutral" : "bad"}
                title="invariants.order_signing_tools must be 0"
                className="tnum"
              />
              <StatusPill
                value={`key reading tools ${keyReadingTools === null ? "—" : int(keyReadingTools)}`}
                tone={keyReadingTools === 0 ? "ok" : keyReadingTools === null ? "neutral" : "bad"}
                title="invariants.key_reading_tools must be 0"
                className="tnum"
              />
              <StatusPill
                value={ring3ReadOnly === true ? "Ring 3 read-only" : "Ring 3 writable"}
                tone={passTone(ring3ReadOnly)}
                title="air_gap.ring3_read_only must be true"
              />
              <span className="pill mono">{text(data.transport, "in-process")}</span>
            </div>

            {tools.length === 0 ? (
              <EmptyCleanSlate
                title="No MCP tools exposed"
                body="The air-gapped bridge reports the tools it is willing to serve. An empty inventory means the bridge is not attached to this gateway process."
              />
            ) : (
              <div className="scroll-y flex max-h-[228px] flex-col gap-1">
                {tools.map((tool, index) => {
                  const name = text(tool.name, `tool ${index + 1}`);
                  const isReadOnly = tool.read_only === true;
                  const active = name === selected;
                  return (
                    <button
                      key={`${name}-${index}`}
                      type="button"
                      className="palette-item"
                      data-active={active}
                      aria-pressed={active}
                      onClick={() => setToolName(name)}
                      aria-label={`Select the MCP tool ${name}`}
                    >
                      <span className="flex min-w-0 flex-1 flex-col">
                        <span className="mono truncate text-[10.5px]">{name}</span>
                        <span className="truncate text-[10px] text-[var(--dim)]">
                          {text(tool.description, "no description")}
                        </span>
                      </span>
                      <StatusPill
                        value={isReadOnly ? "read-only" : "writable"}
                        tone={isReadOnly ? "ok" : "bad"}
                        dot={false}
                      />
                    </button>
                  );
                })}
              </div>
            )}

            <div className="flex flex-col gap-2 border-t border-[var(--line)] pt-2">
              <label className="field">
                <span className="field-label">Tool</span>
                <select
                  className="select mono"
                  value={selected}
                  onChange={(event) => setToolName(event.target.value)}
                  aria-label="MCP tool to invoke"
                >
                  {toolNames.length === 0 ? <option value="">no tools</option> : null}
                  {toolNames.map((name) => (
                    <option key={name} value={name}>
                      {name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                <span className="field-label">Arguments (JSON)</span>
                <textarea
                  className="textarea mono"
                  value={argsText}
                  onChange={(event) => setArgsText(event.target.value)}
                  spellCheck={false}
                  aria-label="MCP tool arguments as a JSON object"
                />
              </label>
              <div className="row-between">
                <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--dim)] uppercase">
                  read-only · Ring 3 has no signing surface
                </span>
                <button
                  type="button"
                  className="btn btn-sm btn-accent"
                  onClick={invoke}
                  disabled={action.busy || selected === ""}
                  aria-label={`Invoke the read-only MCP tool ${selected || "none selected"}`}
                >
                  {action.busy ? "Invoking…" : "Invoke read-only tool"}
                </button>
              </div>
              {parseError ? <ErrorBlock message={parseError} /> : null}
              {action.error ? <ErrorBlock message={action.error} onRetry={invoke} /> : null}

              {action.receipt ? (
                <div className="flex flex-col gap-2">
                  <div className="chip-row">
                    <StatusPill value={callOk === true ? "OK" : callOk === false ? "ERROR" : "UNKNOWN"} tone={passTone(callOk)} />
                    <span className="pill mono">{text(receipt.name, selected)}</span>
                    <span className="pill mono">
                      <TabularValue value={receipt.elapsed_ms} digits={1} /> ms
                    </span>
                  </div>
                  {text(callError, "") !== "" ? <ErrorBlock message={text(callError)} /> : null}
                  <pre className="code-block">
                    {JSON.stringify(result === undefined || result === null ? { status: "no result" } : result, null, 2)}
                  </pre>
                </div>
              ) : null}
            </div>
          </>
        ) : null}

        {!data && !loading && !error ? (
          <EmptyCleanSlate
            title="No MCP inventory"
            body="The read-only bridge has not reported its tool inventory. Reload once the gateway has attached the in-process MCP bridge."
          />
        ) : null}
      </div>
    </GlassPanel>
  );
}

/* ---------------------------- factory reset ---------------------------- */

function FactoryResetPanel(): ReactNode {
  const action = useAction<JsonRecord>();
  const [password, setPassword] = useState("");
  const [livePin, setLivePin] = useState("");
  const [open, setOpen] = useState(false);

  const valid = password.trim() !== "" && livePin.trim() !== "";

  const execute = (): void => {
    void action.run(() => authApi.resetEverything({ password, live_pin: livePin })).then((receipt) => {
      setOpen(false);
      if (receipt) {
        setPassword("");
        setLivePin("");
        navigate("/setup");
      }
    });
  };

  return (
    <GlassPanel
      className="border-[rgba(251,113,133,0.42)]"
      title="Two-Factor Sovereign Factory Reset"
      eyebrow="danger zone · irreversible"
    >
      <div className="flex flex-col gap-3">
        <div className="hazard-thin" aria-hidden="true" />
        <p className="max-w-[86ch] text-[11.5px] text-[var(--muted)]">
          Erases the operator credential, the live PIN, every stored broker and LLM credential, and all paper workspace
          state — silos, vaults, orders, ledger, logbook and shadow nursery. Ring 0 key material is destroyed with the
          vault. Both factors are required: the operator password and the live PIN.
        </p>

        <div className="grid grid-cols-2 gap-2">
          <label className="field">
            <span className="field-label">Operator password</span>
            <input
              className="input"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoComplete="current-password"
              aria-label="Operator password for the factory reset"
            />
          </label>
          <label className="field">
            <span className="field-label">Live PIN</span>
            <input
              className="input mono tnum"
              type="password"
              inputMode="numeric"
              value={livePin}
              onChange={(event) => setLivePin(event.target.value)}
              autoComplete="off"
              aria-label="Live PIN for the factory reset"
            />
          </label>
        </div>

        <div className="row-between">
          <span className="mono text-[9.5px] tracking-[0.08em] text-[var(--neg)] uppercase">
            requires typing FACTORY RESET in the confirm dialog
          </span>
          <button
            type="button"
            className="btn btn-danger"
            onClick={() => setOpen(true)}
            disabled={!valid || action.busy}
            aria-label="Open the factory reset confirmation dialog"
          >
            Factory reset
          </button>
        </div>

        {action.error ? <ErrorBlock message={action.error} onRetry={execute} /> : null}

        <ConfirmDangerModal
          open={open}
          title="Factory reset the sovereign cockpit"
          body={
            <>
              This wipes the operator credential, the live PIN, all broker and LLM credentials and every paper workspace
              object. It cannot be undone and the gateway returns to the clean-slate setup portal.
            </>
          }
          confirmLabel="Erase everything"
          requireText="FACTORY RESET"
          busy={action.busy}
          onCancel={() => setOpen(false)}
          onConfirm={execute}
        />
      </div>
    </GlassPanel>
  );
}
