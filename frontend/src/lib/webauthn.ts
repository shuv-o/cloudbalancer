/**
 * The browser half of the two WebAuthn ceremonies.
 *
 * All of this is translation. The API speaks base64url because JSON cannot
 * carry bytes; `navigator.credentials` speaks ArrayBuffer. Every bug in a
 * first WebAuthn integration lives in that gap, so the conversion happens
 * here and nowhere else.
 */

function base64urlToBytes(value: string): Uint8Array {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(padded.padEnd(padded.length + ((4 - (padded.length % 4)) % 4), "="));
  return Uint8Array.from(binary, (c) => c.charCodeAt(0));
}

function bytesToBase64url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/**
 * Whether this browser can do passkeys at all.
 *
 * `navigator.credentials` is only exposed in a secure context, so this is
 * false over plain HTTP on anything but localhost — which is the single most
 * common reason the button should not be shown.
 */
export function supported(): boolean {
  return (
    typeof window !== "undefined" &&
    window.isSecureContext &&
    typeof window.PublicKeyCredential !== "undefined"
  );
}

/** Why passkeys are unavailable here, in words an operator can act on. */
export function unavailableReason(): string | null {
  if (typeof window === "undefined") return null;
  if (typeof window.PublicKeyCredential === "undefined") {
    return "This browser does not support passkeys.";
  }
  if (!window.isSecureContext) {
    return "Passkeys need a secure connection. Open the panel over HTTPS at its own hostname.";
  }
  return null;
}

type Options = Record<string, unknown>;

/** Create a credential. `options` is the server's response, passed through unchanged. */
export async function create(options: Options): Promise<Options> {
  const publicKey = {
    ...options,
    challenge: base64urlToBytes(options.challenge as string),
    user: {
      ...(options.user as Record<string, unknown>),
      id: base64urlToBytes((options.user as { id: string }).id),
    },
    excludeCredentials: ((options.excludeCredentials as { id: string }[]) ?? []).map((c) => ({
      ...c,
      id: base64urlToBytes(c.id),
    })),
  } as unknown as PublicKeyCredentialCreationOptions;

  const credential = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential;
  if (!credential) throw new Error("No passkey was created.");

  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: bytesToBase64url(credential.rawId),
    type: credential.type,
    // Sent so the server can record how the authenticator was reached, which
    // is what later tells an operator which device a passkey lives on.
    transports: response.getTransports?.() ?? [],
    response: {
      clientDataJSON: bytesToBase64url(response.clientDataJSON),
      attestationObject: bytesToBase64url(response.attestationObject),
    },
  };
}

/** Produce an assertion. No credential list is sent; the browser offers what it holds. */
export async function get(options: Options): Promise<Options> {
  const publicKey = {
    ...options,
    challenge: base64urlToBytes(options.challenge as string),
    allowCredentials: ((options.allowCredentials as { id: string }[]) ?? []).map((c) => ({
      ...c,
      id: base64urlToBytes(c.id),
    })),
  } as unknown as PublicKeyCredentialRequestOptions;

  const credential = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential;
  if (!credential) throw new Error("No passkey was offered.");

  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: bytesToBase64url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: bytesToBase64url(response.clientDataJSON),
      authenticatorData: bytesToBase64url(response.authenticatorData),
      signature: bytesToBase64url(response.signature),
      userHandle: response.userHandle ? bytesToBase64url(response.userHandle) : null,
    },
  };
}

/**
 * Turn a ceremony failure into something worth reading.
 *
 * The browser reports almost everything as NotAllowedError, including a
 * cancelled prompt and a timeout, so the raw message is close to useless.
 */
export function describe(error: unknown): string {
  const err = error as { name?: string; message?: string };
  switch (err?.name) {
    case "NotAllowedError":
      return "No passkey was used. The prompt was dismissed or it timed out.";
    case "InvalidStateError":
      return "This device already has a passkey for your account.";
    case "SecurityError":
      return "The page's address does not match the one passkeys are registered under.";
    case "AbortError":
      return "Cancelled.";
    default:
      return err?.message || "The passkey could not be used.";
  }
}
