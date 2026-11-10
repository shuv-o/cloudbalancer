/**
 * Your own account: password, passkeys, authenticator.
 *
 * Separate from the Security page, which is admin-only. Everything here acts
 * on the person signed in and on nothing else, so every role can reach it —
 * and must be able to, because the panel holds every account at second-factor
 * enrolment before it will show anything.
 */
import { FormEvent, useState } from "react";
import { ApiError, api } from "../lib/api";
import { User } from "../lib/types";
import { Field, Notice, Outcome, Panel, Tag, useToast } from "../components/ui";
import { PasskeysPanel } from "../components/PasskeysPanel";

const ROLE_MEANING: Record<string, string> = {
  viewer:
    "You can see every domain, route, backend and chart. Changing them, and the Security page, are not available to you.",
  operator:
    "You can add and change domains, backends, routes and certificates, and deploy the gateway. Changing who may reach the panel is not available to you.",
  admin: "You can change anything in the panel, including who else may reach it.",
};

function PasswordPanel({ forced }: { forced?: boolean }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();

  const mismatch = confirm.length > 0 && next !== confirm;

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (mismatch) return;

    setBusy(true);
    setError(null);
    try {
      await api.post("/api/v1/security/password/", {
        current_password: current,
        new_password: next,
      });
      setCurrent("");
      setNext("");
      setConfirm("");
      toast("Password changed. Any other session you had is now signed out.");
      if (forced) window.location.reload();
    } catch (err) {
      setError((err as ApiError).message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel
      title="Password"
      hint="Changing it signs out every other session you have"
    >
      <form onSubmit={submit}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error}
          </div>
        )}

        <Field label="Current password">
          <input
            className="input"
            type="password"
            value={current}
            autoComplete="current-password"
            onChange={(e) => setCurrent(e.target.value)}
            required
          />
        </Field>

        <Field label="New password">
          <input
            className="input"
            type="password"
            value={next}
            autoComplete="new-password"
            onChange={(e) => setNext(e.target.value)}
            required
          />
        </Field>

        <Field
          label="New password again"
          error={mismatch ? "These two do not match." : undefined}
        >
          <input
            className="input"
            type="password"
            value={confirm}
            autoComplete="new-password"
            onChange={(e) => setConfirm(e.target.value)}
            required
          />
        </Field>

        <button
          className="btn btn--primary"
          disabled={busy || mismatch || !current || !next}
        >
          {busy ? "Changing…" : "Change password"}
        </button>
      </form>
    </Panel>
  );
}

/**
 * The whole panel, replaced by this one screen, until the password changes.
 *
 * An account created by an admin signs in with a password that admin chose
 * and still knows. Until the owner replaces it, nothing either of them does
 * can be told apart in the audit trail — so the server refuses everything
 * else, and this is the screen that says why.
 */
export function ForcePasswordChange() {
  return (
    <div className="login">
      <div className="login__card">
        <h1 style={{ fontSize: "var(--step-2)", marginBottom: 6 }}>
          Choose your own password
        </h1>
        <p
          style={{
            margin: "0 0 18px",
            color: "var(--text-dim)",
            fontSize: "0.88rem",
            lineHeight: 1.6,
          }}
        >
          You signed in with a password set by whoever created this account.
          They still know it. Until you replace it, nothing you do here can be
          told apart from something they did.
        </p>
        <PasswordPanel forced />
      </div>
    </div>
  );
}

export function Account({ user }: { user: User }) {
  const role = user.role ?? null;

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Your account</h1>
          <p>
            Signed in as <span className="mono">{user.username}</span>.
          </p>
        </div>
        {role && <Tag tone="hit">{role}</Tag>}
      </header>

      <div className="grid grid--2" style={{ marginBottom: 16 }}>
        <Panel title="What you can do" hint="Set by an admin">
          <Outcome>
            {role
              ? ROLE_MEANING[role] ?? "Your role does not match any this panel knows."
              : "This account holds no role, so the panel will refuse everything. An admin needs to give it one."}
          </Outcome>
          {!role && (
            <Notice tone="warn">
              Accounts created outside the panel start this way. Ask an admin to
              set a role on the Security page.
            </Notice>
          )}
        </Panel>

        <PasswordPanel />
      </div>

      <div className="grid grid--2" style={{ marginBottom: 16 }}>
        <PasskeysPanel />
      </div>
    </>
  );
}
