/**
 * `/live-unlock` â€” elevate an existing PAPER session to LIVE with the PIN.
 */

import { useState } from "react";
import { Badge, Checkbox, Field, TextInput } from "../components/ui";
import { Icon } from "../components/Icon";
import { authElevateLive, errorMessage } from "../lib/api";
import { duration } from "../lib/format";
import { navigateTo } from "../lib/router";
import { useSession } from "../state/session";
import { PortalError, PortalFrame, PortalSubmit } from "./PortalFrame";

export function LiveUnlockModal() {
  const { session, refresh } = useSession();
  const [pin, setPin] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError(null);
    if (!/^[0-9]{4,12}$/.test(pin)) {
      setError("The live PIN must be 4 to 12 digits.");
      return;
    }
    if (!acknowledged) {
      setError("Explicit real-capital acknowledgement is required to elevate.");
      return;
    }
    setBusy(true);
    try {
      await authElevateLive(pin);
      await refresh();
      navigateTo("/live/dashboard", { replace: true });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <PortalFrame
      eyebrow="Elevation Â· Live Sovereignty"
      title="Raise this session to LIVE"
      subtitle="You already hold a PAPER session. Elevation does not re-key the operator; it adds the live PIN factor and re-verifies the 23-gate certificate against the live venue set."
      mode="live"
      width={440}
      footer={
        <div className="flex items-center justify-between text-[10px] text-ink-3">
          <a href="/paper/dashboard" className="uppercase tracking-[0.12em] hover:text-ink-2">
            Stay in paper sandbox
          </a>
          <Badge tone={session?.authenticated ? "accent" : "muted"}>{session?.authenticated ? "SESSION PRESENT" : "NO SESSION"}</Badge>
        </div>
      }
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 rounded-[4px] border border-hairline bg-canvas px-2.5 py-1.5">
        <div className="flex items-center gap-1.5 text-[9.5px] uppercase tracking-[0.12em] text-ink-3">
          <Icon name="clock" size={11} />
          PAPER TTL
          <span className="tabular-nums text-ink-2">{duration(session?.ttlRemainingS ?? 0)}</span>
        </div>
        <div className="text-[9.5px] uppercase tracking-[0.12em] text-ink-3">
          ELEVATES TO <span className="font-semibold text-bear">LIVE</span>
        </div>
      </div>
      <PortalError message={error} />
      <div className="space-y-3">
        <Field label="6-Digit Live PIN" hint="PBKDF2 digest in Ring 0. Three failed attempts void the session.">
          <TextInput
            value={pin}
            onChange={(event) => setPin(event.target.value.replace(/[^0-9]/g, ""))}
            inputMode="numeric"
            maxLength={12}
            mono
            autoFocus
            placeholder="000000"
          />
        </Field>
        <Checkbox checked={acknowledged} onChange={setAcknowledged} label="I understand that elevation arms real-capital order signing for this session." />
        <PortalSubmit label={busy ? "Elevatingâ€¦" : "Elevate to Live"} onClick={submit} disabled={busy} />
      </div>
    </PortalFrame>
  );
}
