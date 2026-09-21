import { FormEvent, useEffect, useState } from "react";
import { api } from "../lib/api";
import { useAction } from "../lib/hooks";
import { Field, Notice } from "../components/ui";

interface Enrollment {
  secret: string;
  otpauth_uri: string;
  qr_svg: string;
  issuer: string;
}

/**
 * Mandatory enrollment, shown in place of the panel until a second factor
 * exists.
 *
 * Nothing about signing in changes until the code is confirmed, so an operator
 * who closes this halfway has not locked themselves out — they simply meet it
 * again next time.
 */
export function TotpSetup({ onComplete }: { onComplete: () => void }) {
  const [enrollment, setEnrollment] = useState<Enrollment | null>(null);
  const [code, setCode] = useState("");
  const [recoveryCodes, setRecoveryCodes] = useState<string[] | null>(null);
  const [saved, setSaved] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        setEnrollment(await api.post<Enrollment>("/api/v1/security/totp/setup/"));
      } catch (err) {
        setLoadError((err as Error).message);
      }
    })();
  }, []);

  const [confirm, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    const result = await api.post<{ recovery_codes: string[] }>(
      "/api/v1/security/totp/confirm/",
      { code },
    );
    setRecoveryCodes(result.recovery_codes);
  });

  if (recoveryCodes) {
    return (
      <div className="login">
        <div className="login__card" style={{ width: "min(480px, 100%)" }}>
          <h1 style={{ fontSize: "1.3rem", marginBottom: 6 }}>Save your recovery codes</h1>
          <p style={{ color: "var(--text-dim)", fontSize: "0.86rem", marginTop: 0 }}>
            Each one signs you in once if you lose your authenticator. They are not
            shown again — only their hashes are stored.
          </p>

          <section className="panel" style={{ marginTop: 16 }}>
            <div className="panel__body">
              <div
                className="mono"
                style={{
                  display: "grid",
                  gridTemplateColumns: "1fr 1fr",
                  gap: "6px 18px",
                  fontSize: "0.85rem",
                }}
              >
                {recoveryCodes.map((recoveryCode) => (
                  <span key={recoveryCode}>{recoveryCode}</span>
                ))}
              </div>

              <div style={{ display: "flex", gap: 8, marginTop: 18 }}>
                <button
                  className="btn"
                  onClick={() => {
                    void navigator.clipboard?.writeText(recoveryCodes.join("\n"));
                  }}
                >
                  Copy
                </button>
                <button
                  className="btn"
                  onClick={() => {
                    const blob = new Blob([recoveryCodes.join("\n") + "\n"], {
                      type: "text/plain",
                    });
                    const url = URL.createObjectURL(blob);
                    const link = document.createElement("a");
                    link.href = url;
                    link.download = "gateway-recovery-codes.txt";
                    link.click();
                    URL.revokeObjectURL(url);
                  }}
                >
                  Download
                </button>
              </div>
            </div>
          </section>

          <label className="check" style={{ marginTop: 16 }}>
            <input
              type="checkbox"
              checked={saved}
              onChange={(e) => setSaved(e.target.checked)}
            />
            <span className="check__text">I have saved these somewhere safe</span>
          </label>

          <button
            className="btn btn--primary"
            style={{ width: "100%", justifyContent: "center" }}
            disabled={!saved}
            onClick={onComplete}
          >
            Continue to the panel
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="login">
      <div className="login__card" style={{ width: "min(460px, 100%)" }}>
        <h1 style={{ fontSize: "1.3rem", marginBottom: 6 }}>Set up your authenticator</h1>
        <p style={{ color: "var(--text-dim)", fontSize: "0.86rem", marginTop: 0 }}>
          This panel controls every domain the gateway serves, so a password on its
          own is not enough to reach it.
        </p>

        {loadError && <Notice tone="error">{loadError}</Notice>}

        {enrollment && (
          <section className="panel" style={{ marginTop: 16 }}>
            <div className="panel__body">
              <p style={{ margin: "0 0 14px", fontSize: "0.86rem" }}>
                Scan this with an authenticator app — 1Password, Aegis, Google
                Authenticator, or any other.
              </p>

              <div
                style={{
                  background: "#fff",
                  padding: 12,
                  borderRadius: "var(--radius)",
                  width: 180,
                  margin: "0 auto 16px",
                }}
                // The QR is server-generated SVG for a URI this app just
                // requested; there is no user-supplied content in it.
                dangerouslySetInnerHTML={{ __html: enrollment.qr_svg }}
              />

              <details style={{ marginBottom: 16 }}>
                <summary
                  style={{
                    fontSize: "0.82rem",
                    color: "var(--text-dim)",
                    cursor: "pointer",
                  }}
                >
                  Can't scan it?
                </summary>
                <p
                  className="mono"
                  style={{
                    fontSize: "0.8rem",
                    wordBreak: "break-all",
                    marginBottom: 0,
                    color: "var(--text-dim)",
                  }}
                >
                  {enrollment.secret}
                </p>
              </details>

              <form onSubmit={confirm}>
                <Field
                  label="Code from the app"
                  help="Six digits. If it is rejected, check the clock on the device generating it."
                  error={error?.message}
                >
                  <input
                    className="input input--mono"
                    value={code}
                    inputMode="numeric"
                    autoComplete="one-time-code"
                    placeholder="123456"
                    onChange={(e) => setCode(e.target.value)}
                    autoFocus
                    required
                  />
                </Field>

                <button
                  className="btn btn--primary"
                  style={{ width: "100%", justifyContent: "center" }}
                  disabled={busy || code.length < 6}
                >
                  {busy ? "Checking…" : "Confirm"}
                </button>
              </form>
            </div>
          </section>
        )}
      </div>
    </div>
  );
}
