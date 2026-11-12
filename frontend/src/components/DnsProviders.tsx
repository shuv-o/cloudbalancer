/**
 * Where each zone's DNS lives, which is what makes a wildcard possible.
 *
 * A wildcard certificate cannot be issued over HTTP-01 — proving control of
 * one subdomain says nothing about another, so the only proof a CA accepts is
 * a TXT record in the zone. That means credentials, and which credentials
 * depends on whose nameserver holds the zone. Some will be Cloudflare's and
 * some a local BIND or Knot, so both are here and a domain is matched to
 * whichever provider holds its zone.
 */
import { useState } from "react";
import { ApiError, api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { relativeTime } from "../lib/format";
import { Empty, Field, Modal, Notice, Outcome, Panel, Row, Tag, useToast } from "./ui";

interface Provider {
  id: number;
  name: string;
  kind: "cloudflare" | "rfc2136";
  kind_label: string;
  zones: string[];
  credentials: string;
  server: string;
  port: number;
  tsig_name: string;
  tsig_algorithm: string;
  propagation_seconds: number;
  is_enabled: boolean;
  last_used_at: string | null;
  last_error: string;
}

interface Coverage {
  domain: string;
  is_wildcard: boolean;
  provider: string | null;
  zone: string | null;
}

interface Payload {
  providers: Provider[];
  algorithms: string[];
  coverage: Coverage[];
}

const BLANK = {
  name: "",
  kind: "cloudflare" as Provider["kind"],
  zones: "",
  api_token: "",
  server: "",
  port: 53,
  tsig_name: "",
  tsig_algorithm: "HMAC-SHA256",
  tsig_secret: "",
  propagation_seconds: 60,
};

function ProviderForm({
  algorithms,
  onClose,
  onSaved,
}: {
  algorithms: string[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState(BLANK);
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  const [submit, { busy }] = useAction(async () => {
    setError(null);
    const zones = draft.zones
      .split(/[\s,]+/)
      .map((z) => z.trim())
      .filter(Boolean);

    const body =
      draft.kind === "cloudflare"
        ? {
            name: draft.name,
            kind: draft.kind,
            zones,
            api_token: draft.api_token,
            propagation_seconds: draft.propagation_seconds,
          }
        : {
            name: draft.name,
            kind: draft.kind,
            zones,
            server: draft.server,
            port: draft.port,
            tsig_name: draft.tsig_name,
            tsig_algorithm: draft.tsig_algorithm,
            tsig_secret: draft.tsig_secret,
            propagation_seconds: draft.propagation_seconds,
          };

    try {
      await api.post("/api/v1/certificates/dns-providers/", body);
      toast(`${draft.name} added. Domains in ${zones.join(", ")} can now use wildcards.`);
      onSaved();
      onClose();
    } catch (err) {
      setError((err as ApiError).message);
    }
  });

  return (
    <Modal
      title="Add a DNS provider"
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn--primary"
            form="dns-provider"
            disabled={busy || !draft.name || !draft.zones}
          >
            {busy ? "Adding…" : "Add provider"}
          </button>
        </>
      }
    >
      <form
        id="dns-provider"
        onSubmit={(e) => {
          e.preventDefault();
          submit();
        }}
      >
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error}
          </div>
        )}

        <div className="row-2">
          <Field label="Name" help="How this appears when a certificate is issued.">
            <input
              className="input"
              value={draft.name}
              autoFocus
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              required
            />
          </Field>

          <Field label="Kind">
            <select
              className="select"
              value={draft.kind}
              onChange={(e) =>
                setDraft({ ...draft, kind: e.target.value as Provider["kind"] })
              }
            >
              <option value="cloudflare">Cloudflare</option>
              <option value="rfc2136">BIND, Knot or other RFC2136</option>
            </select>
          </Field>
        </div>

        <Field
          label="Zones"
          help="Apex zones this provider is authoritative for, separated by spaces or commas. A domain is matched to whichever provider holds the longest matching zone, so a delegated sub-zone on another server still works."
        >
          <input
            className="input input--mono"
            value={draft.zones}
            placeholder="example.com example.net"
            onChange={(e) => setDraft({ ...draft, zones: e.target.value })}
            required
          />
        </Field>

        {draft.kind === "cloudflare" ? (
          <>
            <Field
              label="API token"
              help="A token, not the global API key. Scope it to Zone:DNS:Edit on these zones — a global key can change billing."
            >
              <input
                className="input input--mono"
                type="password"
                value={draft.api_token}
                autoComplete="off"
                onChange={(e) => setDraft({ ...draft, api_token: e.target.value })}
              />
            </Field>
            <Notice tone="info">
              <span>
                Stored encrypted, and never shown again — not here and not in
                the API. It is in every nightly backup, which is why.
              </span>
            </Notice>
          </>
        ) : (
          <>
            <div className="row-2">
              <Field label="Nameserver" help="Where dynamic updates are sent.">
                <input
                  className="input input--mono"
                  value={draft.server}
                  placeholder="10.0.0.53"
                  onChange={(e) => setDraft({ ...draft, server: e.target.value })}
                />
              </Field>
              <Field label="Port">
                <input
                  className="input"
                  type="number"
                  value={draft.port}
                  onChange={(e) => setDraft({ ...draft, port: Number(e.target.value) })}
                />
              </Field>
            </div>

            <div className="row-2">
              <Field label="TSIG key name">
                <input
                  className="input input--mono"
                  value={draft.tsig_name}
                  placeholder="cloudbalancer-key"
                  onChange={(e) => setDraft({ ...draft, tsig_name: e.target.value })}
                />
              </Field>
              <Field label="Algorithm">
                <select
                  className="select"
                  value={draft.tsig_algorithm}
                  onChange={(e) =>
                    setDraft({ ...draft, tsig_algorithm: e.target.value })
                  }
                >
                  {algorithms.map((algorithm) => (
                    <option key={algorithm} value={algorithm}>
                      {algorithm}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            <Field label="TSIG secret" help="The base64 key material.">
              <input
                className="input input--mono"
                type="password"
                value={draft.tsig_secret}
                autoComplete="off"
                onChange={(e) => setDraft({ ...draft, tsig_secret: e.target.value })}
              />
            </Field>

            <Notice tone="info">
              <span>
                The zone must accept updates signed with this key — an{" "}
                <span className="mono">acl</span> with the{" "}
                <span className="mono">update</span> action in Knot, or{" "}
                <span className="mono">allow-update &#123; key … &#125;</span> in
                BIND.
              </span>
            </Notice>
          </>
        )}

        <Field
          label="Propagation wait (seconds)"
          help="How long to wait after writing the record before asking the CA to check. Cloudflare's edge is fast and consistent; a pair of servers with zone transfers between them is not."
        >
          <input
            className="input"
            type="number"
            value={draft.propagation_seconds}
            onChange={(e) =>
              setDraft({ ...draft, propagation_seconds: Number(e.target.value) })
            }
          />
        </Field>
      </form>
    </Modal>
  );
}

export function DnsProviders() {
  const { data, reload } = useResource<Payload>("/api/v1/certificates/dns-providers/");
  const [adding, setAdding] = useState(false);
  const toast = useToast();

  const [remove] = useAction(async (provider: Provider) => {
    if (!window.confirm(`Remove ${provider.name}? Wildcards in its zones will stop renewing.`))
      return;
    try {
      await api.delete(`/api/v1/certificates/dns-providers/${provider.id}/`);
      toast(`${provider.name} removed.`);
      reload();
    } catch (err) {
      toast((err as ApiError).message, "error");
    }
  });

  if (!data) return null;

  const uncovered = data.coverage.filter((c) => c.is_wildcard && !c.provider);

  return (
    <>
      <Panel
        title="DNS providers"
        hint="What makes a wildcard certificate possible"
        actions={
          <button className="btn btn--small" onClick={() => setAdding(true)}>
            Add provider
          </button>
        }
        flush
      >
        <div className="panel__body">
          <Outcome>
            A wildcard cannot be proved over HTTP — control of one subdomain
            says nothing about another — so a CA will only accept a TXT record
            in the zone. Add the zones you run and{" "}
            <span className="mono">*.example.com</span> becomes a domain you can
            request a certificate for.
          </Outcome>

          {uncovered.length > 0 && (
            <Notice tone="warn">
              <span>
                {uncovered.map((c) => c.domain).join(", ")} cannot be issued:
                no provider holds the zone.
              </span>
            </Notice>
          )}
        </div>

        {data.providers.length === 0 ? (
          <Empty
            title="No DNS providers"
            body="Ordinary certificates work without one — they are proved over HTTP. This is only needed for wildcards, and for zones where port 80 cannot reach this gateway."
          />
        ) : (
          <div className="rows">
            {data.providers.map((provider) => (
              <Row
                key={provider.id}
                state={
                  !provider.is_enabled
                    ? "degraded"
                    : provider.last_error
                      ? "down"
                      : "healthy"
                }
                columns="minmax(0,1fr) auto auto"
              >
                <div>
                  <div className="row__primary">{provider.name}</div>
                  <div className="row__secondary">
                    {provider.kind_label} · <span className="mono">{provider.credentials}</span>
                    {provider.last_used_at
                      ? ` · last used ${relativeTime(provider.last_used_at)}`
                      : " · never used"}
                    {provider.last_error && (
                      <>
                        {" · "}
                        <span style={{ color: "var(--down)" }}>
                          {provider.last_error.slice(0, 120)}
                        </span>
                      </>
                    )}
                  </div>
                </div>

                <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                  {provider.zones.map((zone) => (
                    <Tag key={zone} tone="hit">
                      {zone}
                    </Tag>
                  ))}
                </div>

                <div className="row__actions">
                  <button className="btn btn--small" onClick={() => remove(provider)}>
                    Remove
                  </button>
                </div>
              </Row>
            ))}
          </div>
        )}
      </Panel>

      {adding && (
        <ProviderForm
          algorithms={data.algorithms}
          onClose={() => setAdding(false)}
          onSaved={reload}
        />
      )}
    </>
  );
}
