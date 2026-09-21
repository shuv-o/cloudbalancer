import { FormEvent, useRef, useState } from "react";
import { ApiError, api, primeCsrf } from "../lib/api";
import { User } from "../lib/types";
import { Field } from "../components/ui";

type Stage = "credentials" | "code";

export function Login({ onSignedIn }: { onSignedIn: (user: User) => void }) {
  const [stage, setStage] = useState<Stage>("credentials");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [otp, setOtp] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [locked, setLocked] = useState(false);
  const [busy, setBusy] = useState(false);
  const codeRef = useRef<HTMLInputElement>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    try {
      await primeCsrf();
      const user = await api.post<User>("/api/v1/auth/login/", {
        username,
        password,
        otp: stage === "code" ? otp : "",
      });
      onSignedIn({ ...user, authenticated: true });
    } catch (err) {
      const apiError = err as ApiError;
      const body = apiError.body as { totp_required?: boolean } | null;

      if (apiError.status === 401 && body?.totp_required) {
        // The password was right and a code is needed. Not a failure — the form
        // simply has one more thing to ask for.
        const alreadyAsking = stage === "code";
        setStage("code");
        setOtp("");
        setError(alreadyAsking ? apiError.message : null);
        window.setTimeout(() => codeRef.current?.focus(), 50);
      } else {
        setLocked(apiError.status === 429);
        setError(apiError.message);
        if (stage === "code") setOtp("");
      }
    } finally {
      setBusy(false);
    }
  }

  function startOver() {
    setStage("credentials");
    setOtp("");
    setPassword("");
    setError(null);
  }

  return (
    <div className="login">
      <div className="login__card">
        <div className="login__brand">
          <svg width="30" height="30" viewBox="0 0 32 32" aria-hidden="true">
            <rect width="32" height="32" rx="7" fill="var(--ink-700)" />
            <path d="M5 16h6m10 0h6" stroke="var(--signal)" strokeWidth="2.4" strokeLinecap="round" />
            <circle cx="16" cy="16" r="4.2" fill="none" stroke="var(--hit)" strokeWidth="2.4" />
          </svg>
          <div>
            <div style={{ fontSize: "1.15rem", fontWeight: 600, letterSpacing: "-0.02em" }}>
              Gateway Console
            </div>
            <div className="mono" style={{ color: "var(--text-faint)", fontSize: "0.75rem" }}>
              {window.location.host}
            </div>
          </div>
        </div>

        <section className="panel">
          <div className="panel__body">
            <form onSubmit={submit}>
              {stage === "credentials" ? (
                <>
                  <Field label="Username">
                    <input
                      className="input"
                      value={username}
                      autoComplete="username"
                      autoFocus
                      onChange={(e) => setUsername(e.target.value)}
                      required
                    />
                  </Field>
                  <Field label="Password">
                    <input
                      className="input"
                      type="password"
                      value={password}
                      autoComplete="current-password"
                      onChange={(e) => setPassword(e.target.value)}
                      required
                    />
                  </Field>
                </>
              ) : (
                <>
                  <p
                    style={{
                      margin: "0 0 16px",
                      fontSize: "0.85rem",
                      color: "var(--text-dim)",
                    }}
                  >
                    Signing in as <span className="mono">{username}</span>. Enter the
                    current code from your authenticator app, or one of your recovery
                    codes.
                  </p>
                  <Field label="Authenticator code">
                    <input
                      ref={codeRef}
                      className="input input--mono"
                      value={otp}
                      inputMode="text"
                      autoComplete="one-time-code"
                      autoFocus
                      placeholder="123456"
                      onChange={(e) => setOtp(e.target.value)}
                      required
                    />
                  </Field>
                </>
              )}

              {error && (
                <div
                  className={locked ? "notice notice--error" : "notice notice--warn"}
                  style={{ marginBottom: 14 }}
                >
                  {error}
                </div>
              )}

              <button
                className="btn btn--primary"
                style={{ width: "100%", justifyContent: "center" }}
                disabled={
                  busy ||
                  locked ||
                  (stage === "credentials" ? !username || !password : !otp)
                }
              >
                {busy
                  ? "Checking…"
                  : stage === "credentials"
                    ? "Continue"
                    : "Sign in"}
              </button>

              {stage === "code" && (
                <button
                  type="button"
                  className="btn btn--ghost"
                  style={{ width: "100%", justifyContent: "center", marginTop: 8 }}
                  onClick={startOver}
                >
                  Use a different account
                </button>
              )}
            </form>
          </div>
        </section>

        <p
          style={{
            marginTop: 16,
            fontSize: "0.78rem",
            color: "var(--text-faint)",
            lineHeight: 1.6,
          }}
        >
          Attempts are recorded and repeated failures lock the account. If you have
          lost your authenticator, another operator can reset it from the Security
          page.
        </p>
      </div>
    </div>
  );
}
