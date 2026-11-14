/**
 * Live login - three factors and an explicit real-capital acknowledgement.
 *
 * The door reads the 22-gate pre-flight certificate before it renders, so an
 * operator can never mistake a degraded vault for a ready one.
 */

import { useState, type ReactNode } from "react";
import { authApi } from "@/lib/api";
import { navigate, type Portal } from "@/lib/router";
import { useSession } from "@/lib/session";
import { toast } from "@/lib/toast";
import { useApi } from "@/lib/use-api";
import { num } from "@/lib/format";
import { Btn, Field, StatusBadge } from "@/design-system/primitives";
import { Glyph } from "@/shells/icons";

const FACTORS = [
  ["1", "Username", "Sovereign operator identity"],
  ["2", "Password", "Argon2 digest, same credential as paper"],
  ["3", "Six-digit live PIN", "Separate secret, never sent to the paper door"],
];

export function LiveLoginPortal({ onPortal }: { onPortal: (portal: Portal) => void }): ReactNode {
  const { signIn } = useSession();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pin, setPin] = useState("");
  const [acknowledged, setAcknowledged] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const preflight = useApi(() => authApi.preflight(), []);

  const passed = num(preflight.data, "passed_gates", num(preflight.data, "certificate.passed_gates", 0));
  const total = num(preflight.data, "total_gates", num(preflight.data, "certificate.total_gates", 22));
  const ready = total > 0 && passed >= total;

  const submit = async () => {
    setError(null);
    if (!username.trim() || !password) return setError("Username and password are required.");
    if (!/^\d{6}$/.test(pin)) return setError("The live PIN must be exactly six digits.");
    if (!acknowledged) return setError("Acknowledge that live mode signs orders with real capital.");
    setBusy(true);
    try {
      const session = await authApi.loginLive({
        username: username.trim(),
        password,
        live_pin: pin,
      });
      signIn(session);
      toast("Live sovereign vault unlocked. Lease is 30 minutes.", "success");
      navigate("/live/dashboard", { replace: true });
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "live login rejected");
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
          <span className="nx-sidebar-brand-sub">Live Sovereign Vault Access</span>
        </div>
        <span style={{ marginLeft: "auto" }}>
          <StatusBadge tone="bear" large dot>
            🔒 LIVE PRODUCTION — REAL CAPITAL
          </StatusBadge>
        </span>
      </header>

      <div className="nx-portal-body">
        <div className="nx-portal-card" style={{ maxWidth: 520 }}>
          <div className="nx-portal-title">
            <span className="nx-kicker">THREE-FACTOR DOOR</span>
            <h1>Unlock the live vault</h1>
            <p>
              This surface signs real orders. Ring 0 verifies the SHA-256 digest, the instrument whitelist,
              the notional cap, the 98% conformal gate and the tail-copula shield on every proposal.
            </p>
          </div>

          <div className="nx-panel" style={{ borderColor: ready ? "#234235" : "#472527" }}>
            <div className="nx-panel-head">
              <div className="nx-panel-title">
                <strong>22-gate pre-flight</strong>
                <span>
                  {ready ? "Certificate complete - vault may be unlocked" : "Certificate incomplete - the vault will refuse live orders"}
                </span>
              </div>
              <div className="nx-panel-actions">
                <StatusBadge tone={ready ? "bull" : "bear"}>
                  {passed}/{total} PASS
                </StatusBadge>
              </div>
            </div>
            <div className="nx-panel-body">
              <div className="nx-meter">
                <div
                  className={`nx-meter-fill${ready ? " nx-meter-fill--bull" : " nx-meter-fill--bear"}`}
                  style={{ width: `${total > 0 ? Math.min(100, (passed / total) * 100) : 0}%` }}
                />
              </div>
              <div className="nx-row nx-row--between" style={{ marginTop: 6 }}>
                <span className="nx-xs nx-muted">
                  {preflight.error ? `preflight unavailable: ${preflight.error}` : "GET /api/v2/control/preflight"}
                </span>
                <Btn size="sm" tone="ghost" onClick={preflight.reload}>
                  Re-read
                </Btn>
              </div>
            </div>
          </div>

          <div className="nx-stack" style={{ gap: 6 }}>
            {FACTORS.map(([index, label, detail]) => (
              <div className="nx-list-item" key={label}>
                <span className="nx-badge nx-badge--muted">{index}</span>
                <span className="nx-stack" style={{ gap: 0 }}>
                  <span className="nx-sm nx-strong">{label}</span>
                  <span className="nx-xs nx-muted">{detail}</span>
                </span>
              </div>
            ))}
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
            <Field label="Six-digit live PIN" hint="The PIN is rate-limited and never persisted in the browser.">
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

            <label className="nx-check">
              <input
                type="checkbox"
                checked={acknowledged}
                onChange={(event) => setAcknowledged(event.target.checked)}
              />
              <span>
                I acknowledge that this vault holds REAL CAPITAL, that live orders are irreversible once
                signed, and that the 22-gate certificate above is my pre-flight evidence.
              </span>
            </label>

            <Btn tone="danger" block onClick={() => void submit()} disabled={busy}>
              {busy ? "Verifying three factors..." : "Unlock live sovereign vault"}
            </Btn>
          </form>

          <div className="nx-hr" />
          <div className="nx-row nx-row--between">
            <button type="button" className="nx-portal-note" onClick={() => onPortal("paper-login")}>
              Use the paper sandbox instead
            </button>
            <button type="button" className="nx-portal-note" onClick={() => onPortal("login")}>
              Back to the dual gateway
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
