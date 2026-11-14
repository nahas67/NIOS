/**
 * Paper login - two factors, no capital.
 */

import { useState, type ReactNode } from "react";
import { authApi } from "@/lib/api";
import { navigate, type Portal } from "@/lib/router";
import { useSession } from "@/lib/session";
import { toast } from "@/lib/toast";
import { Btn, Field, StatusBadge } from "@/design-system/primitives";
import { Glyph } from "@/shells/icons";

export function PaperLoginPortal({ onPortal }: { onPortal: (portal: Portal) => void }): ReactNode {
  const { signIn } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    if (!username.trim() || !password) return setError("Username and password are required.");
    setBusy(true);
    try {
      const session = await authApi.loginPaper({ username: username.trim(), password });
      signIn(session);
      toast("Paper sandbox unlocked", "success");
      navigate("/paper/dashboard", { replace: true });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "login rejected");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-portal">
      <header className="nx-portal-top">
        <span className="nx-sidebar-brand-mark" aria-hidden="true">
          <Glyph name="coin" size={13} />
        </span>
        <div className="nx-sidebar-brand-text">
          <span className="nx-sidebar-brand-title">NIOS</span>
          <span className="nx-sidebar-brand-sub">Paper Sandbox Access</span>
        </div>
        <StatusBadge tone="neutral" dot>
          SIMULATED EXECUTION ONLY (ALL VALUES START AT 0)
        </StatusBadge>
      </header>

      <div className="nx-portal-body">
        <div className="nx-portal-card">
          <div className="nx-portal-title">
            <span className="nx-kicker">TWO-FACTOR DOOR</span>
            <h1>Enter the paper sandbox</h1>
            <p>
              Username and password are enough here. No live PIN is collected on this surface, and the
              gateway issues a PAPER-tier session only.
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
            <Field label="Username">
              <input
                className="nx-input"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
              />
            </Field>
            <Field label="Password">
              <input
                className="nx-input"
                type="password"
                autoComplete="current-password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </Field>
            <Btn tone="primary" block onClick={() => void submit()} disabled={busy}>
              {busy ? "Authenticating..." : "Unlock paper sandbox"}
            </Btn>
          </form>

          <div className="nx-hr" />
          <div className="nx-row nx-row--between">
            <button type="button" className="nx-portal-note" onClick={() => onPortal("login")}>
              Back to the dual gateway
            </button>
            <button type="button" className="nx-portal-note" onClick={() => onPortal("live-login")}>
              I need the live vault
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
