/**
 * `/setup` — first-run sovereign operator onboarding.
 *
 * Creates the single operator, the six-digit live PIN and a guaranteed
 * clean-slate workspace: every vault zero, no orders, autopilot off.
 */

import { useState, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, CheckCircle2, Fingerprint } from "lucide-react";
import type { Portal } from "@/lib/router";
import { navigate } from "@/lib/router";
import { authApi } from "@/lib/api";
import { useAction } from "@/lib/use-api";
import { useSession } from "@/lib/session";
import { pushToast } from "@/lib/toast";
import type { AuthSession } from "@/lib/types";
import { PortalScaffold } from "./PortalScaffold";
import { ErrorBlock, StatusPill } from "@/design-system/primitives";

const CLEAN_SLATE = [
  "All Vaults = 0",
  "Orders = 0",
  "Autopilot = OFF",
  "Shadow nursery 0/30",
  "Logbook empty",
];

export function SetupPortal({ onPortal }: { onPortal: (portal: Portal) => void }): ReactNode {
  const { signIn } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pin, setPin] = useState("");
  const setup = useAction<AuthSession>();

  const pinValid = /^\d{6}$/.test(pin);
  const passwordValid = password.length >= 8;
  const ready = username.trim().length >= 3 && passwordValid && pinValid && !setup.busy;

  const submit = async () => {
    if (!ready) return;
    const receipt = await setup.run(() =>
      authApi.setup({ username: username.trim(), password, live_pin: pin }),
    );
    if (receipt) {
      signIn(receipt);
      pushToast("Sovereign operator created", "Clean slate sealed · 0 everywhere", "ok");
      navigate("/login");
    }
  };

  return (
    <PortalScaffold
      mode="neutral"
      eyebrow="First run · sovereign onboarding"
      title="Create the operator"
      subtitle="One operator, one live PIN, one clean slate. NIOS never ships with demo capital, demo orders or a pre-armed autopilot."
      aside={
        <div className="flex h-full flex-col justify-between gap-8">
          <div>
            <p className="eyebrow">Clean-slate initialization</p>
            <h2 className="mt-2 text-[22px] leading-tight font-semibold">Nothing is pre-funded. Nothing is pre-armed.</h2>
            <p className="mt-2 text-[12px] leading-relaxed text-[var(--muted)]">
              The setup call creates the operator, seals the live PIN and initialises all four silos at exactly zero. You
              can verify every one of these values in the ledger before the first deposit.
            </p>
            <ul className="mt-5 flex flex-col gap-2">
              {CLEAN_SLATE.map((item) => (
                <li key={item} className="glass row gap-2 px-3 py-2">
                  <CheckCircle2 size={13} className="text-[var(--pos)]" aria-hidden="true" />
                  <span className="mono text-[11px] text-[var(--ink-soft)]">{item}</span>
                </li>
              ))}
            </ul>
          </div>
          <div className="rounded-[var(--r-md)] border border-[var(--line)] bg-[var(--panel-sunken)] p-3">
            <div className="row">
              <Fingerprint size={15} className="text-[var(--accent)]" aria-hidden="true" />
              <p className="text-[12px] font-medium">Two independent secrets</p>
            </div>
            <p className="mt-1.5 text-[11px] leading-relaxed text-[var(--muted)]">
              The password opens the paper sandbox. The six-digit PIN is only ever checked by the live vault door and is
              never stored in the browser.
            </p>
          </div>
        </div>
      }
      footer={
        <div className="row-between">
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => onPortal("login")}>
            <ArrowLeft size={11} aria-hidden="true" />
            Back to the doors
          </button>
          <StatusPill value="sealed clean slate" tone="accent2" />
        </div>
      }
    >
      <form
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          void submit();
        }}
      >
        <div className="chip-row">
          <StatusPill value="all vaults = 0" tone="accent" />
          <StatusPill value="orders = 0" tone="accent" />
          <StatusPill value="autopilot = off" tone="accent2" />
        </div>

        <label className="field">
          <span className="field-label">Operator username</span>
          <input
            className="input"
            value={username}
            autoComplete="username"
            onChange={(event) => setUsername(event.target.value)}
            aria-label="Operator username"
            required
            minLength={3}
          />
        </label>
        <label className="field">
          <span className="field-label">Password · minimum 8 characters</span>
          <input
            className="input"
            type="password"
            value={password}
            autoComplete="new-password"
            onChange={(event) => setPassword(event.target.value)}
            aria-label="Operator password"
            aria-invalid={password !== "" && !passwordValid}
            required
            minLength={8}
          />
        </label>
        <label className="field">
          <span className="field-label">Six-digit live PIN</span>
          <input
            className="input tnum tracking-[0.5em]"
            inputMode="numeric"
            pattern="\d{6}"
            maxLength={6}
            value={pin}
            autoComplete="off"
            onChange={(event) => setPin(event.target.value.replace(/\D/g, "").slice(0, 6))}
            aria-label="Six digit live PIN"
            aria-invalid={pin !== "" && !pinValid}
            required
          />
        </label>

        {setup.error ? <ErrorBlock message={setup.error} /> : null}

        <button type="submit" className="btn btn-accent mt-1" disabled={!ready}>
          {setup.busy ? "Sealing the slate…" : "Create operator & seal clean slate"}
          <ArrowRight size={12} aria-hidden="true" />
        </button>

        <p className="mono text-[9px] leading-relaxed tracking-[0.06em] text-[var(--dim)] uppercase">
          start_clean_slate=true · 0 real capital · zero broker keys provisioned
        </p>
      </form>
    </PortalScaffold>
  );
}
