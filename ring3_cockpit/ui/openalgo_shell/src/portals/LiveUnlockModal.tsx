/**
 * `/live-unlock` — elevate an existing operator session to the live tier.
 *
 * The paper token is never promoted in place by the backend; this screen asks
 * for the PIN again and swaps the session for a fresh 30-minute live lease.
 */

import { useState, type ReactNode } from "react";
import { ArrowRight, KeyRound, LockKeyhole } from "lucide-react";
import type { Portal } from "@/lib/router";
import { navigate } from "@/lib/router";
import { authApi } from "@/lib/api";
import { useAction } from "@/lib/use-api";
import { useSession } from "@/lib/session";
import { pushToast } from "@/lib/toast";
import type { AuthSession } from "@/lib/types";
import { PortalScaffold } from "./PortalScaffold";
import { ErrorBlock, StatusPill } from "@/design-system/primitives";

export function LiveUnlockModal({ onPortal }: { onPortal: (portal: Portal) => void }): ReactNode {
  const { signIn, session, username } = useSession();
  const [pin, setPin] = useState("");
  const elevate = useAction<AuthSession>();
  const pinValid = /^\d{6}$/.test(pin);

  const submit = async () => {
    if (!pinValid) return;
    const receipt = await elevate.run(() => authApi.elevate(pin));
    if (receipt) {
      signIn(receipt);
      pushToast("Live tier unlocked", "30 minute lease · PIN verified", "warn");
      navigate("/live/dashboard");
    }
  };

  return (
    <PortalScaffold
      mode="live"
      eyebrow="Vault elevation · step-up"
      title="Unlock the live tier"
      subtitle="Your operator session is valid, but live execution needs a second factor. Enter the six-digit live PIN to mint a 30-minute live lease."
      aside={
        <div className="flex h-full flex-col justify-between gap-8">
          <div>
            <p className="eyebrow">Step-up authentication</p>
            <h2 className="mt-2 text-[22px] leading-tight font-semibold">The PIN is the last human gate before Ring 0.</h2>
            <p className="mt-2 text-[12px] leading-relaxed text-[var(--muted)]">
              Ring 2 can write a proposal without a PIN. Ring 0 will not stage a transfer, sign an order or release a lease
              until this vault door is open.
            </p>
            <div className="mt-5 flex flex-col gap-2">
              {[
                "Session tier before unlock: PAPER or NONE",
                "Session tier after unlock: LIVE, 30 minutes",
                "Kill switch remains available at every tier",
                "Wrong PIN returns 403 LIVE_PIN_REJECTED",
              ].map((item) => (
                <div key={item} className="row gap-2 rounded-[var(--r-sm)] border border-[var(--line)] px-3 py-2">
                  <LockKeyhole size={12} className="text-[#f43f5e]" aria-hidden="true" />
                  <span className="mono text-[10.5px] text-[var(--ink-soft)]">{item}</span>
                </div>
              ))}
            </div>
          </div>
          <div className="chip-row">
            <StatusPill value={session?.tier ?? "NONE"} tone="neutral" />
            <StatusPill value={username ?? "operator"} tone="info" />
            <StatusPill value="step-up required" tone="warn" />
          </div>
        </div>
      }
      footer={
        <div className="row-between">
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => onPortal("login")}>
            Back to the doors
          </button>
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => onPortal("paper-login")}>
            Stay in paper
          </button>
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
        <div className="row gap-2 rounded-[var(--r-sm)] border border-[rgba(245,158,11,0.3)] bg-[rgba(245,158,11,0.08)] px-3 py-2">
          <KeyRound size={13} className="text-[#f59e0b]" aria-hidden="true" />
          <p className="mono text-[9.5px] tracking-[0.08em] text-[#fcd34d] uppercase">second factor · live pin</p>
        </div>

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

        {elevate.error ? <ErrorBlock message={elevate.error} /> : null}

        <button type="submit" className="btn btn-danger mt-1" disabled={!pinValid || elevate.busy}>
          {elevate.busy ? "Unlocking…" : "Unlock live tier"}
          <ArrowRight size={12} aria-hidden="true" />
        </button>

        <p className="mono text-[9px] leading-relaxed tracking-[0.06em] text-[var(--dim)] uppercase">
          POST /api/v2/auth/elevate-live · 30 minute lease · no capital moved by this call
        </p>
      </form>
    </PortalScaffold>
  );
}
