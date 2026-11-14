/**
 * Live sovereign vault shell.
 *
 * Everything that can move real capital is here and nowhere else: the hazard
 * ribbon, the 22-gate certificate chip, the 30-minute lease countdown, the
 * two-step sub-millisecond kill switch and the vault lock. There is no deposit,
 * no reset, no autopilot transport and no shadow burst in this file.
 */

import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, authApi } from "@/lib/api";
import { clock, num } from "@/lib/format";
import { useSession } from "@/lib/session";
import { toast } from "@/lib/toast";
import { useApi } from "@/lib/use-api";
import { PAGE_BY_KEY } from "@/pages/registry";
import { Btn, ConfirmModal, StatusBadge } from "@/design-system/primitives";
import { Glyph } from "./icons";
import { Sidebar } from "./Sidebar";
import type { ShellProps } from "./types";

const LEASE_MS = 30 * 60 * 1000;

export function LiveShell({ page, onNavigate, onPortal, children }: ShellProps): ReactNode {
  const [collapsed, setCollapsed] = useState(false);
  const [remainingMs, setRemainingMs] = useState(LEASE_MS);
  const [armed, setArmed] = useState(false);
  const [killOpen, setKillOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const session = useSession();
  const expiresAtMs = session.expiresAtMs;
  const preflight = useApi(() => authApi.preflight(), []);
  const meta = PAGE_BY_KEY[page];

  const deadline = useMemo(() => expiresAtMs ?? Date.now() + LEASE_MS, [expiresAtMs]);

  useEffect(() => {
    const tick = () => setRemainingMs(Math.max(0, deadline - Date.now()));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [deadline]);

  useEffect(() => {
    if (!armed) return;
    const timer = window.setTimeout(() => setArmed(false), 6000);
    return () => window.clearTimeout(timer);
  }, [armed]);

  const passedGates = num(preflight.data, "passed_gates", num(preflight.data, "certificate.passed_gates", 0));
  const totalGates = num(preflight.data, "total_gates", num(preflight.data, "certificate.total_gates", 22));

  const fireKill = useCallback(async () => {
    setBusy(true);
    try {
      const report = await api.killSwitch("cockpit two-step kill switch");
      toast(
        `Kill switch armed: ${String(report.leases_revoked ?? "all")} leases revoked, flatten dispatched`,
        "success",
      );
      setKillOpen(false);
    } catch (cause) {
      toast(cause instanceof Error ? cause.message : "kill switch rejected", "error");
    } finally {
      setBusy(false);
      setArmed(false);
    }
  }, []);

  const lock = useCallback(async () => {
    await session.signOut();
    onPortal("paper-login");
  }, [session, onPortal]);

  const ttlCritical = remainingMs < 5 * 60 * 1000;

  return (
    <div className="nx-app nx-app--live nios-shell-live">
      <Sidebar page={page} collapsed={collapsed} onToggle={() => setCollapsed(!collapsed)} onNavigate={onNavigate} />

      <div className="nx-main">
        <div className="nx-live-ribbon nios-hazard" role="alert">
          <Glyph name="shield" size={12} />
          <span>Live production - real capital - every order is signed by Ring 0</span>
          <span style={{ marginLeft: "auto" }} className="nx-num">
            LEASE {clock(remainingMs)}
          </span>
        </div>

        <header className="nx-topbar nx-topbar--live">
          <span className="sr-only">Live vault control bar</span>
          <div className="nx-topbar-title">
            <strong>{meta.title}</strong>
            <span>Live sovereign vault - signed execution only</span>
          </div>

          <div className="nx-topbar-spacer" />

          <div className="nx-topbar-cluster">
            <StatusBadge tone="bear" large dot title="Workspace tier">
              🔒 LIVE PRODUCTION — REAL CAPITAL
            </StatusBadge>
            <StatusBadge
              tone={passedGates >= totalGates ? "bull" : "accent"}
              title="22-gate go-live certificate"
            >
              22-GATE {passedGates}/{totalGates}
            </StatusBadge>
            <span className={ttlCritical ? "nx-live-ttl nx-live-ttl--warn" : "nx-live-ttl"} title="Session lease remaining">
              <Glyph name="backtest" size={12} />
              TTL {clock(remainingMs)}
            </span>

            <div className="nx-topbar-readout">
              <span>Signed by</span>
              <strong>{session.username ?? "unbound"}</strong>
            </div>

            <Btn
              size="sm"
              tone="danger"
              armed={armed}
              onClick={() => {
                if (!armed) {
                  setArmed(true);
                  toast("Kill switch armed - confirm within 6s", "error");
                  return;
                }
                setKillOpen(true);
              }}
            >
              {armed ? "CONFIRM <1ms KILL" : "EMERGENCY KILL SWITCH"}
            </Btn>
            <Btn size="sm" tone="ghost" onClick={lock}>
              <Glyph name="logout" size={12} />
              Lock Live Vault (Drop to Paper)
            </Btn>
          </div>
        </header>

        <main className="nx-content">{children}</main>
      </div>

      <ConfirmModal
        open={killOpen}
        title="Emergency kill switch"
        description="Global halt: every active lease is revoked, the kill latch is set and a flatten is dispatched. This is not a pause."
        confirmLabel="Execute global halt"
        tone="danger"
        busy={busy}
        onConfirm={fireKill}
        onClose={() => {
          setKillOpen(false);
          setArmed(false);
        }}
      >
        <p className="nx-portal-note">
          The latch is read from shared memory on the Ring 1 hot path, so the halt lands in under one
          millisecond. Re-arming requires an explicit operator action from the control room.
        </p>
      </ConfirmModal>
    </div>
  );
}
