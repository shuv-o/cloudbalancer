/**
 * Your passkeys.
 *
 * Lives here rather than on the Security page because every role needs it and
 * that page is admin-only. A viewer who could not manage their own second
 * factor would be held at enrolment by a screen they are not allowed to open.
 */
import { FormEvent, useEffect, useState } from "react";
import { ApiError, api } from "../lib/api";
import { useResource } from "../lib/hooks";
import { relativeTime } from "../lib/format";
import { usePasskeyOptions } from "../lib/usePasskeyOptions";
import { Notice, Outcome, Panel, Row, Tag, useToast } from "./ui";
import * as webauthn from "../lib/webauthn";

interface Passkey {
  id: number;
  label: string;
  is_backed_up: boolean;
  transports: string[];
  created_at: string;
  last_used_at: string | null;
  rp_id: string;
  usable: boolean;
}

interface PasskeyList {
  available: boolean;
  rp_id: string | null;
  passkeys: Passkey[];
}

/** A starting name for the device, so most people only have to press the button. */
function guessDeviceName(): string {
  const ua = navigator.userAgent;
  if (/iPhone/.test(ua)) return "iPhone";
  if (/iPad/.test(ua)) return "iPad";
  if (/Android/.test(ua)) return "Android phone";
  if (/Mac OS X/.test(ua)) return "Mac";
  if (/Windows/.test(ua)) return "Windows PC";
  if (/Linux/.test(ua)) return "Linux PC";
  return "This device";
}

export function PasskeysPanel() {
  const { data, error: listError, reload } = useResource<PasskeyList>(
    "/api/v1/security/passkeys/",
  );
  const [busy, setBusy] = useState(false);
  const [label, setLabel] = useState(guessDeviceName);
  const [platform, setPlatform] = useState<boolean | null>(null);
  const toast = useToast();

  const browserReason = webauthn.unavailableReason();
  const keys = data?.passkeys ?? [];
  const usable = keys.filter((k) => k.usable);
  const stranded = keys.filter((k) => !k.usable);
  const ready = !!data?.available && !browserReason;

  const ceremony = usePasskeyOptions("/api/v1/auth/passkey/register/options/", ready);

  useEffect(() => {
    if (webauthn.supported()) {
      webauthn.platformAuthenticatorAvailable().then(setPlatform);
    }
  }, []);

  async function enrol(event: FormEvent) {
    event.preventDefault();
    const options = ceremony.take();
    if (!options) return;
    setBusy(true);

    const name = label.trim().slice(0, 64) || "This device";
    try {
      // First await in the handler, so the gesture is still live. The name
      // was typed beforehand for the same reason: a modal prompt() here, or
      // any await before this line, discards the activation the browser
      // needs.
      const credential = await webauthn.create(options);
      await api.post("/api/v1/auth/passkey/register/verify/", { credential, label: name });
      toast(`${name} can now sign you in.`);
      setLabel(guessDeviceName());
      reload();
    } catch (err) {
      // Keep the exception. Everything below turns it into a sentence an
      // operator can read, and that sentence is deliberately vague about which
      // of several causes applied -- so the raw name and message are the only
      // thing left to diagnose a device that will not enrol.
      console.error("[passkey] enrolment failed", err);
      toast(err instanceof ApiError ? err.message : webauthn.describe(err), "error");
    } finally {
      // Used or not, that challenge is spent. Get another.
      ceremony.refresh();
      setBusy(false);
    }
  }

  async function remove(key: Passkey) {
    const lastUsable = key.usable && usable.length === 1;
    if (lastUsable) {
      const sure = window.confirm(
        `${key.label} is your only working passkey. If the panel requires a second ` +
          "factor and you have no authenticator app, you will be asked to set one up " +
          "the next time you sign in. Continue?",
      );
      if (!sure) return;
    }
    try {
      await api.delete(`/api/v1/security/passkeys/${key.id}/`);
      toast(`${key.label} removed.`);
    } catch (err) {
      toast(err instanceof ApiError ? err.message : "Could not remove that passkey.", "error");
    }
    reload();
    // The options in hand still exclude the credential just removed, so the
    // device that held it would be refused if it enrolled again.
    ceremony.refresh();
  }

  return (
    <Panel title="Passkeys" flush>
      <div className="panel__body">
        <Outcome>
          {usable.length === 0
            ? "A passkey signs you in with your device's fingerprint, face or PIN instead of a password and a code — and cannot be phished, because the signature only works on this panel's own address."
            : `${usable.length === 1 ? "One passkey signs" : `${usable.length} passkeys sign`} you in with your device's fingerprint, face or PIN. If you lose every device that holds one, an admin can reset your second factor.`}
        </Outcome>

        {listError && <Notice tone="error">Could not load your passkeys: {listError.message}</Notice>}

        {data && !data.available && !browserReason && (
          <Notice tone="warn">
            Passkeys need the panel published at a hostname of its own. A passkey is
            bound to a domain name and an IP address cannot be one, so enrolment is
            held until an admin publishes it on the Security page.
          </Notice>
        )}
        {browserReason && <Notice tone="warn">{browserReason}</Notice>}

        {stranded.length > 0 && data?.rp_id && (
          <Notice tone="warn">
            {stranded.length === 1 ? "One passkey was" : `${stranded.length} passkeys were`}{" "}
            made for a different address than{" "}
            <span className="mono">{data.rp_id}</span> and cannot sign in here. A passkey
            is permanently tied to the address it was made on; add a new one below and
            remove the old.
          </Notice>
        )}

        {ready && ceremony.error && (
          <Notice tone="error">
            {ceremony.error}{" "}
            <button className="btn btn--small" type="button" onClick={ceremony.refresh}>
              Retry
            </button>
          </Notice>
        )}
      </div>

      {keys.length > 0 && (
        <div className="rows">
          {keys.map((key) => (
            <Row
              key={key.id}
              state={key.usable ? "healthy" : "degraded"}
              columns="minmax(0,1fr) auto auto"
            >
              <div>
                <div className="row__primary">{key.label}</div>
                <div className="row__secondary">
                  {key.last_used_at
                    ? `last used ${relativeTime(key.last_used_at)}`
                    : "never used"}
                  {` · added ${relativeTime(key.created_at)}`}
                  {!key.usable && key.rp_id && (
                    <>
                      {" · made for "}
                      <span className="mono">{key.rp_id}</span>
                    </>
                  )}
                </div>
              </div>
              <div style={{ display: "flex", gap: 6 }}>
                {key.usable ? (
                  <Tag tone={key.is_backed_up ? "hit" : "signal"}>
                    {key.is_backed_up ? "synced" : "this device only"}
                  </Tag>
                ) : (
                  <Tag tone="warn">does not work here</Tag>
                )}
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

      <form className="panel__body" onSubmit={enrol}>
        {ready && (
          <label className="field" style={{ maxWidth: 320 }}>
            <span className="field__label">Device name</span>
            <input
              className="input"
              value={label}
              maxLength={64}
              onChange={(e) => setLabel(e.target.value)}
              disabled={busy}
            />
          </label>
        )}
        <button
          type="submit"
          className="btn btn--primary"
          disabled={busy || !ready || !ceremony.options}
        >
          {busy
            ? "Waiting for your device…"
            : ready && !ceremony.options && !ceremony.error
              ? "Preparing…"
              : "Add a passkey"}
        </button>

        {ready && platform === false && (
          <p className="field__help" style={{ marginTop: 8 }}>
            This device has no built-in fingerprint, face or PIN unlock that the
            panel can use. You can still enrol a security key, or a phone, from the
            prompt — or set up a screen lock here first.
          </p>
        )}
      </form>
    </Panel>
  );
}
