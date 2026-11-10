/**
 * Your passkeys.
 *
 * Lives here rather than on the Security page because every role needs it and
 * that page is admin-only. A viewer who could not manage their own second
 * factor would be held at enrolment by a screen they are not allowed to open.
 */
import { useEffect, useState } from "react";
import { api } from "../lib/api";
import { useResource } from "../lib/hooks";
import { relativeTime } from "../lib/format";
import { Notice, Outcome, Panel, Row, Tag, useToast } from "./ui";
import * as webauthn from "../lib/webauthn";

interface Passkey {
  id: number;
  label: string;
  is_backed_up: boolean;
  transports: string[];
  created_at: string;
  last_used_at: string | null;
}

export function PasskeysPanel() {
  const { data, reload } = useResource<{ available: boolean; passkeys: Passkey[] }>(
    "/api/v1/security/passkeys/",
  );
  const [busy, setBusy] = useState(false);
  const [options, setOptions] = useState<Record<string, unknown> | null>(null);
  const [platform, setPlatform] = useState<boolean | null>(null);
  const [nonce, setNonce] = useState(0);
  const toast = useToast();

  const browserReason = webauthn.unavailableReason();
  const keys = data?.passkeys ?? [];
  const ready = !!data?.available && !browserReason;

  useEffect(() => {
    if (webauthn.supported()) {
      webauthn.platformAuthenticatorAvailable().then(setPlatform);
    }
  }, []);

  // The challenge is fetched before the button is pressed, not after.
  //
  // navigator.credentials.create() needs the user gesture that started it, and
  // Safari drops that activation across an awaited network request. Asking the
  // server for options inside the click handler therefore fails there with
  // NotAllowedError -- the same error the browser gives for a dismissed
  // prompt, which is why it reads as though nobody touched anything.
  //
  // One effect and one request. The server holds a single challenge per
  // session, so two of these in flight would leave the browser carrying one
  // challenge while the server waited for the other -- a failure that would
  // land on the enrolment after this one, not this one.
  useEffect(() => {
    if (!ready) {
      setOptions(null);
      return;
    }

    let ignore = false;
    api
      .post<Record<string, unknown>>("/api/v1/auth/passkey/register/options/", {})
      .then((fetched) => {
        if (!ignore) setOptions(fetched);
      })
      .catch(() => {
        if (!ignore) setOptions(null);
      });

    return () => {
      ignore = true;
    };
  }, [ready, nonce]);

  async function enrol() {
    if (!options) return;
    setBusy(true);

    try {
      // First await in the handler, so the gesture is still live.
      const credential = await webauthn.create(options);

      // Named afterwards. A modal prompt() beforehand also discards the
      // activation, and by here the ceremony is already done.
      const label =
        window.prompt("What should this device be called?", "This device")?.trim() ||
        "This device";

      await api.post("/api/v1/auth/passkey/register/verify/", { credential, label });
      toast(`${label} can now sign you in.`);
      reload();
    } catch (err) {
      // Keep the exception. Everything below turns it into a sentence an
      // operator can read, and that sentence is deliberately vague about which
      // of several causes applied -- so the raw name and message are the only
      // thing left to diagnose a device that will not enrol.
      console.error("[passkey] enrolment failed", err);
      const apiError = err as { status?: number; message?: string };
      toast(apiError.status ? apiError.message! : webauthn.describe(err), "error");
    } finally {
      // Used or not, that challenge is spent. Get another.
      setOptions(null);
      setNonce((n) => n + 1);
      setBusy(false);
    }
  }

  async function remove(key: Passkey) {
    if (keys.length === 1) {
      const sure = window.confirm(
        `${key.label} is your only passkey. Remove it and you will sign in with your password and authenticator code instead. Continue?`,
      );
      if (!sure) return;
    }
    await api.delete(`/api/v1/security/passkeys/${key.id}/`);
    toast(`${key.label} removed.`);
    reload();
    // The options in hand still exclude the credential just removed, so the
    // device that held it would be refused if it enrolled again.
    setNonce((n) => n + 1);
  }

  return (
    <Panel title="Passkeys" flush>
      <div className="panel__body">
        <Outcome>
          {keys.length === 0
            ? "You sign in with a password and an authenticator code. A passkey replaces both with your device's fingerprint, face or PIN — and cannot be phished, because the signature only works on this panel's own address."
            : `${keys.length === 1 ? "One passkey signs" : `${keys.length} passkeys sign`} you in without a password or a code. Your password still works as a fallback.`}
        </Outcome>

        {!data?.available && !browserReason && (
          <Notice tone="warn">
            Passkeys need the panel published at a hostname of its own. A passkey is
            bound to a domain name and an IP address cannot be one, so enrolment is
            held until you publish it above.
          </Notice>
        )}
        {browserReason && <Notice tone="warn">{browserReason}</Notice>}
      </div>

      {keys.length > 0 && (
        <div className="rows">
          {keys.map((key) => (
            <Row key={key.id} state="healthy" columns="minmax(0,1fr) auto auto">
              <div>
                <div className="row__primary">{key.label}</div>
                <div className="row__secondary">
                  {key.last_used_at
                    ? `last used ${relativeTime(key.last_used_at)}`
                    : "never used"}
                  {` · added ${relativeTime(key.created_at)}`}
                </div>
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                <Tag tone={key.is_backed_up ? "hit" : "signal"}>
                  {key.is_backed_up ? "synced" : "this device only"}
                </Tag>
              </div>
              <div className="row__actions">
                <button className="btn btn--small" onClick={() => remove(key)}>
                  Remove
                </button>
              </div>
            </Row>
          ))}
        </div>
      )}

      <div className="panel__body">
        <button
          className="btn btn--primary"
          disabled={busy || !ready || !options}
          onClick={enrol}
        >
          {busy ? "Waiting for your device…" : "Add a passkey"}
        </button>

        {ready && platform === false && (
          <p className="field__help" style={{ marginTop: 8 }}>
            This device has no built-in fingerprint, face or PIN unlock that the
            panel can use. You can still enrol a security key, or a phone, from the
            prompt — or set up a screen lock here first.
          </p>
        )}
      </div>
    </Panel>
  );
}
