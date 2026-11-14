/**
 * First-run setup portal.
 *
 * Creates the single sovereign operator and initialises every silo, vault,
 * order book, logbook row and performance counter at exactly zero. This screen
 * is the only place a `live_pin` is ever chosen.
 */

import { useState, type ReactNode } from "react";
import { authApi } from "@/lib/api";
import type { Portal } from "@/lib/router";
import { useSession } from "@/lib/session";
import { toast } from "@/lib/toast";
import { Btn, Field } from "@/design-system/primitives";
import { RingTopology } from "@/design-system/ring-topology";

const CLEAN_SLATE_ROWS = [
  ["All Vaults = 0", "ReserveYield - LongTermCompounder - TacticalSwing - SpinalScalper"],
  ["All NAV = 0", "Four silos, sixteen vaults, no opening balance"],
  ["All Orders = 0", "No working order, no fill, no shadow fill"],
  ["All Logbook = 0", "No trade row, no R-multiple, no cost drag"],
  ["Autopilot = OFF", "Nothing runs until an operator arms it"],
];

export function SetupPortal({ onPortal }: { onPortal: (portal: Portal) => void }): ReactNode {
  const { signIn } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [livePin, setLivePin] = useState("");
  const [ack, setAck] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const submit = async () => {
    setError(null);
    if (username.trim().length < 3) return setError("Username must be at least 3 characters.");
    if (password.length < 8) return setError("Password must be at least 8 characters.");
    if (password !== confirm) return setError("Password confirmation does not match.");
    if (!/^\d{6}$/.test(livePin)) return setError("The live PIN must be exactly six digits.");
    if (!ack) return setError("Acknowledge that live mode can sign real orders.");
    setBusy(true);
    try {
      const session = await authApi.setup({
        username: username.trim(),
        password,
        live_pin: livePin,
      });
      signIn(session);
      toast("Sovereign operator created. Every value starts at zero.", "success");
      onPortal("login");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "setup rejected");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="nx-portal">
      <header className="nx-portal-top">
        <span className="nx-sidebar-brand-mark" aria-hidden="true">
          N
        </span>
        <div className="nx-sidebar-brand-text">
          <span className="nx-sidebar-brand-title">NIOS</span>
          <span className="nx-sidebar-brand-sub">Sovereign Operator Setup</span>
        </div>
        <span className="nx-badge nx-badge--muted" style={{ marginLeft: "auto" }}>
          FIRST BOOT
        </span>
      </header>

      <div className="nx-portal-body">
        <div className="nx-portal-card nx-portal-card--wide">
          <div className="nx-portal-title">
            <span className="nx-kicker">BOOTSTRAP</span>
            <h1>Create the sovereign operator</h1>
            <p>
              One human identity owns the cockpit. Two credentials open the paper sandbox; the six-digit
              live PIN is the third factor that unlocks the funded vault.
            </p>
          </div>

          <div className="nx-grid nx-grid--1-2">
            <div className="nx-stack">
              <RingTopology active="ring3" />
              <div className="nx-panel">
                <div className="nx-panel-head">
                  <div className="nx-panel-title">
                    <strong>CLEAN_SLATE initialisation</strong>
                    <span>Nothing is seeded, funded or pre-traded</span>
                  </div>
                </div>
                <div className="nx-panel-body nx-stack">
                  {CLEAN_SLATE_ROWS.map(([title, body]) => (
                    <div className="nx-list-item" key={title}>
                      <span className="nx-badge nx-badge--muted">0</span>
                      <span className="nx-stack" style={{ gap: 0 }}>
                        <span className="nx-strong nx-sm">{title}</span>
                        <span className="nx-xs nx-muted">{body}</span>
                      </span>
                    </div>
                  ))}
                </div>
              </div>
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

              <Field label="Operator username">
                <input
                  className="nx-input"
                  autoComplete="username"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  placeholder="sovereign"
                />
              </Field>
              <Field label="Password" hint="Minimum 8 characters. Stored as an Argon2 digest.">
                <input
                  className="nx-input"
                  type="password"
                  autoComplete="new-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                />
              </Field>
              <Field label="Confirm password">
                <input
                  className="nx-input"
                  type="password"
                  autoComplete="new-password"
                  value={confirm}
                  onChange={(event) => setConfirm(event.target.value)}
                />
              </Field>
              <Field label="Six-digit live PIN" hint="Third factor for /api/v2/auth/login/live.">
                <input
                  className="nx-input"
                  inputMode="numeric"
                  maxLength={6}
                  value={livePin}
                  onChange={(event) => setLivePin(event.target.value.replace(/\D/g, ""))}
                  placeholder="000000"
                />
              </Field>

              <label className="nx-check">
                <input type="checkbox" checked={ack} onChange={(event) => setAck(event.target.checked)} />
                <span>
                  I understand that the live vault can sign real orders with real capital, and that live
                  mode only unlocks after the 22-gate pre-flight certificate passes.
                </span>
              </label>

              <Btn tone="primary" block onClick={() => void submit()} disabled={busy}>
                {busy ? "Initialising..." : "Initialise sovereign workspace"}
              </Btn>
              <button type="button" className="nx-portal-note" onClick={() => onPortal("login")}>
                An operator already exists? Go to the gateway.
              </button>
            </form>
          </div>
        </div>
      </div>
    </div>
  );
}
