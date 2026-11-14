/**
 * Control room and the air-gapped MCP console.
 *
 * The 22-gate go-live certificate, the credential vault, key rotation, the LLM
 * pool probe, the supervisor transport and the read-only MCP tool inventory.
 */

import { useState, type ReactNode } from "react";
import { api } from "@/lib/api";
import { num, rows, str } from "@/lib/format";
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

const VENUES = ["binance", "okx", "bybit", "dhan", "zerodha", "angel", "upstox", "interactive_brokers", "alpaca", "vnpy_mt5", "vnpy_ctp", "oanda"];

export function ControlPage(): ReactNode {
  const preflight = useApi(() => api.preflight(), []);
  const mcp = useApi(() => api.mcp(), []);
  const vault = useApi(() => api.vaultStatus(), []);
  const [busy, setBusy] = useState(false);
  const [probe, setProbe] = useState<Record<string, unknown> | null>(null);
  const [supervisor, setSupervisor] = useState<Record<string, unknown> | null>(null);
  const [cred, setCred] = useState({ silo: "CRYPTO_BINANCE", venue: "binance", api_key: "", api_secret: "" });
  const [stored, setStored] = useState<Record<string, unknown> | null>(null);

  const gates = rows(preflight.data, "gates");
  const passed = num(preflight.data, "passed_gates", 0);
  const total = num(preflight.data, "total_gates", 22);
  const tools = rows(mcp.data, "tools");
  const passedPct = total > 0 ? (passed / total) * 100 : 0;

  const runPreflight = async () => {
    setBusy(true);
    try {
      const next = await api.runPreflight(false, "ALL");
      toast(`Pre-flight re-run: ${String(next.passed_gates ?? passed)} / ${String(next.total_gates ?? total)}`, "success");
      preflight.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "preflight rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const rotate = async () => {
    setBusy(true);
    try {
      await api.rotateKeys();
      toast("Vault keys rotated", "success");
      vault.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "rotation rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const probeLlms = async () => {
    setBusy(true);
    try {
      const next = await api.llmProbe();
      setProbe(next);
      toast("LLM pool probed", "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "probe rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const supervisorAction = async (action: "start" | "stop" | "status") => {
    setBusy(true);
    try {
      const next = await api.supervisor(action);
      setSupervisor(next);
      toast(`Supervisor ${action} dispatched`, "success");
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "supervisor rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const storeCredential = async () => {
    setBusy(true);
    try {
      const next = await api.brokerCredentials(cred);
      setStored(next);
      toast(`${cred.venue} credential sealed in Ring 0`, "success");
      setCred({ ...cred, api_key: "", api_secret: "" });
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "credential rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const supervisorState = str(supervisor, "status", "").toUpperCase();
  const supervisorRunning =
    supervisorState.includes("RUN") || supervisorState.includes("ARM") || supervisorState.includes("OK");
  const workerCount = num(supervisor, "distinct_os_processes", 4);
  const shmPrefix = str(supervisor, "shm_prefix", "nios");

  return (
    <div className="nx-page">
      <SectionHeader
        kicker="RING 3 - SOVEREIGN CONTROL"
        title="Control room, vault and MCP console"
        sub="Read-only telemetry plus four explicit control endpoints. The global kill switch lives in the live shell, behind a two-step confirmation."
        actions={
          <>
            <StatusBadge tone={passed >= total && total > 0 ? "bull" : "accent"} dot>
              {passed}/{total} GATES
            </StatusBadge>
            <Btn size="sm" tone="primary" onClick={runPreflight} disabled={busy}>
              Re-run pre-flight
            </Btn>
          </>
        }
      />

      <div className="nx-grid nx-grid--4">
        <MetricTile
          label="Go-live certificate"
          value={`${passed} / ${total}`}
          sub="22 hard gates"
          badge={<StatusBadge tone={passed >= total ? "bull" : "accent"}>{passedPct.toFixed(0)}%</StatusBadge>}
        />
        <MetricTile
          label="MCP tools"
          value={String(num(mcp.data, "count", tools.length))}
          sub="air-gapped, read-only"
          badge={<StatusBadge tone="muted">NO WRITE</StatusBadge>}
        />
        <MetricTile
          label="Vault"
          value={str(vault.data, "status", "SEALED").toUpperCase()}
          sub={`${num(vault.data, "keys", 0)} keys held`}
        />
        <MetricTile
          label="LLM pool"
          value={probe ? str(probe, "status", "PROBED").toUpperCase() : "-"}
          sub="Ring 2 only, never the hot path"
        />
      </div>

      <div className="nx-grid nx-grid--2-1">
        <TerminalPanel
          title="22-Gate Pre-Flight Go-Live Readiness Certificate"
          subtitle="Each row names the artefact it inspected"
        >
          <DataGrid
            columns={[
              {
                key: "id",
                label: "Gate",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-mono nx-muted">{str(row, "id", "-")}</span>
                ),
              },
              {
                key: "title",
                label: "Check",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-strong nx-xs">{str(row, "title", str(row, "name", "-"))}</span>
                ),
              },
              {
                key: "detail",
                label: "Evidence",
                render: (row: Record<string, unknown>) => (
                  <span className="nx-xs nx-dim nx-truncate" style={{ maxWidth: 260, display: "inline-block" }}>
                    {str(row, "detail", "-")}
                  </span>
                ),
              },
              {
                key: "status",
                label: "Status",
                align: "right",
                render: (row: Record<string, unknown>) => {
                  const state = str(row, "status", "UNKNOWN").toUpperCase();
                  return <StatusBadge tone={state === "PASS" ? "bull" : state === "FAIL" ? "bear" : "muted"}>{state}</StatusBadge>;
                },
              },
            ]}
            rows={gates}
            getKey={(row, index) => str(row, "id", String(index))}
            empty={
              <EmptyCleanSlate
                title="Certificate not measured"
                body="Press re-run pre-flight to execute the 22 gates. A cached certificate is never presented as a fresh measurement."
                hint="GET /api/v2/control/preflight"
              />
            }
          />
          <div className="nx-hr" style={{ margin: "10px 0" }} />
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted nx-mono">ready_for_live_cutover</span>
            <StatusBadge tone={preflight.data && preflight.data.ready_for_live_cutover === true ? "bull" : "muted"}>
              {preflight.data && preflight.data.ready_for_live_cutover === true ? "TRUE" : "FALSE"}
            </StatusBadge>
          </div>
          <div className="nx-row nx-row--between">
            <span className="nx-xs nx-muted nx-mono">certificate_sha256</span>
            <span className="nx-mono nx-xs nx-dim nx-truncate" style={{ maxWidth: 260, display: "inline-block" }}>
              {str(preflight.data, "certificate_sha256", "not issued")}
            </span>
          </div>
          <span className="nx-xs nx-dim" style={{ display: "block", marginTop: 6 }}>
            GET /api/v2/control/preflight - a cached certificate is replayed with the commit it was measured on.
          </span>
        </TerminalPanel>

        <div className="nx-stack">
          <TerminalPanel title="Ring OS process isolation matrix" subtitle="Four rings, four distinct OS processes">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Supervisor</span>
              <StatusBadge tone={supervisorRunning ? "bull" : "muted"}>
                {supervisorRunning ? "SUPERVISOR RUNNING" : "SUPERVISOR IDLE"}
              </StatusBadge>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">distinct pids</span>
              <span className="nx-sm nx-strong nx-num">{supervisor ? String(workerCount) : "-"}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">SHM prefix</span>
              <span className="nx-xs nx-strong nx-mono">{supervisor ? shmPrefix : "-"}</span>
            </div>
            <div className="nx-row" style={{ marginTop: 8 }}>
              <Btn size="sm" tone="ghost" onClick={() => void supervisorAction("status")} disabled={busy}>
                Status
              </Btn>
              <Btn size="sm" tone="ghost" onClick={() => void supervisorAction("start")} disabled={busy}>
                Start
              </Btn>
              <Btn size="sm" tone="danger" onClick={() => void supervisorAction("stop")} disabled={busy}>
                Stop
              </Btn>
            </div>
            <span className="nx-xs nx-dim" style={{ display: "block", marginTop: 6 }}>
              POST /api/v2/control/supervisor
            </span>
          </TerminalPanel>

          <TerminalPanel title="Ring 0 Zero-Downtime Key Rotation" subtitle="Fernet + Ed25519, OS keystore">
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Sealed keys</span>
              <span className="nx-sm nx-strong nx-num">{num(vault.data, "keys", 0)}</span>
            </div>
            <div className="nx-row nx-row--between">
              <span className="nx-xs nx-muted">Key version</span>
              <span className="nx-xs nx-strong nx-num">{str(vault.data, "key_version", "-")}</span>
            </div>
            <Btn tone="danger" block onClick={rotate} disabled={busy} style={{ marginTop: 8 }}>
              Rotate vault keys
            </Btn>
            <span className="nx-xs nx-dim" style={{ display: "block", marginTop: 6 }}>
              POST /api/v2/control/vault/rotate-keys
            </span>
          </TerminalPanel>

          <TerminalPanel title="LLM Pool & Ollama Probe" subtitle="Ollama, Gemini, Groq, DeepSeek, Claude">
            <Btn tone="primary" block onClick={probeLlms} disabled={busy}>
              Probe pool
            </Btn>
            <span className="nx-xs nx-muted" style={{ display: "block", marginTop: 6 }}>
              {probe ? `Status ${str(probe, "status", "ok")}, ${num(probe, "models", 0)} models answered.` : "Ring 2 only. The microsecond loop holds zero tokens."}
            </span>
            <span className="nx-xs nx-dim" style={{ display: "block", marginTop: 4 }}>
              POST /api/v2/control/llms/probe
            </span>
          </TerminalPanel>
        </div>
      </div>

      <TerminalPanel
        title="Write-Only Broker Credential Vault"
        subtitle="Ring 0 encrypts, signs a dry-run and returns only the last four characters"
        actions={
          <StatusBadge tone={stored ? "bull" : "muted"}>
            {stored ? "SEALED" : "WRITE-ONLY"}
          </StatusBadge>
        }
      >
        <div className="nx-grid nx-grid--4">
          <Field label="Silo">
            <select
              className="nx-select"
              value={cred.silo}
              onChange={(event) => setCred({ ...cred, silo: event.target.value })}
            >
              {["CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"].map((silo) => (
                <option key={silo} value={silo}>
                  {silo}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Venue">
            <select
              className="nx-select"
              value={cred.venue}
              onChange={(event) => setCred({ ...cred, venue: event.target.value })}
            >
              {VENUES.map((venue) => (
                <option key={venue} value={venue}>
                  {venue}
                </option>
              ))}
            </select>
          </Field>
          <Field label="API key">
            <input
              className="nx-input"
              type="password"
              autoComplete="off"
              placeholder="never rendered again"
              value={cred.api_key}
              onChange={(event) => setCred({ ...cred, api_key: event.target.value })}
            />
          </Field>
          <Field label="API secret">
            <input
              className="nx-input"
              type="password"
              autoComplete="off"
              placeholder="Fernet envelope"
              value={cred.api_secret}
              onChange={(event) => setCred({ ...cred, api_secret: event.target.value })}
            />
          </Field>
        </div>
        <div className="nx-row nx-row--between" style={{ marginTop: 10 }}>
          <Btn
            tone="primary"
            size="sm"
            onClick={storeCredential}
            disabled={busy || cred.api_key.length === 0 || cred.api_secret.length === 0}
          >
            Store credential
          </Btn>
          <span className="nx-xs nx-dim nx-mono">
            POST /api/v2/control/brokers/credentials
          </span>
        </div>
        <div className="nx-hr" style={{ margin: "10px 0" }} />
        <div className="nx-row nx-row--between">
          <span className="nx-xs nx-muted">Masked secret</span>
          <span className="nx-xs nx-strong nx-mono">{stored ? str(stored, "api_secret_masked", "-") : "-"}</span>
        </div>
        <div className="nx-row nx-row--between">
          <span className="nx-xs nx-muted">Dry-run verified</span>
          <StatusBadge tone={stored && stored.dry_run_verified === true ? "bull" : "muted"}>
            {stored && stored.dry_run_verified === true ? "ED25519 VERIFIED" : "NOT RUN"}
          </StatusBadge>
        </div>
        <span className="nx-xs nx-dim" style={{ display: "block", marginTop: 6 }}>
          The plaintext never leaves Ring 0; the cockpit keeps verdicts and masks only.
        </span>
      </TerminalPanel>

      <TerminalPanel title="MCP console"
        subtitle="Air-gapped read-only tool inventory"
        flush
        actions={<StatusBadge tone="muted">{str(mcp.data, "transport", "STDIO").toUpperCase()}</StatusBadge>}
      >
        <DataGrid
          columns={[
            {
              key: "name",
              label: "Tool",
              render: (row: Record<string, unknown>) => (
                <span className="nx-mono">{str(row, "name", "-")}</span>
              ),
            },
            {
              key: "description",
              label: "Description",
              render: (row: Record<string, unknown>) => (
                <span className="nx-xs nx-dim nx-truncate" style={{ maxWidth: 520, display: "inline-block" }}>
                  {str(row, "description", "-")}
                </span>
              ),
            },
            {
              key: "kind",
              label: "Access",
              align: "right",
              render: (row: Record<string, unknown>) => (
                <StatusBadge tone={str(row, "kind", "read").toLowerCase().includes("read") ? "bull" : "bear"}>
                  {str(row, "kind", "READ").toUpperCase()}
                </StatusBadge>
              ),
            },
          ]}
          rows={tools}
          getKey={(row, index) => str(row, "name", String(index))}
          empty={
            <EmptyCleanSlate
              title="MCP surface sealed"
              body="No MCP tool is mounted in this build; the Main Core ships before any MCP bridge is authorised."
              hint="deferred scope"
            />
          }
        />
      </TerminalPanel>
    </div>
  );
}
