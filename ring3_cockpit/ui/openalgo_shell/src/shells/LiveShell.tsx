/**
 * LIVE workspace shell â€” real capital.
 *
 * Contains zero paper-workspace controls.  The only write paths are the
 * two-step emergency kill switch and the vault lock that drops the operator
 * back to /login.
 */

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { Badge, Banner, Button, Modal, cx } from "../components/ui";
import { Icon } from "../components/Icon";
import { api, authLogout, errorMessage } from "../lib/api";
import { duration, int, usd } from "../lib/format";
import { navMicros, preflightOf, type PreflightSummary } from "../lib/payload";
import { navigateTo } from "../lib/router";
import { useMarketStream } from "../lib/stream";
import { useSession, useSessionTtl } from "../state/session";
import { AlertsButton, ShellFrame, StreamIndicator, TopBarDivider, TopBarLabel, TopBarShell } from "./ShellFrame";

export function LiveShell({ children }: { children: ReactNode }) {
  const { session, unacknowledged, critical } = useSession();
  const ttl = useSessionTtl();
  const stream = useMarketStream({ max: 40 });
  const [state, setState] = useState<Record<string, unknown>>({});
  const [preflight, setPreflight] = useState<PreflightSummary | null>(null);
  const [killOpen, setKillOpen] = useState(false);
  const [armed, setArmed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const nav = navMicros(state);

  const load = useCallback(async () => {
    try {
      const payload = await api.get<unknown>("/workspace/live/state");
      setState(payload !== null && typeof payload === "object" ? (payload as Record<string, unknown>) : {});
    } catch {
      /* the live workspace may be sealed until elevation; the shell still renders */
    }
  }, []);

  const loadPreflight = useCallback(async () => {
    try {
      const payload = await api.get<unknown>("/control/preflight");
      setPreflight(preflightOf(payload));
    } catch {
      setPreflight(null);
    }
  }, []);

  useEffect(() => {
    void load();
    void loadPreflight();
    const stateTimer = globalThis.setInterval(() => void load(), 12_000);
    const gateTimer = globalThis.setInterval(() => void loadPreflight(), 45_000);
    return () => {
      globalThis.clearInterval(stateTimer);
      globalThis.clearInterval(gateTimer);
    };
  }, [load, loadPreflight]);

  useEffect(() => {
    if (!armed) return;
    const timer = globalThis.setTimeout(() => setArmed(false), 6_000);
    return () => globalThis.clearTimeout(timer);
  }, [armed]);

  const fireKillSwitch = async () => {
    if (!armed) {
      setArmed(true);
      return;
    }
    setBusy(true);
    try {
      await api.post("/control/killswitch", { reason: "operator emergency kill switch", flatten: true, confirm: true });
      setNotice("GLOBAL HALT ACCEPTED â€” ALL SILOS FLATTENING");
    } catch (error) {
      setNotice(errorMessage(error));
    } finally {
      setBusy(false);
      setArmed(false);
      setKillOpen(false);
    }
  };

  const lockVault = async () => {
    setBusy(true);
    try {
      await authLogout();
    } finally {
      setBusy(false);
      navigateTo("/login", { replace: true });
    }
  };

  const gatesPassed = preflight?.passed ?? 0;
  const gatesTotal = preflight?.total ?? 23;
  const gatesReady = preflight?.ready ?? false;

  const topBar = (
    <TopBarShell tone="bear">
      <div className="flex items-center gap-2">
        <Badge tone="bear">ðŸ”’ LIVE PRODUCTION â€” REAL CAPITAL</Badge>
      </div>
      <TopBarDivider />
      <TopBarLabel label="LIVE NAV" value={usd(nav)} />
      <TopBarLabel label="SESSION TTL" value={duration(ttl)} tone={ttl < 120 ? "bear" : undefined} />
      <div
        className={cx(
          "flex items-center gap-1.5 rounded-[4px] border px-1.5 py-[3px] text-[9px] font-semibold uppercase tracking-[0.1em]",
          gatesReady ? "border-bull-border bg-bull-tint text-bull" : "border-bear-border bg-bear-tint text-bear",
        )}
        title={preflight ? `Release ${preflight.release} Â· ${gatesPassed}/${gatesTotal} gates` : "pre-flight unavailable"}
      >
        <Icon name="verification" size={12} />
        23-GATE PRE-FLIGHT
        <span className="tabular-nums">
          {int(gatesPassed)}/{int(gatesTotal)}
        </span>
      </div>
      <StreamIndicator status={stream.status} frames={stream.frames.length} />
      <AlertsButton count={unacknowledged} critical={critical} href="/live/control" />
      <div className="ml-auto flex flex-wrap items-center gap-1.5">
        <Button size="sm" variant="danger" icon="power" onClick={() => setKillOpen(true)} disabled={busy}>
          &lt;1ms Emergency Kill Switch
        </Button>
        <Button size="sm" variant="ghost" icon="lock" onClick={lockVault} disabled={busy}>
          Lock Live Vault
        </Button>
      </div>
    </TopBarShell>
  );

  return (
    <ShellFrame workspace="live" topBar={topBar}>
      <div className="flex min-h-full flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-[4px] border border-bear-border bg-bear-tint px-2.5 py-1.5">
          <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-bear">
            <Icon name="lock" size={12} />
            LIVE PRODUCTION â€” ORDER SIGNING ARMED â€” REAL CAPITAL AT RISK
          </div>
          <div className="flex items-center gap-3 text-[9.5px] uppercase tracking-[0.1em] text-ink-2">
            <span>
              OPERATOR <span className="text-ink">{session?.username ?? "UNSIGNED"}</span>
            </span>
            <span>
              TIER <span className={cx("font-semibold", session?.mode === "LIVE" ? "text-bear" : "text-ink")}>{session?.mode ?? "NONE"}</span>
            </span>
            <span>
              EXPIRES <span className="tabular-nums text-ink">{session?.expiresAtUtc ?? "â€”"}</span>
            </span>
          </div>
        </div>
        {notice !== null ? (
          <Banner tone="bear" icon="warning">
            {notice}
          </Banner>
        ) : null}
        {children}
      </div>

      <Modal
        open={killOpen}
        title="Emergency Global Kill Switch"
        onClose={() => {
          setKillOpen(false);
          setArmed(false);
        }}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setKillOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="danger" onClick={fireKillSwitch} disabled={busy}>
              {armed ? "CONFIRM HALT â€” FLATTEN EVERYTHING" : "ARM KILL SWITCH"}
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <Banner tone="bear" icon="warning">
            This is a one-way global halt: every lease is disarmed, every silo goes reduce-only and open risk is flattened under
            ADR-009. It cannot be undone without a human reset.
          </Banner>
          <div className="rounded-[4px] border border-hairline bg-canvas p-2 font-mono text-[10px] leading-relaxed text-ink-2">
            POST /api/v2/control/killswitch
            <br />
            {`{ "reason": "operator emergency kill switch", "flatten": true, "confirm": true }`}
          </div>
          {armed ? <div className="text-[10px] font-semibold uppercase tracking-[0.12em] text-bear">ARMED â€” SECOND CLICK FIRES</div> : null}
        </div>
      </Modal>
    </ShellFrame>
  );
}
