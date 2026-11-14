/**
 * Step-up live PIN.
 *
 * A paper session that needs one privileged action re-enters the third factor
 * without discarding the password session. On success the gateway re-mints the
 * token at LIVE tier and the cockpit routes to the live dashboard.
 */

import { useState, type ReactNode } from "react";
import { authApi } from "@/lib/api";
import { navigate, type Portal } from "@/lib/router";
import { useSession } from "@/lib/session";
import { toast } from "@/lib/toast";
import { Btn, Field, StatusBadge } from "@/design-system/primitives";
import { Glyph } from "@/shells/icons";

export function LiveUnlockModal({ onPortal }: { onPortal: (portal: Portal) => void }): ReactNode {
  const { signIn, username, tier } = useSession();
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    if (!/^\d{6}$/.test(pin)) return setError("The live PIN must be exactly six digits.");
    setBusy(true);
    try {
      const session = await authApi.elevate(pin);
      signIn(session);
      toast("Live tier elevated for this lease", "success");
      navigate("/live/dashboard", { replace: true });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "elevation rejected");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-portal">
      <header className="nx-portal-top">
        <span className="nx-sidebar-brand-mark" aria-hidden="true" style={{ borderColor: "#472527", color: "#E05555" }}>
          <Glyph name="shield" size={13} />
        </span>
        <div className="nx-sidebar-brand-text">
          <span className="nx-sidebar-brand-title">NIOS</span>
          <span className="nx-sidebar-brand-sub">Privilege Elevation</span>
        </div>
        <span style={{ marginLeft: "auto" }}>
          <StatusBadge tone="accent">STEP-UP REQUIRED</StatusBadge>
        </span>
      </header>

      <div className="nx-portal-body">
        <div className="nx-portal-card" style={{ maxWidth: 440 }}>
          <div className="nx-portal-title">
            <span className="nx-kicker">THIRD FACTOR</span>
            <h1>Elevate to the live vault</h1>
            <p>
              Signed in as <span className="nx-strong">{username ?? "unbound operator"}</span> at{" "}
              <span className="nx-strong">{tier}</span> tier. A six-digit live PIN upgrades this lease to
              LIVE for 30 minutes.
            </p>
          </div>

          <form
            className="nx-portal-form"
            onSubmit={(event) => {
              event.preventDefault();
              void submit();
            }}
          >
            {error ? (
              <div className="nx-portal-err" role="alert">
                {error}
              </div>
            ) : null}
            <Field label="Six-digit live PIN">
              <input
                className="nx-input"
                type="password"
                inputMode="numeric"
                maxLength={6}
                autoComplete="off"
                value={pin}
                onChange={(event) => setPin(event.target.value.replace(/\D/g, ""))}
                placeholder="000000"
              />
            </Field>
            <Btn tone="danger" block onClick={() => void submit()} disabled={busy}>
              {busy ? "Elevating..." : "Elevate lease to LIVE"}
            </Btn>
          </form>

          <div className="nx-hr" />
          <div className="nx-row nx-row--between">
            <button type="button" className="nx-portal-note" onClick={() => onPortal("paper-login")}>
              Stay in the paper sandbox
            </button>
            <span className="nx-xs nx-muted">POST /api/v2/auth/elevate-live</span>
          </div>
        </div>
      </div>
    </div>
  );
}
