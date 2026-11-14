/**
 * Live production shell.
 *
 * Matte carbon, crimson + amber, a hazard rail above the header and no
 * synthetic controls of any kind: the only write actions on this surface are
 * the two-step emergency kill switch and dropping the vault back to paper.
 * The 30-minute session lease is shown ticking at all times.
 */

import { useMemo, useState, type ReactNode } from "react";
import { ArrowLeftRight, Lock, ShieldAlert, Siren, Timer } from "lucide-react";
import { PAGE_BY_KEY } from "@/pages/registry";
import { Sidebar } from "./Sidebar";
import type { ShellProps } from "./types";
import { api, authApi } from "@/lib/api";
import { useAction, useNow, useResource } from "@/lib/use-api";
import { useSession } from "@/lib/session";
import { pushToast, toastError } from "@/lib/toast";
import { num } from "@/lib/format";
import { ConfirmDangerModal, StatusPill } from "@/design-system/primitives";

export function LiveShell({ page, children, onNavigate, onPortal }: ShellProps): ReactNode {
  const meta = PAGE_BY_KEY[page];
  const { username, signOut, expiresAtMs } = useSession();
  const now = useNow(1000);
  const preflight = useResource("live-preflight", () => authApi.preflight(), 30_000);
  const liveState = useResource("live-workspace-state", () => authApi.liveState(), 12_000);
  const kill = useAction<Record<string, unknown>>();
  const [killOpen, setKillOpen] = useState(false);
  const [dropping, setDropping] = useState(false);

  const remainingS = useMemo(() => {
    if (!expiresAtMs) return null;
    return Math.max(0, Math.round((expiresAtMs - now) / 1000));
  }, [expiresAtMs, now]);

  const expired = remainingS !== null && remainingS <= 0;
  const ttl = remainingS === null ? "—" : `${String(Math.floor(remainingS / 60)).padStart(2, "0")}:${String(remainingS % 60).padStart(2, "0")}`;

  const certificate = preflight.data;
  const passed = certificate?.passed_gates ?? 0;
  const total = certificate?.total_gates ?? 22;
  const ready = certificate?.ready_for_live_cutover === true;

  const liveFills = liveState.data?.live_fills ?? 0;
  const brokersConfigured = Object.values(liveState.data?.silos ?? {}).filter((silo) => silo.configured).length;

  const executeKill = async () => {
    const receipt = await kill.run(() => api.killSwitch("operator emergency kill switch from the live vault"));
    if (receipt) {
      pushToast("Emergency kill switch executed", "Global halt latch armed; every lease disarmed", "bad");
      setKillOpen(false);
      preflight.reload();
      liveState.reload();
    } else if (kill.error) {
      toastError("Kill switch rejected", kill.error);
    }
  };

  const dropToPaper = async () => {
    setDropping(true);
    try {
      await signOut();
      onPortal("paper-login");
    } finally {
      setDropping(false);
    }
  };

  const leave = async () => {
    await signOut();
    onPortal("login");
  };

  return (
    <div className="shell nios-shell-live" data-collapsed={false}>
      <Sidebar
        workspace="live"
        active={page}
        onNavigate={onNavigate}
        badges={{
          control: `${passed}/${total}`,
          brokers: `${brokersConfigured}/4`,
          orders: num(liveFills, 0),
        }}
        username={username}
        tier="LIVE"
        onSignOut={() => void leave()}
      />

      <div className="main">
        <div className="hazard-rail" role="presentation" />

        <header className="topbar">
          <div className="topbar-title">
            <h1>{meta.title}</h1>
            <span className="topbar-meta">
              {meta.route} · {meta.ring} · real-capital execution
            </span>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <span className="pill pill-bad">
              <Lock size={10} aria-hidden="true" />
              🔒 LIVE PRODUCTION — REAL CAPITAL
            </span>
            <span className="ttl" data-expired={expired} title="Live session lease (30 minute TTL)">
              <Timer size={10} className="mr-1 inline" aria-hidden="true" />
              LEASE {ttl}
            </span>
            <button
              type="button"
              className="btn btn-danger"
              onClick={() => setKillOpen(true)}
              aria-label="Arm the two-step emergency kill switch"
            >
              <Siren size={12} aria-hidden="true" />
              EMERGENCY KILL SWITCH
            </button>
          </div>
        </header>

        <div className="ribbon ribbon-live" role="status">
          <ShieldAlert size={12} aria-hidden="true" />
          LIVE VAULT — EVERY ORDER IS SIGNED BY RING 0 AND SETTLES REAL CAPITAL
          <span className="dim">·</span>
          <span className="tnum">fills {num(liveFills, 0)}</span>
          <span className="dim">·</span>
          <span className="tnum">
            22-gate pre-flight {passed}/{total}
          </span>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            <StatusPill value={ready ? "PRE-FLIGHT GREEN" : certificate ? "PRE-FLIGHT ATTENTION" : "PRE-FLIGHT PENDING"} tone={ready ? "ok" : certificate ? "warn" : "neutral"} />
            <button
              type="button"
              className="btn btn-sm"
              onClick={() => void dropToPaper()}
              disabled={dropping}
              aria-label="Lock the live vault and drop back to the paper sandbox"
            >
              <ArrowLeftRight size={12} aria-hidden="true" />
              Lock Live Vault (Drop to Paper)
            </button>
          </div>
        </div>

        {expired ? (
          <div className="border-b border-[rgba(244,63,94,0.4)] bg-[rgba(244,63,94,0.12)] px-5 py-2">
            <div className="row-between mx-auto max-w-[1680px]">
              <p className="text-[12px] text-[#fecdd3]">
                The 30-minute live lease has expired. Ring 1 will not accept a new intent until the vault door is re-opened.
              </p>
              <button type="button" className="btn btn-sm btn-danger" onClick={() => onPortal("live-login")}>
                Re-open vault door
              </button>
            </div>
          </div>
        ) : null}

        <main className="content" id="main-content">
          <div className="content-inner">{children}</div>
        </main>
      </div>

      <ConfirmDangerModal
        open={killOpen}
        title="Two-step emergency kill switch"
        body={
          <span>
            This arms the global halt latch (<span className="mono">&lt;1 ms</span>), disarms every Ring 1 lease, cancels all
            working orders and flattens every silo. The vault stays reduce-only until a human re-arms it. Type KILL to fire.
          </span>
        }
        requireText="KILL"
        confirmLabel="Fire kill switch"
        busy={kill.busy}
        onCancel={() => setKillOpen(false)}
        onConfirm={() => void executeKill()}
      />

      <div className="sr-only" aria-live="polite">
        Live session lease {ttl}, pre-flight {passed} of {total} gates passed.
      </div>
    </div>
  );
}
