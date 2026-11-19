/**
 * `/control` — 23-gate go-live certificate, incident centre, runtime risk policy,
 * write-only credential vault, key rotation, LLM pool, MCP console and reset.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Banner,
  Button,
  Card,
  Cell,
  Checkbox,
  EmptyState,
  Field,
  JsonBlock,
  KeyValue,
  PageHeader,
  ProgressBar,
  Row,
  SelectInput,
  Spinner,
  Stat,
  StatGrid,
  Table,
  Tabs,
  TextArea,
  TextInput,
} from "../components/ui";
import { SILOS } from "../config/nav";
import { api, assertFinite, errorMessage } from "../lib/api";
import { asArray, asRecord, clockNs, int, num, shortHash, text, titleize } from "../lib/format";
import { numberAt, preflightOf, rowsOf, statusTone } from "../lib/payload";
import { useTicker } from "../state/session";
import type { ApiNote, PageProps } from "./types";

const VENUES = [
  "binance",
  "okx",
  "bybit",
  "dhan",
  "zerodha",
  "angel",
  "upstox",
  "interactive_brokers",
  "alpaca",
  "vnpy_mt5",
  "vnpy_ctp",
  "oanda",
];
const VENUE_SILO: Record<string, string> = {
  binance: "CRYPTO_BINANCE",
  okx: "CRYPTO_BINANCE",
  bybit: "CRYPTO_BINANCE",
  dhan: "INDIA_ZERODHA_DHAN",
  zerodha: "INDIA_ZERODHA_DHAN",
  angel: "INDIA_ZERODHA_DHAN",
  upstox: "INDIA_ZERODHA_DHAN",
  interactive_brokers: "US_IBKR",
  alpaca: "US_IBKR",
  vnpy_mt5: "FX_MT5",
  vnpy_ctp: "FX_MT5",
  oanda: "FX_MT5",
};
const CONNECTOR_MODES = ["OFFLINE_REPLAY", "SANDBOX_TESTNET", "LIVE_VENUE"];
const LLM_TARGET_DAILY_USD = 3.1;

interface SettingsForm {
  connector_mode: string;
  allow_network: boolean;
  max_drawdown_bps: string;
  alert_slippage_bps: string;
  alert_drift_threshold: string;
  default_reserve_floor_pct: string;
  default_tax_escrow_pct: string;
  autopilot_max_symbols: string;
  lifecycle_enabled: boolean;
}

const EMPTY_SETTINGS: SettingsForm = {
  connector_mode: "OFFLINE_REPLAY",
  allow_network: false,
  max_drawdown_bps: "250",
  alert_slippage_bps: "15",
  alert_drift_threshold: "0.75",
  default_reserve_floor_pct: "15",
  default_tax_escrow_pct: "20",
  autopilot_max_symbols: "274",
  lifecycle_enabled: true,
};

export function ControlPage({ mode }: PageProps) {
  const [preflightRaw, setPreflightRaw] = useState<Record<string, unknown>>({});
  const [alerts, setAlerts] = useState<Record<string, unknown>[]>([]);
  const [settings, setSettings] = useState<Record<string, unknown>>({});
  const [form, setForm] = useState<SettingsForm>(EMPTY_SETTINGS);
  const [tools, setTools] = useState<Record<string, unknown>[]>([]);
  const [llm, setLlm] = useState<Record<string, unknown>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<ApiNote | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [alertFilter, setAlertFilter] = useState("ALL");
  const [credVenue, setCredVenue] = useState("binance");
  const [credKey, setCredKey] = useState("");
  const [credSecret, setCredSecret] = useState("");
  const [credMask, setCredMask] = useState("");
  const [rotateConfirm, setRotateConfirm] = useState(false);
  const [rotation, setRotation] = useState<Record<string, unknown> | null>(null);
  const [mcpTool, setMcpTool] = useState("");
  const [mcpBody, setMcpBody] = useState("{}");
  const [mcpResult, setMcpResult] = useState<Record<string, unknown> | null>(null);
  const [resetPassword, setResetPassword] = useState("");
  const [resetPin, setResetPin] = useState("");
  const [resetPhrase, setResetPhrase] = useState("");
  const [resetDone, setResetDone] = useState(false);
  const [lastSync, setLastSync] = useState(0);
  const now = useTicker(1000);

  const hydrateSettings = useCallback((payload: Record<string, unknown>) => {
    setForm({
      connector_mode: text(payload.connector_mode, "OFFLINE_REPLAY"),
      allow_network: payload.allow_network === true,
      max_drawdown_bps: text(payload.max_drawdown_bps, "250"),
      alert_slippage_bps: text(payload.alert_slippage_bps, "15"),
      alert_drift_threshold: text(payload.alert_drift_threshold, "0.75"),
      default_reserve_floor_pct: text(payload.default_reserve_floor_pct, "15"),
      default_tax_escrow_pct: text(payload.default_tax_escrow_pct, "20"),
      autopilot_max_symbols: text(payload.autopilot_max_symbols, "274"),
      lifecycle_enabled: payload.lifecycle_enabled !== false,
    });
  }, []);

  const loadAlerts = useCallback(async () => {
    try {
      const payload = asRecord(await api.get<unknown>("/alerts"));
      setAlerts(rowsOf(payload.alerts, ["alerts"]));
    } catch (caught) {
      setError(errorMessage(caught));
    }
  }, []);

  const loadAll = useCallback(async () => {
    try {
      const [preflightPayload, settingsPayload, toolsPayload, statePayload] = await Promise.all([
        api.get<unknown>("/control/preflight"),
        api.get<unknown>("/settings"),
        api.get<unknown>("/mcp/tools"),
        api.get<unknown>("/cockpit/state"),
      ]);
      const preflightRecord = asRecord(preflightPayload);
      setPreflightRaw(preflightRecord);
      const settingsRecord = asRecord(settingsPayload);
      setSettings(settingsRecord);
      hydrateSettings(settingsRecord);
      const toolRows = rowsOf(asRecord(toolsPayload).tools, ["tools"]);
      setTools(toolRows);
      setMcpTool((current) => current || text(toolRows[0]?.name, ""));
      setLlm(asRecord(asRecord(statePayload).llm));
      setLastSync(Date.now());
      setError(null);
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setLoading(false);
    }
    await loadAlerts();
  }, [hydrateSettings, loadAlerts]);

  useEffect(() => {
    void loadAll();
  }, [loadAll]);

  useEffect(() => {
    const timer = globalThis.setInterval(() => void loadAlerts(), 10_000);
    return () => globalThis.clearInterval(timer);
  }, [loadAlerts]);

  const preflight = useMemo(() => preflightOf(preflightRaw), [preflightRaw]);
  const warned = numberAt(preflightRaw, ["warned_gates"], 0);
  const certificate = shortHash(preflightRaw.certificate_sha256, 16);
  const liveCritical = useMemo(() => {
    const fromServer = asArray(settings.live_critical_keys)
      .map((entry) => text(entry, ""))
      .filter(Boolean);
    const source =
      fromServer.length > 0
        ? fromServer
        : ["connector_mode", "allow_network", "max_drawdown_bps", "autopilot_interval_s"];
    return new Set(source);
  }, [settings]);

  const visibleAlerts = useMemo(() => {
    return alerts.filter((alert) => {
      const severity = text(alert.severity, "").toUpperCase();
      if (alertFilter === "CRITICAL") return severity === "CRITICAL";
      if (alertFilter === "UNACKNOWLEDGED") return alert.acknowledged !== true;
      return true;
    });
  }, [alerts, alertFilter]);

  const unacked = alerts.filter((alert) => alert.acknowledged !== true).length;
  const critical = alerts.filter((alert) => text(alert.severity, "").toUpperCase() === "CRITICAL").length;

  const llmSpend = numberAt(llm, ["daily_spend_usd"], 0);
  const llmCap = numberAt(llm, ["daily_spend_cap_usd"], LLM_TARGET_DAILY_USD);
  const llmRows = useMemo(
    () =>
      ["system1", "system2"]
        .map((slot) => asRecord(llm[slot]))
        .filter((slot) => Object.keys(slot).length > 0),
    [llm],
  );

  const runPreflight = async () => {
    setNote(null);
    setBusy("preflight");
    try {
      const payload = asRecord(
        await api.post<unknown>("/control/preflight", { strict: true, full_smoke: false, refresh: true }),
      );
      setPreflightRaw(payload);
      const summary = preflightOf(payload);
      setNote({
        tone: summary.ready ? "bull" : "bear",
        message: summary.ready
          ? `READY FOR LIVE CUTOVER · ${summary.passed}/${summary.total} gates · sha256 ${shortHash(payload.certificate_sha256, 16)}`
          : `NOT READY · ${summary.failed} failing gate(s) · resolve every FAIL row before confirm_live`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const ackAlert = async (alertId: string) => {
    setNote(null);
    setBusy(alertId);
    try {
      await api.post(`/alerts/${encodeURIComponent(alertId)}/ack`, {});
      setNote({ tone: "bull", message: `ACK · ${alertId} acknowledged by the operator` });
      await loadAlerts();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const saveSettings = async () => {
    setNote(null);
    setBusy("settings");
    try {
      if (form.connector_mode === "LIVE_VENUE" && mode !== "LIVE") {
        throw new Error("LIVE_VENUE REQUIRES A LIVE SESSION");
      }
      if (form.connector_mode === "LIVE_VENUE" && !form.allow_network) {
        throw new Error("LIVE_MODE_REQUIRES_ALLOW_NETWORK");
      }
      const patch: Record<string, unknown> = {};
      if (form.connector_mode !== text(settings.connector_mode, "OFFLINE_REPLAY")) {
        patch.connector_mode = form.connector_mode;
      }
      if (form.allow_network !== (settings.allow_network === true)) {
        patch.allow_network = form.allow_network;
      }
      if (form.lifecycle_enabled !== (settings.lifecycle_enabled !== false)) {
        patch.lifecycle_enabled = form.lifecycle_enabled;
      }
      const numeric: Array<[string, string]> = [
        ["max_drawdown_bps", form.max_drawdown_bps],
        ["alert_slippage_bps", form.alert_slippage_bps],
        ["alert_drift_threshold", form.alert_drift_threshold],
        ["default_reserve_floor_pct", form.default_reserve_floor_pct],
        ["default_tax_escrow_pct", form.default_tax_escrow_pct],
        ["autopilot_max_symbols", form.autopilot_max_symbols],
      ];
      for (const [key, raw] of numeric) {
        const next = assertFinite(Number(raw), key);
        if (next !== numberAt(settings, [key], Number.NaN)) patch[key] = next;
      }
      const keys = Object.keys(patch);
      if (keys.length === 0) throw new Error("no setting changed");
      const blocked = keys.filter((key) => liveCritical.has(key));
      if (blocked.length > 0 && mode !== "LIVE") {
        throw new Error(`LIVE-CRITICAL · ${blocked.join(", ")} requires a LIVE-elevated session`);
      }
      const payload = asRecord(
        await api.post<unknown>("/settings", { ...patch, operator: "ring3.operator" }),
      );
      setSettings(payload);
      hydrateSettings(payload);
      setNote({
        tone: "bull",
        message: `SETTINGS PERSISTED · ${int(payload.changed_count)} key(s) changed · sha256 ${shortHash(
          payload.settings_sha256,
          12,
        )}`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const writeCredentials = async () => {
    setNote(null);
    setBusy("credentials");
    try {
      if (credKey.trim().length === 0 || credSecret.trim().length === 0) {
        throw new Error("api_key and api_secret are both required");
      }
      const report = asRecord(
        await api.post<unknown>("/control/brokers/credentials", {
          silo: VENUE_SILO[credVenue] ?? SILOS[0].id,
          venue: credVenue,
          api_key: credKey,
          api_secret: credSecret,
          sandbox: mode !== "LIVE",
        }),
      );
      setCredKey("");
      setCredSecret("");
      setCredMask(text(report.api_key_masked, "***"));
      setNote({ tone: "bull", message: "WRITTEN — VALUE NOT ECHOED" });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const rotateKeys = async () => {
    setNote(null);
    setBusy("rotate");
    try {
      if (!rotateConfirm) throw new Error("rotation requires the confirmation checkbox");
      const payload = asRecord(
        await api.post<unknown>("/control/vault/rotate-keys", {
          confirm: true,
          reason: "operator confirmed zero-downtime rotation",
          operator: "ring3.operator",
        }),
      );
      setRotation(payload);
      setNote({
        tone: "bull",
        message: `KEY ROTATION COMPLETE · zero downtime=${text(payload.zero_downtime)} · zero loss=${text(
          payload.credentials_zero_loss,
        )} · ${num(payload.elapsed_ms, 1)} ms`,
      });
      setRotateConfirm(false);
      await loadAll();
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const probeLlms = async () => {
    setNote(null);
    setBusy("llm");
    try {
      const payload = asRecord(await api.post<unknown>("/control/llms/probe", {}));
      setNote({
        tone: payload.local_ollama_live === true ? "bull" : "neutral",
        message: `LLM PROBE · ${text(payload.status, "UNKNOWN")} · ${num(payload.elapsed_ms, 1)} ms · fallback ${text(
          payload.fallback_active,
        )}`,
      });
      const statePayload = asRecord(await api.get<unknown>("/cockpit/state"));
      setLlm(asRecord(statePayload.llm));
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const callTool = async () => {
    setNote(null);
    setBusy("mcp");
    try {
      if (mcpTool.trim().length === 0) throw new Error("select a read-only MCP tool first");
      let parsed: unknown;
      try {
        parsed = JSON.parse(mcpBody.trim().length === 0 ? "{}" : mcpBody);
      } catch {
        throw new Error("arguments must be valid JSON");
      }
      if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
        throw new Error("arguments must be a JSON object");
      }
      const payload = asRecord(await api.post<unknown>("/mcp/call", { tool: mcpTool, arguments: parsed }));
      setMcpResult(payload);
      setNote({
        tone: "bull",
        message: `MCP · ${mcpTool} · ok=${text(payload.ok)} · ${num(payload.elapsed_ms, 2)} ms`,
      });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const factoryReset = async () => {
    setNote(null);
    setBusy("reset");
    try {
      if (resetPassword.length < 8) throw new Error("password must be at least 8 characters");
      if (!/^[0-9]{4,12}$/.test(resetPin)) throw new Error("live PIN must be 4 to 12 digits");
      if (resetPhrase.trim().toUpperCase() !== "RESET")
        throw new Error("type RESET to confirm the factory reset");
      await api.post<unknown>("/auth/reset", { password: resetPassword, live_pin: resetPin });
      setResetDone(true);
      setResetPassword("");
      setResetPin("");
      setResetPhrase("");
      setNote({ tone: "bull", message: "FACTORY RESET COMPLETE" });
    } catch (caught) {
      setNote({ tone: "bear", message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };

  const selectedTool = tools.find((tool) => text(tool.name, "") === mcpTool);
  const requiredFields = Array.isArray(asRecord(selectedTool?.inputSchema).required)
    ? (asRecord(selectedTool?.inputSchema).required as unknown[]).map((entry) => text(entry, "")).join(", ")
    : "";
  const resetReady =
    resetPassword.length >= 8 &&
    /^[0-9]{4,12}$/.test(resetPin) &&
    resetPhrase.trim().toUpperCase() === "RESET";

  return (
    <div className="flex flex-col gap-3">
      <PageHeader
        title="CONTROL"
        subtitle="Go-live certificate · incidents · runtime risk policy · keys · LLM pool · MCP · reset"
        badges={
          <>
            <Badge tone={mode === "LIVE" ? "bear" : "accent"}>{mode}</Badge>
            <Badge tone={preflight.ready ? "bull" : "bear"}>{preflight.ready ? "READY" : "NOT READY"}</Badge>
          </>
        }
        actions={
          <>
            <Button variant="subtle" size="sm" icon="refresh" onClick={() => void loadAll()}>
              Refresh
            </Button>
            <Button
              variant="primary"
              size="sm"
              icon="shield"
              disabled={busy !== null}
              onClick={() => void runPreflight()}
            >
              {busy === "preflight" ? "RUNNING…" : "Re-run Pre-Flight"}
            </Button>
          </>
        }
      />

      {error ? <Banner tone="bear">{error}</Banner> : null}
      {note ? <Banner tone={note.tone}>{note.message}</Banner> : null}

      <StatGrid columns={6}>
        <Stat
          label="Certificate"
          value={preflight.ready ? "READY FOR LIVE CUTOVER" : "NOT READY"}
          tone={preflight.ready ? "bull" : "bear"}
          hint={preflight.release || "23 gates"}
        />
        <Stat
          label="Passed"
          value={int(preflight.passed)}
          tone="bull"
          hint={`of ${int(preflight.total)} gates`}
        />
        <Stat label="Failed" value={int(preflight.failed)} tone={preflight.failed === 0 ? "bull" : "bear"} />
        <Stat label="Warned" value={int(warned)} tone={warned === 0 ? "bull" : "accent"} />
        <Stat label="Unacked Alerts" value={int(unacked)} tone={unacked === 0 ? "bull" : "accent"} />
        <Stat label="Critical" value={int(critical)} tone={critical === 0 ? "bull" : "bear"} />
      </StatGrid>

      <Card
        title="23-Gate Pre-Flight Certificate"
        subtitle={`certificate sha256 ${certificate} · digest ${text(preflightRaw.certificate_digest_algorithm, "sha256")}`}
        icon="verification"
        dense
        actions={
          <Badge tone={preflight.ready ? "bull" : "bear"}>
            {preflight.ready ? "READY FOR LIVE CUTOVER" : "NOT READY"}
          </Badge>
        }
      >
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-6 text-[11px] text-ink-3">
            <Spinner /> CERTIFYING GATES…
          </div>
        ) : preflight.gates.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="NO GATE ROWS"
              hint="The certificate returned no gates. Re-run the pre-flight to rebuild it."
            />
          </div>
        ) : (
          <Table dense columns={["ID", "STATUS", "DETAIL"]}>
            {preflight.gates.map((gate, index) => (
              <Row key={`${gate.id}-${index}`} tone={statusTone(gate.status) === "bear" ? "bear" : undefined}>
                <Cell mono className="text-[10px]">
                  {gate.id}
                </Cell>
                <Cell align="center">
                  <Badge tone={statusTone(gate.status)}>{gate.status}</Badge>
                </Cell>
                <Cell className="text-[10px] text-ink-2">{gate.detail || "—"}</Cell>
              </Row>
            ))}
          </Table>
        )}
      </Card>

      <Card
        title="Incident & Alert Center"
        subtitle={`poll 10s · last sync ${clockNs(lastSync * 1e6)} UTC`}
        icon="bell"
        dense
        actions={
          <Badge tone={unacked === 0 ? "bull" : "accent"} mono>
            {int(unacked)} UNACKED
          </Badge>
        }
      >
        <div className="px-2 pt-2">
          <Tabs
            active={alertFilter}
            onChange={setAlertFilter}
            tabs={[
              { id: "ALL", label: "All", count: alerts.length },
              { id: "CRITICAL", label: "Critical", count: critical },
              { id: "UNACKNOWLEDGED", label: "Unacknowledged", count: unacked },
            ]}
          />
        </div>
        {loading ? (
          <div className="flex items-center gap-2 px-3 py-6 text-[11px] text-ink-3">
            <Spinner /> LOADING INCIDENTS…
          </div>
        ) : visibleAlerts.length === 0 ? (
          <div className="p-3">
            <EmptyState
              title="NO INCIDENTS"
              hint="The alert journal is empty for this filter. Nothing needs an operator."
            />
          </div>
        ) : (
          <Table dense columns={["SEVERITY", "TITLE", "MESSAGE", "CREATED", "STATE", ""]}>
            {visibleAlerts.map((alert, index) => {
              const detail = asRecord(alert.detail);
              const escalation =
                text(detail.escalation_state, "").toUpperCase() === "MANUAL_INTERVENTION_REQUIRED" ||
                text(alert.code, "") === "UNKNOWN_ORDER_MANUAL_INTERVENTION";
              const severity = text(alert.severity, "INFO").toUpperCase();
              const acked = alert.acknowledged === true;
              return (
                <Row
                  key={`${text(alert.alert_id, "alert")}-${index}`}
                  className={escalation ? "border-l-2 border-l-bear" : undefined}
                >
                  <Cell>
                    <div className="flex flex-col items-start gap-1">
                      <Badge tone={statusTone(severity)}>{severity}</Badge>
                      {escalation ? <Badge tone="bear">CRITICAL</Badge> : null}
                    </div>
                  </Cell>
                  <Cell className="text-[11px] text-ink">{titleize(alert.code)}</Cell>
                  <Cell className="text-[10px] text-ink-2">
                    {text(alert.message)}
                    {text(alert.silo, "").length > 0 ? (
                      <span className="ml-1 text-ink-3">· {text(alert.silo)}</span>
                    ) : null}
                  </Cell>
                  <Cell mono className="whitespace-nowrap text-[10px]">
                    {clockNs(alert.created_at_utc_ns)}
                  </Cell>
                  <Cell align="center">
                    <Badge tone={acked ? "muted" : "accent"}>{acked ? "ACKED" : "OPEN"}</Badge>
                  </Cell>
                  <Cell align="right">
                    {acked ? (
                      <span className="text-[10px] text-ink-3">{text(alert.acknowledged_by, "—")}</span>
                    ) : (
                      <Button
                        size="sm"
                        variant="subtle"
                        icon="check"
                        disabled={busy !== null}
                        onClick={() => void ackAlert(text(alert.alert_id, ""))}
                      >
                        {busy === text(alert.alert_id, "") ? "ACKING" : "ACK"}
                      </Button>
                    )}
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card
          title="Runtime Risk & Connector Policy"
          subtitle="Journal-backed · a live-risk knob needs a LIVE-elevated session"
          icon="control"
        >
          <div className="grid grid-cols-2 gap-2">
            <Field
              label="Connector Mode"
              className="col-span-2"
              hint={mode === "LIVE" ? undefined : "LIVE_VENUE REQUIRES A LIVE SESSION"}
            >
              <SelectInput
                value={form.connector_mode}
                onChange={(value) => {
                  if (value === "LIVE_VENUE" && mode !== "LIVE") {
                    setNote({ tone: "bear", message: "LIVE_VENUE REQUIRES A LIVE SESSION" });
                    return;
                  }
                  setForm((current) => ({ ...current, connector_mode: value }));
                }}
                options={CONNECTOR_MODES.map((option) => ({
                  value: option,
                  label: option === "LIVE_VENUE" && mode !== "LIVE" ? `${option} · LIVE ONLY` : option,
                }))}
              />
            </Field>
            <Field label="Max Drawdown (bps)" hint="0-10000">
              <TextInput
                value={form.max_drawdown_bps}
                onChange={(event) =>
                  setForm((current) => ({ ...current, max_drawdown_bps: event.target.value }))
                }
                inputMode="decimal"
                mono
              />
            </Field>
            <Field label="Slippage Alert (bps)">
              <TextInput
                value={form.alert_slippage_bps}
                onChange={(event) =>
                  setForm((current) => ({ ...current, alert_slippage_bps: event.target.value }))
                }
                inputMode="decimal"
                mono
              />
            </Field>
            <Field label="Drift Threshold" hint="0-1">
              <TextInput
                value={form.alert_drift_threshold}
                onChange={(event) =>
                  setForm((current) => ({ ...current, alert_drift_threshold: event.target.value }))
                }
                inputMode="decimal"
                mono
              />
            </Field>
            <Field label="Reserve Floor %" hint="reserve + escrow <= 90">
              <TextInput
                value={form.default_reserve_floor_pct}
                onChange={(event) =>
                  setForm((current) => ({ ...current, default_reserve_floor_pct: event.target.value }))
                }
                inputMode="numeric"
                mono
              />
            </Field>
            <Field label="Tax Escrow %">
              <TextInput
                value={form.default_tax_escrow_pct}
                onChange={(event) =>
                  setForm((current) => ({ ...current, default_tax_escrow_pct: event.target.value }))
                }
                inputMode="numeric"
                mono
              />
            </Field>
            <Field label="Autopilot Universe" hint="1-5000 symbols">
              <TextInput
                value={form.autopilot_max_symbols}
                onChange={(event) =>
                  setForm((current) => ({ ...current, autopilot_max_symbols: event.target.value }))
                }
                inputMode="numeric"
                mono
              />
            </Field>
          </div>
          <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-hairline pt-2">
            <Checkbox
              checked={form.allow_network}
              onChange={(checked) => setForm((current) => ({ ...current, allow_network: checked }))}
              label="allow_network — a socket may only open when this is explicitly true"
            />
            <Checkbox
              checked={form.lifecycle_enabled}
              onChange={(checked) => setForm((current) => ({ ...current, lifecycle_enabled: checked }))}
              label="lifecycle supervisor enabled"
            />
            <Button
              variant="primary"
              icon="check"
              disabled={busy !== null}
              onClick={() => void saveSettings()}
            >
              {busy === "settings" ? "SAVING…" : "Persist Settings"}
            </Button>
          </div>
          <div className="mt-1 text-[10px] text-ink-3">
            revision {int(settings.revision_count)} · sha256 {shortHash(settings.settings_sha256, 12)} ·
            live-critical {Array.from(liveCritical).join(" · ")}
          </div>
        </Card>

        <Card
          title="Write-Only Credential Vault"
          subtitle="Testnet credentials are sealed in Ring 0 · only ***last4 returns"
          icon="lock"
        >
          <div className="grid grid-cols-2 gap-2">
            <Field label="Venue" className="col-span-2">
              <SelectInput
                value={credVenue}
                onChange={setCredVenue}
                options={VENUES.map((venue) => ({ value: venue, label: `${venue} · ${VENUE_SILO[venue]}` }))}
              />
            </Field>
            <Field label="API Key" hint="never echoed">
              <TextInput
                type="password"
                value={credKey}
                onChange={(event) => setCredKey(event.target.value)}
                autoComplete="off"
                mono
              />
            </Field>
            <Field label="API Secret" hint="never echoed">
              <TextInput
                type="password"
                value={credSecret}
                onChange={(event) => setCredSecret(event.target.value)}
                autoComplete="off"
                mono
              />
            </Field>
          </div>
          <div className="mt-2 flex items-center justify-between gap-2">
            <span className="text-[10px] uppercase tracking-[0.1em] text-ink-3">
              {credMask.length > 0
                ? `WRITTEN — VALUE NOT ECHOED · mask ${credMask}`
                : "no credential written this session"}
            </span>
            <Button
              variant="primary"
              icon="key"
              disabled={busy !== null}
              onClick={() => void writeCredentials()}
            >
              {busy === "credentials" ? "WRITING…" : "Write Credential"}
            </Button>
          </div>
          <div className="mt-1 text-[10px] text-ink-3">
            LIVE-guarded: the credential route answers LIVE_ELEVATION_REQUIRED to a PAPER session. Nothing is
            written when the tier check fails.
          </div>
          <div className="mt-2 border-t border-hairline pt-2">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <Checkbox
                checked={rotateConfirm}
                onChange={setRotateConfirm}
                label="Rotate the Ring 0 vault key. Re-encrypts every stored secret with zero downtime."
              />
              <Button
                variant="danger"
                icon="refresh"
                disabled={!rotateConfirm || busy !== null}
                onClick={() => void rotateKeys()}
              >
                {busy === "rotate" ? "ROTATING…" : "Rotate Keys"}
              </Button>
            </div>
            {mode === "LIVE" ? null : (
              <div className="mt-1 text-[10px] text-ink-3">
                rotation is LIVE-guarded · a PAPER session receives LIVE_ELEVATION_REQUIRED and no key is
                touched.
              </div>
            )}
            {rotation ? (
              <KeyValue
                className="mt-2"
                columns={3}
                items={[
                  { label: "Zero Downtime", value: text(rotation.zero_downtime), tone: "bull" },
                  { label: "Zero Loss", value: text(rotation.credentials_zero_loss), tone: "bull" },
                  { label: "Elapsed", value: `${num(rotation.elapsed_ms, 1)} ms` },
                  { label: "Venues", value: int(rotation.broker_venues) },
                  { label: "Version", value: int(rotation.key_version) },
                  { label: "Fingerprint", value: shortHash(rotation.ed25519_public_fingerprint, 14) },
                ]}
              />
            ) : null}
          </div>
        </Card>
      </div>

      <Card
        title={`LLM Pool · target $${LLM_TARGET_DAILY_USD.toFixed(2)}/day`}
        subtitle="System 1 stays on the local daemon under a 250 ms hard cap · a dead lease abstains, zero tokens"
        icon="zap"
        actions={
          <Button
            variant="subtle"
            size="sm"
            icon="activity"
            disabled={busy !== null}
            onClick={() => void probeLlms()}
          >
            {busy === "llm" ? "PROBING…" : "Probe Providers"}
          </Button>
        }
      >
        <div className="mb-2">
          <ProgressBar
            value={llmSpend}
            max={llmCap > 0 ? llmCap : LLM_TARGET_DAILY_USD}
            tone={llmSpend > llmCap ? "bear" : "accent"}
            height={6}
            label={`$${num(llmSpend, 4)} of $${num(llmCap, 2)} cap · remaining $${num(llm.budget_remaining_usd, 4)}`}
          />
        </div>
        {llmRows.length === 0 ? (
          <EmptyState
            title="NO LLM ROWS"
            hint="The cockpit state did not expose a provider slot. Probe the pool to populate it."
          />
        ) : (
          <Table dense columns={["PROVIDER", "MODEL", "LATENCY", "STATE", "SPEND", "CAP"]}>
            {llmRows.map((slot, index) => {
              const provider = text(slot.provider, "—");
              return (
                <Row key={`${provider}-${index}`}>
                  <Cell className="font-semibold text-ink">{provider}</Cell>
                  <Cell mono className="text-[10px]">
                    {text(slot.model, "—")}
                  </Cell>
                  <Cell align="right">{num(slot.last_latency_ms, 2)} ms</Cell>
                  <Cell align="center">
                    <Badge tone={slot.circuit_open === true ? "bear" : "bull"}>
                      {slot.circuit_open === true ? "CIRCUIT OPEN" : "CLOSED"}
                    </Badge>
                  </Cell>
                  <Cell align="right">${num(slot.spend_usd, 4)}</Cell>
                  <Cell align="right" tone="muted">
                    ${num(slot.daily_spend_cap_usd, 2)}
                  </Cell>
                </Row>
              );
            })}
          </Table>
        )}
      </Card>

      <Card
        title="MCP Console · Air-Gapped"
        subtitle="Five read-only tools · no order-signing surface · session-guarded"
        icon="terminal"
        actions={
          <Badge tone="muted" mono>
            {int(tools.length)} TOOLS
          </Badge>
        }
      >
        <div className="grid gap-3 xl:grid-cols-2">
          <div>
            <Field
              label="Tool"
              hint={requiredFields.length > 0 ? `required: ${requiredFields}` : "no required fields"}
            >
              <SelectInput
                value={mcpTool}
                onChange={setMcpTool}
                options={tools.map((tool) => ({ value: text(tool.name, ""), label: text(tool.name, "") }))}
              />
            </Field>
            <div className="mt-2">
              <span className="mb-1 block text-[10px] font-medium uppercase tracking-[0.14em] text-ink-3">
                Arguments (JSON)
              </span>
              <TextArea value={mcpBody} onChange={setMcpBody} rows={8} placeholder="{}" />
            </div>
            <div className="mt-2 flex items-center justify-between gap-2">
              <span className="text-[10px] text-ink-3">
                {text(selectedTool?.description, "select a tool")}
              </span>
              <Button variant="primary" icon="play" disabled={busy !== null} onClick={() => void callTool()}>
                {busy === "mcp" ? "CALLING…" : "Call Tool"}
              </Button>
            </div>
          </div>
          <div>
            {mcpResult === null ? (
              <EmptyState
                title="NO TOOL RESULT"
                hint="Every call returns a sealed read-only payload; nothing here can sign an order."
              />
            ) : (
              <JsonBlock value={mcpResult} className="max-h-[280px]" />
            )}
          </div>
        </div>
      </Card>

      <Card
        title="Factory Reset"
        subtitle="Destroys the operator credential and zeroes the workspace · irreversible"
        icon="warning"
      >
        <div className="grid gap-2 md:grid-cols-3">
          <Field label="Password" hint="current operator password">
            <TextInput
              type="password"
              value={resetPassword}
              onChange={(event) => setResetPassword(event.target.value)}
              autoComplete="off"
            />
          </Field>
          <Field label="Live PIN" hint="4-12 digits">
            <TextInput
              type="password"
              value={resetPin}
              onChange={(event) => setResetPin(event.target.value.replace(/[^0-9]/g, ""))}
              inputMode="numeric"
              maxLength={12}
              mono
            />
          </Field>
          <Field label="Type RESET" hint="both factors plus this word are required">
            <TextInput value={resetPhrase} onChange={(event) => setResetPhrase(event.target.value)} mono />
          </Field>
        </div>
        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-[10px] text-ink-3">
            {resetDone ? "FACTORY RESET COMPLETE" : "the session cookie is destroyed by Ring 3 on success"}
          </span>
          <div className="flex items-center gap-2">
            {resetDone ? (
              <a
                href="/setup"
                className="rounded-[4px] border border-accent bg-accent px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.1em] text-[#0D0E10]"
              >
                Provision again
              </a>
            ) : null}
            <Button
              variant="danger"
              icon="warning"
              disabled={!resetReady || busy !== null}
              onClick={() => void factoryReset()}
            >
              {busy === "reset" ? "RESETTING…" : "Factory Reset"}
            </Button>
          </div>
        </div>
      </Card>

      <div className="flex items-center gap-2 text-[10px] uppercase tracking-[0.1em] text-ink-3">
        <span>last sync {clockNs(lastSync * 1e6)} UTC</span>
        <span>·</span>
        <span>now {clockNs(now * 1e6)} UTC</span>
        <span>·</span>
        <span>alerts poll 10s</span>
      </div>
    </div>
  );
}

export default ControlPage;
