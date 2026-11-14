/**
 * Paper sandbox shell.
 *
 * Slate neutral because nothing here touches real capital: a simulated NAV
 * readout, the autopilot transport, a funding door and a hard reset to zero.
 * The live armament - hazard ribbon, lease countdown, kill switch - is absent by
 * construction, not hidden by a flag.
 */

import { useCallback, useEffect, useState, type ReactNode } from "react";
import { authApi } from "@/lib/api";
import { money } from "@/lib/format";
import { toast } from "@/lib/toast";
import { useApi } from "@/lib/use-api";
import { PAGE_BY_KEY } from "@/pages/registry";
import { Btn, ConfirmModal, Field, StatusBadge } from "@/design-system/primitives";
import { Glyph } from "./icons";
import { Sidebar } from "./Sidebar";
import type { ShellProps } from "./types";
import { bool, num, pick, str } from "@/lib/format";

export function PaperShell({ page, onNavigate, onPortal, children }: ShellProps): ReactNode {
  const [collapsed, setCollapsed] = useState(false);
  const [fundOpen, setFundOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [silo, setSilo] = useState("INDIA");
  const [amount, setAmount] = useState("100000");
  const paper = useApi(() => authApi.paperState(), []);

  const nav = paper.data;
  const autopilotEnabled = bool(nav, "autopilot.enabled");
  const totalMicros = num(nav, "nav.total_micros", num(nav, "nav_micros", 0));
  const cleanSlate = bool(nav, "clean_slate_active", true);
  const meta = PAGE_BY_KEY[page];

  const toggleAutopilot = useCallback(async () => {
    try {
      const next = await authApi.setAutopilot(!autopilotEnabled);
      toast(next.autopilot.enabled ? "Autopilot engaged" : "Autopilot paused", "success");
      paper.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "autopilot rejected", "error");
    }
  }, [autopilotEnabled, paper]);

  useEffect(() => {
    const timer = window.setInterval(() => paper.reload(), 15000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const fund = async () => {
    setBusy(true);
    try {
      await authApi.fundPaper({ silo, amount_units: Number(amount) || 0 });
      toast(`Funded ${silo} with ${amount} paper units`, "success");
      setFundOpen(false);
      paper.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "funding rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  const reset = async () => {
    setBusy(true);
    try {
      await authApi.resetWorkspace();
      toast("Paper workspace reset to zero", "success");
      setResetOpen(false);
      paper.reload();
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "reset rejected", "error");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-app nx-app--paper nios-shell-paper">
      <Sidebar page={page} collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)} onNavigate={onNavigate} />

      <div className="nx-main">
        <header className="nx-topbar">
          <span className="sr-only">Paper sandbox control bar</span>
          <div className="nx-topbar-title">
            <strong>{meta.title}</strong>
            <span>Paper sandbox - simulated fills only</span>
          </div>

          <div className="nx-topbar-spacer" />

          <div className="nx-topbar-cluster">
            <StatusBadge tone="neutral" dot title="Workspace tier">
              PAPER SANDBOX
            </StatusBadge>
            <StatusBadge tone={cleanSlate ? "muted" : "accent"} title="Clean slate state">
              {cleanSlate ? "CLEAN SLATE 0" : "FUNDED"}
            </StatusBadge>
            {autopilotEnabled ? (
              <StatusBadge tone="bull" dot>
                AUTOPILOT RUNNING
              </StatusBadge>
            ) : (
              <StatusBadge tone="muted">AUTOPILOT PAUSED</StatusBadge>
            )}

            <div className="nx-topbar-readout">
              <span>Simulated NAV</span>
              <strong>{money(totalMicros)}</strong>
            </div>

            <Btn size="sm" tone="ghost" onClick={toggleAutopilot}>
              {autopilotEnabled ? "Pause Autopilot" : "Play Autopilot"}
            </Btn>
            <Btn size="sm" onClick={() => setFundOpen(true)}>
              Deposit Paper Capital
            </Btn>
            <Btn size="sm" tone="ghost" onClick={() => setResetOpen(true)}>
              Reset All Values to Zero
            </Btn>
            <Btn size="sm" tone="primary" onClick={() => onPortal("live-login")}>
              <Glyph name="shield" size={12} />
              Switch to Live Vault
            </Btn>
          </div>
        </header>

        <main className="nx-content">{children}</main>
      </div>

      <ConfirmModal
        open={fundOpen}
        title="Deposit paper capital"
        description="Simulated units only. The paper ledger is a mirror; it can never settle against a broker."
        confirmLabel="Fund sandbox"
        busy={busy}
        onConfirm={fund}
        onClose={() => setFundOpen(false)}
      >
        <Field label="Silo">
          <select className="nx-select" value={silo} onChange={(event) => setSilo(event.target.value)}>
            <option value="INDIA">INDIA</option>
            <option value="US">US</option>
            <option value="FX">FX</option>
            <option value="CRYPTO">CRYPTO</option>
          </select>
        </Field>
        <Field label="Units" hint="Deposits are written to the ReserveYield vault.">
          <input
            className="nx-input"
            inputMode="numeric"
            value={amount}
            onChange={(event) => setAmount(event.target.value)}
          />
        </Field>
        <p className="nx-portal-note">
          Current sandbox NAV {money(totalMicros)} across {Object.keys((pick(nav, "silos") as object) ?? {}).length || 0} silos.
          {str(nav, "autopilot.mode") ? ` Autopilot mode ${str(nav, "autopilot.mode")}.` : ""}
        </p>
      </ConfirmModal>

      <ConfirmModal
        open={resetOpen}
        title="Reset to zero"
        description="This clears every paper order, fill, logbook row and shadow-fill counter. It cannot be undone."
        confirmLabel="Reset to Zero"
        tone="danger"
        busy={busy}
        onConfirm={reset}
        onClose={() => setResetOpen(false)}
      >
        <p className="nx-portal-note">
          The live vault is not touched. Live capital can only be changed from the live shell behind a
          step-up PIN.
        </p>
      </ConfirmModal>
    </div>
  );
}
