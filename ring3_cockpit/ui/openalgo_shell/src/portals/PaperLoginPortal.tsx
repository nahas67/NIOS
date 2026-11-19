/**
 * `/paper/login` — two-factor PAPER portal.
 */

import { useState } from "react";
import { Badge, Field, TextInput } from "../components/ui";
import { Icon } from "../components/Icon";
import { authLoginPaper, errorMessage } from "../lib/api";
import { navigateTo } from "../lib/router";
import { useSession } from "../state/session";
import { PortalError, PortalFrame, PortalSubmit } from "./PortalFrame";

export function PaperLoginPortal() {
  const { refresh } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError(null);
    if (username.trim().length === 0 || password.length === 0) {
      setError("Username and password are both required.");
      return;
    }
    setBusy(true);
    try {
      await authLoginPaper(username.trim(), password);
      await refresh();
      navigateTo("/paper/dashboard", { replace: true });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <PortalFrame
      eyebrow="Door 01 · Paper Sandbox"
      title="Enter the paper sandbox"
      subtitle="Simulated execution only. No counterparty, no wire, no statutory tax event. Use it to graduate strategies, not to move money."
      mode="paper"
      footer={
        <div className="flex flex-wrap items-center justify-between gap-2 text-[10px] text-ink-3">
          <a href="/live/login" className="inline-flex items-center gap-1 uppercase tracking-[0.12em] hover:text-ink-2">
            <Icon name="lock" size={11} />
            Live vault instead
          </a>
          <span className="uppercase tracking-[0.12em]">23 gates · 4 silos · 274 instruments</span>
        </div>
      }
    >
      <div className="mb-3 flex items-center gap-2 rounded-[4px] border border-[#3A2A1E] bg-[#1E1712] px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-[0.12em] text-accent">
        <Icon name="play" size={12} />
        PAPER SANDBOX — SIMULATED EXECUTION ONLY
      </div>
      <PortalError message={error} />
      <div className="space-y-3">
        <Field label="Operator Username">
          <TextInput value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="sovereign.operator" />
        </Field>
        <Field label="Password">
          <TextInput type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" placeholder="••••••••" />
        </Field>
        <div className="flex items-center justify-between text-[10px] text-ink-3">
          <span className="uppercase tracking-[0.12em]">2-factor · no PIN on paper</span>
          <Badge tone="accent">SANDBOX</Badge>
        </div>
        <PortalSubmit label={busy ? "Minting session…" : "Enter Sandbox"} onClick={submit} disabled={busy} />
      </div>
    </PortalFrame>
  );
}
