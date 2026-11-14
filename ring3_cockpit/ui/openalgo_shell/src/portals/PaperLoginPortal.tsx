/**
 * `/paper/login` — cyan/lavender glass door into the simulated workspace.
 */

import { useState, type ReactNode } from "react";
import { ArrowLeft, ArrowRight, Boxes } from "lucide-react";
import type { Portal } from "@/lib/router";
import { navigate } from "@/lib/router";
import { authApi } from "@/lib/api";
import { useAction } from "@/lib/use-api";
import { useSession } from "@/lib/session";
import { pushToast } from "@/lib/toast";
import type { AuthSession } from "@/lib/types";
import { PortalScaffold } from "./PortalScaffold";
import { ErrorBlock, StatusPill } from "@/design-system/primitives";

export function PaperLoginPortal({ onPortal }: { onPortal: (portal: Portal) => void }): ReactNode {
  const { signIn } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const login = useAction<AuthSession>();

  const submit = async () => {
    const receipt = await login.run(() => authApi.loginPaper({ username: username.trim(), password }));
    if (receipt) {
      signIn(receipt);
      pushToast("Paper sandbox unlocked", "12 hour simulated session", "ok");
      navigate("/paper/dashboard");
    }
  };

  return (
    <PortalScaffold
      mode="paper"
      eyebrow="Paper portal · two fields"
      title="Enter the paper sandbox"
      subtitle="Simulated capital, zero broker reach. This door mints a 12-hour PAPER session and starts on a clean slate."
      aside={
        <div className="flex h-full flex-col justify-between gap-8">
          <div>
            <p className="eyebrow">Sandbox contract</p>
            <h2 className="mt-2 text-[22px] leading-tight font-semibold">
              Research the market with the full kernel, none of the capital.
            </h2>
            <ul className="mt-5 flex flex-col gap-3 text-[12px] text-[var(--muted)]">
              {[
                "Deposit simulated capital into any of the four silos.",
                "Burst shadow fills to walk the 0/30 graduation nursery.",
                "Reset every value to zero at any time — the logbook included.",
                "Switch to the live vault only by re-authenticating at its own door.",
              ].map((item) => (
                <li key={item} className="flex gap-2">
                  <span className="mt-1.5 h-1 w-1 flex-none rounded-full bg-[#22d3ee]" aria-hidden="true" />
                  {item}
                </li>
              ))}
            </ul>
          </div>
          <div className="glass p-4">
            <div className="row">
              <Boxes size={16} className="text-[#22d3ee]" aria-hidden="true" />
              <p className="text-[12px] font-medium">SIMULATED EXECUTION ONLY (ALL VALUES START AT 0)</p>
            </div>
            <p className="mono mt-2 text-[10px] leading-relaxed text-[var(--dim)]">
              Ring 2 is still air-gapped here. The paper simulator shares the deterministic kernel, so every fill, ratchet
              and conformal residual is real telemetry — only the capital is synthetic.
            </p>
          </div>
        </div>
      }
      footer={
        <div className="row-between">
          <button type="button" className="btn btn-sm btn-ghost" onClick={() => onPortal("login")}>
            <ArrowLeft size={11} aria-hidden="true" />
            Both doors
          </button>
          <p className="mono text-[9px] tracking-[0.08em] text-[var(--dim)] uppercase">session TTL 12h · paper tier</p>
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
        <label className="field">
          <span className="field-label">Username</span>
          <input
            className="input"
            value={username}
            autoComplete="username"
            onChange={(event) => setUsername(event.target.value)}
            aria-label="Paper username"
            required
          />
        </label>
        <label className="field">
          <span className="field-label">Password</span>
          <input
            className="input"
            type="password"
            value={password}
            autoComplete="current-password"
            onChange={(event) => setPassword(event.target.value)}
            aria-label="Paper password"
            required
          />
        </label>

        {login.error ? <ErrorBlock message={login.error} /> : null}

        <button type="submit" className="btn btn-accent mt-1" disabled={login.busy || !username || !password}>
          {login.busy ? "Opening sandbox…" : "Enter paper sandbox"}
          <ArrowRight size={12} aria-hidden="true" />
        </button>

        <div className="row-between mt-1">
          <StatusPill value="no live pin required" tone="accent" />
          <button
            type="button"
            className="btn btn-sm btn-ghost"
            onClick={() => onPortal("setup")}
            aria-label="First-run setup"
          >
            First-run setup
          </button>
        </div>
      </form>
    </PortalScaffold>
  );
}
