/**
 * PAPER workspace shell â€” simulated execution only.
 *
 * Permitted: deposit paper capital, reset the sandbox, drive the autopilot.
 * Forbidden: any real-capital control.  The kill switch lives exclusively in
 * the LIVE shell so a paper operator can never halt live trading by accident.
 */

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { Badge, Banner, Button, Checkbox, Field, Modal, SelectInput, TextInput, cx } from "../components/ui";
import { Icon } from "../components/Icon";
import { SILOS } from "../config/nav";
import { api, errorMessage } from "../lib/api";
import { asRecord, duration, usd } from "../lib/format";
import { boolAt, navMicros } from "../lib/payload";
import { Link } from "../lib/router";
import { useMarketStream } from "../lib/stream";
import { useSession } from "../state/session";
import { AlertsButton, ShellFrame, StreamIndicator, TopBarDivider, TopBarLabel, TopBarShell } from "./ShellFrame";

const SILO_OPTIONS = SILOS.map((silo) => ({ value: silo.id, label: `${silo.short} Â· ${silo.id}` }));

export function PaperShell({ children }: { children: ReactNode }) {
  const { session, unacknowledged, critical } = useSession();
  const stream = useMarketStream({ max: 40 });
  const [state, setState] = useState<Record<string, unknown>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [noticeTone, setNoticeTone] = useState<"neutral" | "bull" | "bear">("neutral");
  const [busy, setBusy] = useState<string | null>(null);
  const [depositOpen, setDepositOpen] = useState(false);
  const [resetOpen, setResetOpen] = useState(false);
  const [silo, setSilo] = useState("CRYPTO_BINANCE");
  const [amount, setAmount] = useState("100000");
  const [resetConfirm, setResetConfirm] = useState(false);

  const nav = navMicros(state);
  const autopilot = boolAt(state, ["autopilot_enabled", "autopilot", "autopilot_active", "tape_running"], false);

  const load = useCallback(async () => {
    try {
      const payload = await api.get<unknown>("/workspace/paper/state");
      setState(asRecord(payload));
    } catch (error) {
      setNotice(errorMessage(error));
      setNoticeTone("bear");
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = globalThis.setInterval(() => void load(), 12_000);
    return () => globalThis.clearInterval(timer);
  }, [load]);

  const act = useCallback(
    async (label: string, run: () => Promise<unknown>, success = "OK") => {
      setBusy(label);
      setNotice(null);
      try {
        await run();
        setNotice(`${label} Â· ${success}`);
        setNoticeTone("bull");
        await load();
      } catch (error) {
        setNotice(`${label} Â· ${errorMessage(error)}`);
        setNoticeTone("bear");
      } finally {
        setBusy(null);
      }
    },
    [load],
  );

  const toggleAutopilot = () =>
    act("AUTOPILOT", () => api.post("/control/workspace/autopilot", { enabled: !autopilot }), autopilot ? "PAUSED" : "RUNNING");

  const tickAutopilot = () => act("AUTOPILOT TICK", () => api.post("/control/autopilot/tick", {}), "TICKED");

  const submitDeposit = () => {
    const units = Number(amount);
    if (!Number.isFinite(units) || units <= 0) {
      setNotice("DEPOSIT Â· amount must be a positive finite number");
      setNoticeTone("bear");
      return;
    }
    void act("DEPOSIT PAPER CAPITAL", () => api.post("/control/ledger/fund-paper", { silo, amount_units: units, vault: "ALL_CANONICAL" }), "FUNDED").then(() =>
      setDepositOpen(false),
    );
  };

  const submitReset = () => {
    void act(
      "RESET ALL VALUES",
      () => api.post("/control/workspace/reset", { reason: "operator reset to zero", workspace: "paper", confirm: true }),
      "ZEROED",
    ).then(() => {
      setResetOpen(false);
      setResetConfirm(false);
    });
  };

  const topBar = (
    <TopBarShell>
      <div className="flex items-center gap-2">
        <Badge tone="accent">PAPER SANDBOX</Badge>
        <span className="hidden text-[9px] uppercase tracking-[0.12em] text-ink-3 sm:inline">SIMULATED EXECUTION ONLY</span>
      </div>
      <TopBarDivider />
      <TopBarLabel label="SIMULATED NAV" value={usd(nav)} />
      <TopBarLabel label="AUTOPILOT" value={autopilot ? "RUNNING" : "PAUSED"} tone={autopilot ? "bull" : undefined} />
      <TopBarDivider />
      <StreamIndicator status={stream.status} frames={stream.frames.length} />
      <AlertsButton count={unacknowledged} critical={critical} href="/paper/control" />
      <div className="ml-auto flex flex-wrap items-center gap-1.5">
        <Button size="sm" variant={autopilot ? "success" : "subtle"} icon={autopilot ? "pause" : "play"} onClick={toggleAutopilot} disabled={busy !== null}>
          {autopilot ? "Pause" : "Play"}
        </Button>
        <Button size="sm" variant="ghost" icon="zap" onClick={tickAutopilot} disabled={busy !== null}>
          Manual Tick
        </Button>
        <Button size="sm" variant="primary" icon="plus" onClick={() => setDepositOpen(true)}>
          Deposit Paper Capital
        </Button>
        <Button size="sm" variant="danger" icon="refresh" onClick={() => setResetOpen(true)}>
          Reset to Zero
        </Button>
        <Link
          to="/live/login"
          className="inline-flex h-6 items-center gap-1 rounded-[4px] border border-bear-border bg-bear-tint px-2 text-[10px] font-medium uppercase tracking-[0.08em] text-bear hover:border-[#6A3538]"
        >
          <Icon name="lock" size={11} />
          Live Vault
        </Link>
      </div>
    </TopBarShell>
  );

  return (
    <ShellFrame workspace="paper" topBar={topBar}>
      <div className="flex min-h-full flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-[4px] border border-[#3A2A1E] bg-[#1E1712] px-2.5 py-1.5">
          <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-[0.14em] text-accent">
            <Icon name="info" size={12} />
            PAPER SANDBOX â€” SIMULATED EXECUTION ONLY
          </div>
          <div className="flex items-center gap-3 text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
            <span>
              OPERATOR <span className="text-ink-2">{session?.username ?? "UNSIGNED"}</span>
            </span>
            <span>
              TTL <span className="tabular-nums text-ink-2">{duration(session?.ttlRemainingS ?? 0)}</span>
            </span>
            <span>
              CLEAN SLATE <span className={cx("tabular-nums", session?.cleanSlate ? "text-bull" : "text-ink-2")}>{session?.cleanSlate ? "TRUE" : "FALSE"}</span>
            </span>
          </div>
        </div>
        {notice !== null ? (
          <Banner tone={noticeTone === "bull" ? "bull" : noticeTone === "bear" ? "bear" : "neutral"} icon={noticeTone === "bear" ? "warning" : "check"}>
            {notice}
          </Banner>
        ) : null}
        {children}
      </div>

      <Modal
        open={depositOpen}
        title="Deposit Paper Capital"
        onClose={() => setDepositOpen(false)}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setDepositOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="primary" onClick={submitDeposit} disabled={busy !== null}>
              Fund Sandbox
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <p className="text-[11px] leading-relaxed text-ink-3">
            Paper capital is virtual. No wire, no ledger movement, no tax event. It exists so the shadow twin can graduate
            strategies before any real currency is exposed.
          </p>
          <Field label="Silo">
            <SelectInput value={silo} onChange={setSilo} options={SILO_OPTIONS} />
          </Field>
          <Field label="Amount (units)" hint="1 unit = 1,000,000 micros. Maximum 1,000,000,000 units.">
            <TextInput value={amount} onChange={(event) => setAmount(event.target.value)} inputMode="decimal" mono />
          </Field>
        </div>
      </Modal>

      <Modal
        open={resetOpen}
        title="Reset All Values to Zero"
        onClose={() => setResetOpen(false)}
        footer={
          <>
            <Button size="sm" variant="ghost" onClick={() => setResetOpen(false)}>
              Cancel
            </Button>
            <Button size="sm" variant="danger" onClick={submitReset} disabled={!resetConfirm || busy !== null}>
              Zero The Sandbox
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <Banner tone="bear" icon="warning">
            This zeroes every paper vault, every mission and every tranche in the sandbox. Live state is untouched.
          </Banner>
          <Checkbox checked={resetConfirm} onChange={setResetConfirm} label="I intend to destroy the entire simulated workspace and start from a clean slate." />
        </div>
      </Modal>
    </ShellFrame>
  );
}
