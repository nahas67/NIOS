/**
 * `/setup` â€” first-run sovereign operator provisioning.
 */

import { useState } from "react";
import { Badge, Checkbox, Field, TextInput } from "../components/ui";
import { Icon } from "../components/Icon";
import { authSetup, errorMessage } from "../lib/api";
import { navigateTo } from "../lib/router";
import { useSession } from "../state/session";
import { PortalError, PortalFrame, PortalSubmit } from "./PortalFrame";

export function SetupPortal() {
  const { refresh } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pin, setPin] = useState("");
  const [cleanSlate, setCleanSlate] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setError(null);
    if (username.trim().length === 0) {
      setError("Username is required.");
      return;
    }
    if (password.length < 8) {
      setError("Password must be at least 8 characters (Ring 0 scrypt policy).");
      return;
    }
    if (!/^[0-9]{4,12}$/.test(pin)) {
      setError("Live PIN must be 4 to 12 digits.");
      return;
    }
    setBusy(true);
    try {
      await authSetup({ username: username.trim(), password, livePin: pin, startCleanSlate: cleanSlate });
      await refresh();
      navigateTo("/paper/login", { replace: true });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <PortalFrame
      eyebrow="First Run Â· Ring 0 Key Vault"
      title="Provision the sovereign operator"
      subtitle="One operator owns this install. Credentials are sealed with scrypt and a PBKDF2 live PIN; the vault never returns them in plaintext."
      mode="neutral"
      footer={
        <div className="flex items-center justify-between text-[10px] text-ink-3">
          <a href="/login" className="uppercase tracking-[0.14em] hover:text-ink-2">
            Already provisioned? Gateway
          </a>
          <Badge tone="muted">clean slate default</Badge>
        </div>
      }
    >
      <PortalError message={error} />
      <div className="space-y-3">
        <Field label="Operator Username" hint="3 to 64 characters, letters, digits, dot, dash, underscore.">
          <TextInput value={username} onChange={(event) => setUsername(event.target.value)} autoComplete="username" placeholder="sovereign.operator" />
        </Field>
        <Field label="Password" hint="Minimum 8 characters. Ring 0 stores only the scrypt digest.">
          <TextInput type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="new-password" placeholder="â€¢â€¢â€¢â€¢â€¢â€¢â€¢â€¢" />
        </Field>
        <Field label="6-Digit Live PIN" hint="Second factor for the LIVE vault. Digits only, 4 to 12.">
          <TextInput value={pin} onChange={(event) => setPin(event.target.value.replace(/[^0-9]/g, ""))} inputMode="numeric" maxLength={12} mono placeholder="000000" />
        </Field>
        <div className="rounded-[4px] border border-hairline bg-canvas p-2.5">
          <Checkbox
            checked={cleanSlate}
            onChange={setCleanSlate}
            label={
              <span>
                Start from a <span className="font-semibold text-ink-2">clean slate</span>: all four silos open at exactly{" "}
                <span className="font-semibold text-ink-2">0 units</span>. No simulated or real balance is imported.
              </span>
            }
          />
          <div className="mt-2 flex items-center gap-1.5 text-[9.5px] uppercase tracking-[0.1em] text-ink-3">
            <Icon name="info" size={11} />
            IBOR opens at 0 Â· no tax lots Â· no phantom fills
          </div>
        </div>
        <PortalSubmit label={busy ? "Provisioningâ€¦" : "Provision Operator"} onClick={submit} disabled={busy} />
      </div>
    </PortalFrame>
  );
}
