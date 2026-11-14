/**
 * Paper sandbox shell.
 *
 * Obsidian canvas, cyan + lavender glass, and the four synthetic controls that
 * only ever exist in a paper workspace: deposit simulated capital, zero every
 * value, play/pause the autopilot, and walk through the door into the live
 * vault (which re-authenticates; the paper token is never elevated in place).
 */

import { useMemo, useState, type ReactNode } from "react";
import { ArrowRight, Lock, Pause, Play, Plus, RotateCcw } from "lucide-react";
import { PAGE_BY_KEY } from "@/pages/registry";
import { Sidebar } from "./Sidebar";
import type { ShellProps } from "./types";
import { authApi } from "@/lib/api";
import { useAction, useResource } from "@/lib/use-api";
import { useSession } from "@/lib/session";
import { pushToast, toastError } from "@/lib/toast";
import { money, num } from "@/lib/format";
import {
  ConfirmDangerModal,
  StatusPill,
  TabularValue,
} from "@/design-system/primitives";

const SILOS = ["CRYPTO_BINANCE", "INDIA_ZERODHA_DHAN", "US_IBKR", "FX_MT5"];

function DepositModal({
  open,
  onClose,
  onDone,
}: {
  open: boolean;
  onClose: () => void;
  onDone: () => void;
}): ReactNode {
  const [silo, setSilo] = useState(SILOS[0]);
  const [amount, setAmount] = useState("100000");
  const funding = useAction<Record<string, unknown>>();

  if (!open) return null;

  const submit = async () => {
    const units = Number(amount);
    if (!Number.isFinite(units) || units <= 0) {
      pushToast("Enter a positive capital amount", undefined, "warn");
      return;
    }
    const receipt = await funding.run(() => authApi.fundPaper({ silo, amount_units: units }));
    if (receipt) {
      pushToast("Paper capital deposited", `${silo} · ${units.toLocaleString()} units`, "ok");
      onDone();
      onClose();
    } else if (funding.error) {
      toastError("Deposit rejected", funding.error);
    }
  };

  return (
    <div className="overlay overlay-center">
      <button
        type="button"
        className="overlay-backdrop"
        aria-label="Close deposit dialog"
        onClick={onClose}
      />
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label="Deposit paper capital"
      >
        <header className="flex items-center justify-between border-b border-[var(--line)] p-4">
          <div>
            <p className="eyebrow">Simulated capital</p>
            <h2 className="text-[14px] font-semibold">Deposit paper capital</h2>
          </div>
          <StatusPill value="zero real risk" tone="accent" />
        </header>
        <div className="grid gap-3 p-4">
          <label className="field">
            <span className="field-label">Target silo</span>
            <select className="select" value={silo} onChange={(event) => setSilo(event.target.value)} aria-label="Target silo">
              {SILOS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span className="field-label">Amount (account units)</span>
            <input
              className="input tnum"
              inputMode="decimal"
              value={amount}
              onChange={(event) => setAmount(event.target.value)}
              aria-label="Paper capital amount"
            />
          </label>
          <p className="mono text-[10px] text-[var(--dim)]">
            Credited to ReserveYield first; the 35 / 15 / 50 ratchet only applies to realised profit.
          </p>
          <div className="flex justify-end gap-2">
            <button type="button" className="btn btn-ghost" onClick={onClose}>
              Cancel
            </button>
            <button type="button" className="btn btn-accent" onClick={() => void submit()} disabled={funding.busy}>
              {funding.busy ? "Depositing…" : "Deposit paper capital"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export function PaperShell({ page, children, onNavigate, onPortal }: ShellProps): ReactNode {
  const meta = PAGE_BY_KEY[page];
  const { username, signOut } = useSession();
  const state = useResource("paper-workspace-state", () => authApi.paperState(), 12_000);
  const autopilot = useAction<{ ok: boolean }>();
  const [depositOpen, setDepositOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [resetting, setResetting] = useState(false);

  const navMicros = useMemo(() => {
    const silos = state.data?.silos ?? {};
    return Object.values(silos).reduce((sum, silo) => sum + (Number(silo.nav_micros) || 0), 0);
  }, [state.data]);

  const autopilotOn = Boolean(state.data?.autopilot?.enabled);
  const shadowFills = state.data?.shadow?.shadow_fills ?? 0;
  const requiredFills = state.data?.shadow?.required_fills ?? 30;
  const working = state.data?.orders?.counts?.working ?? 0;

  const toggleAutopilot = async () => {
    const receipt = await autopilot.run(() => authApi.setAutopilot(!autopilotOn));
    if (receipt) {
      pushToast(autopilotOn ? "Autopilot paused" : "Autopilot running", "Paper feed only", "ok");
      state.reload();
    } else if (autopilot.error) {
      toastError("Autopilot control rejected", autopilot.error);
    }
  };

  const resetWorkspace = async () => {
    setResetting(true);
    try {
      await authApi.resetWorkspace();
      pushToast("Paper workspace zeroed", "All vaults, orders and the logbook return to 0", "ok");
      state.reload();
      setResetOpen(false);
    } catch (problem) {
      toastError("Reset rejected", problem instanceof Error ? problem.message : "unknown failure");
    } finally {
      setResetting(false);
    }
  };

  const leave = async () => {
    await signOut();
    onPortal("login");
  };

  return (
    <div className="shell nios-shell-paper" data-collapsed={false}>
      <Sidebar
        workspace="paper"
        active={page}
        onNavigate={onNavigate}
        badges={{
          orders: String(working),
          analyzer: `${shadowFills}/${requiredFills}`,
          dashboard: autopilotOn ? "AUTO" : "OFF",
        }}
        username={username}
        tier="PAPER"
        onSignOut={() => void leave()}
      />

      <div className="main">
        <header className="topbar">
          <div className="topbar-title">
            <h1>{meta.title}</h1>
            <span className="topbar-meta">
              {meta.route} · {meta.ring} · simulated execution
            </span>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <span className="pill pill-accent">
              <span className="pill-dot pulse" aria-hidden="true" />
              PAPER
            </span>
            <button type="button" className="btn btn-sm" onClick={() => onPortal("live-login")}>
              <Lock size={12} aria-hidden="true" />
              Switch to Live Vault
            </button>
          </div>
        </header>

        <div className="ribbon" role="status">
          <span className="pill-dot" aria-hidden="true" />
          PAPER SANDBOX — SIMULATED CAPITAL (ZERO REAL RISK)
          <span className="dim">·</span>
          <span className="tnum">NAV {money(navMicros)}</span>
          <span className="dim">·</span>
          <span>{autopilotOn ? "autopilot running" : "autopilot paused"}</span>
          <div className="ml-auto flex flex-wrap items-center gap-1.5">
            <button type="button" className="btn btn-sm" onClick={() => setDepositOpen(true)}>
              <Plus size={12} aria-hidden="true" />
              Deposit Paper Capital
            </button>
            <button type="button" className="btn btn-sm" onClick={() => void toggleAutopilot()} disabled={autopilot.busy}>
              {autopilotOn ? <Pause size={12} aria-hidden="true" /> : <Play size={12} aria-hidden="true" />}
              {autopilotOn ? "Pause Autopilot" : "Play Autopilot"}
            </button>
            <button type="button" className="btn btn-sm btn-ghost" onClick={() => setResetOpen(true)}>
              <RotateCcw size={12} aria-hidden="true" />
              Reset All Values to Zero
            </button>
          </div>
        </div>

        {navMicros === 0 ? (
          <div className="border-b border-[var(--line)] bg-[var(--accent-soft)] px-5 py-2">
            <div className="row-between mx-auto max-w-[1680px]">
              <p className="text-[12px] text-[var(--accent)]">
                Clean slate — every vault is 0 and no order has ever been placed. Fund the sandbox to begin.
              </p>
              <button type="button" className="btn btn-sm btn-accent" onClick={() => setDepositOpen(true)}>
                Fund sandbox
                <ArrowRight size={11} aria-hidden="true" />
              </button>
            </div>
          </div>
        ) : null}

        <main className="content" id="main-content">
          <div className="content-inner">{children}</div>
        </main>
      </div>

      <DepositModal open={depositOpen} onClose={() => setDepositOpen(false)} onDone={state.reload} />

      <ConfirmDangerModal
        open={resetOpen}
        title="Zero the paper workspace"
        body={
          <span>
            Every paper vault returns to zero, all working orders are cancelled, the shadow nursery resets to{" "}
            <span className="mono">0/{requiredFills}</span> and the autopilot stops. Type RESET to confirm.
          </span>
        }
        requireText="RESET"
        confirmLabel="Reset All Values to Zero"
        busy={resetting}
        onCancel={() => setResetOpen(false)}
        onConfirm={() => void resetWorkspace()}
      />

      <div className="sr-only" aria-live="polite">
        Paper NAV <TabularValue value={navMicros} kind="money" />, {num(shadowFills, 0)} of {num(requiredFills, 0)} shadow fills.
      </div>
    </div>
  );
}
