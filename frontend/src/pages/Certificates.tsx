import { FormEvent, useState } from "react";
import { api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { AcmeAccount, Certificate, Domain } from "../lib/types";
import { dateTime, expiryPhrase, relativeTime } from "../lib/format";
import { Check, Empty, Field, Modal, Notice, Panel, Row, Tag, useToast } from "../components/ui";

const DIRECTORIES = [
  {
    value: "https://acme-v02.api.letsencrypt.org/directory",
    label: "Let's Encrypt",
    note: "Free, trusted by every browser. Rate limited to 50 certificates per domain per week.",
  },
  {
    value: "https://acme-staging-v02.api.letsencrypt.org/directory",
    label: "Let's Encrypt staging",
    note: "Certificates browsers will not trust, with far looser limits. Use it to test that issuance works before spending a real one.",
  },
  {
    value: "https://acme.zerossl.com/v2/DV90",
    label: "ZeroSSL",
    note: "Needs the key ID and HMAC from your ZeroSSL account.",
  },
] as const;

function AccountForm({ onClose, onSaved }: { onClose: () => void; onSaved: () => void }) {
  const [draft, setDraft] = useState({
    email: "",
    directory_url: DIRECTORIES[0].value as string,
    agreed_to_tos: false,
    external_account_kid: "",
    external_account_hmac: "",
    is_default: true,
  });
  const toast = useToast();

  const [save, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    await api.post("/api/v1/certificates/acme-accounts/", draft);
    toast("Certificate account registered.");
    onSaved();
    onClose();
  });

  const directory = DIRECTORIES.find((d) => d.value === draft.directory_url);
  const needsEab = draft.directory_url.includes("zerossl");

  return (
    <Modal
      title="Connect a certificate authority"
      subtitle="The gateway uses this account to request and renew certificates on your behalf."
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn--primary"
            form="account-form"
            disabled={busy || !draft.email || !draft.agreed_to_tos}
          >
            {busy ? "Registering…" : "Register"}
          </button>
        </>
      }
    >
      <form id="account-form" onSubmit={save}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <Field label="Authority" help={directory?.note}>
          <select
            className="select"
            value={draft.directory_url}
            onChange={(e) => setDraft({ ...draft, directory_url: e.target.value })}
          >
            {DIRECTORIES.map((d) => (
              <option key={d.value} value={d.value}>
                {d.label}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Contact address"
          help="The authority emails this address before a certificate expires."
          error={error?.fields.email?.[0]}
        >
          <input
            className="input"
            type="email"
            value={draft.email}
            placeholder="ops@example.com"
            onChange={(e) => setDraft({ ...draft, email: e.target.value })}
            required
          />
        </Field>

        {needsEab && (
          <div className="row-2">
            <Field label="Key ID">
              <input
                className="input input--mono"
                value={draft.external_account_kid}
                onChange={(e) => setDraft({ ...draft, external_account_kid: e.target.value })}
              />
            </Field>
            <Field label="HMAC key">
              <input
                className="input input--mono"
                value={draft.external_account_hmac}
                onChange={(e) => setDraft({ ...draft, external_account_hmac: e.target.value })}
              />
            </Field>
          </div>
        )}

        <Check
          label="I accept the authority's subscriber agreement"
          checked={draft.agreed_to_tos}
          onChange={(v) => setDraft({ ...draft, agreed_to_tos: v })}
        />

        <Check
          label="Use this account by default"
          checked={draft.is_default}
          onChange={(v) => setDraft({ ...draft, is_default: v })}
        />
      </form>
    </Modal>
  );
}

function RequestForm({
  domains,
  accounts,
  onClose,
  onSaved,
}: {
  domains: Domain[];
  accounts: AcmeAccount[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [domainId, setDomainId] = useState(domains[0]?.id ?? 0);
  const [accountId, setAccountId] = useState(
    accounts.find((a) => a.is_default)?.id ?? accounts[0]?.id ?? 0,
  );
  const [extra, setExtra] = useState("");
  const toast = useToast();

  const [request, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    await api.post("/api/v1/certificates/request/", {
      domain_id: domainId,
      acme_account_id: accountId || null,
      extra_domains: extra.split("\n").filter((d) => d.trim()),
    });
    toast("Requesting a certificate. This usually takes under a minute.");
    onSaved();
    onClose();
  });

  const domain = domains.find((d) => d.id === domainId);

  return (
    <Modal
      title="Get a certificate"
      subtitle="The authority will check that this gateway answers for the hostname before it issues anything."
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn--primary" form="request-form" disabled={busy || !domainId}>
            {busy ? "Requesting…" : "Request certificate"}
          </button>
        </>
      }
    >
      <form id="request-form" onSubmit={request}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <Field label="Hostname">
          <select
            className="select"
            value={domainId}
            onChange={(e) => setDomainId(Number(e.target.value))}
          >
            {domains.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
                {d.certificate ? " (renew)" : ""}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Account">
          <select
            className="select"
            value={accountId}
            onChange={(e) => setAccountId(Number(e.target.value))}
          >
            {accounts.map((a) => (
              <option key={a.id} value={a.id}>
                {a.email} — {a.directory_label}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Also cover these names"
          help="One per line, e.g. a www alias. Every name must point at this gateway."
        >
          <textarea
            className="textarea textarea--mono"
            style={{ minHeight: 70 }}
            value={extra}
            placeholder={domain ? `www.${domain.name}` : ""}
            onChange={(e) => setExtra(e.target.value)}
          />
        </Field>

        <Notice>
          <span>
            Before this can work, <span className="mono">{domain?.name ?? "the hostname"}</span>{" "}
            must resolve to this gateway's public address, and port 80 must be reachable from
            the internet. The gateway already answers the challenge path — that part is set up
            the moment a domain is added.
          </span>
        </Notice>
      </form>
    </Modal>
  );
}

function ImportForm({
  domains,
  onClose,
  onSaved,
}: {
  domains: Domain[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState({
    domain_id: domains[0]?.id ?? 0,
    cert_pem: "",
    key_pem: "",
  });
  const toast = useToast();

  const [save, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    await api.post("/api/v1/certificates/import/", draft);
    toast("Certificate stored. Deploying.");
    onSaved();
    onClose();
  });

  return (
    <Modal
      title="Upload a certificate"
      subtitle="For certificates issued elsewhere. The gateway checks the key matches before storing either."
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn--primary"
            form="import-form"
            disabled={busy || !draft.cert_pem || !draft.key_pem}
          >
            {busy ? "Storing…" : "Store certificate"}
          </button>
        </>
      }
    >
      <form id="import-form" onSubmit={save}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <Field label="Hostname">
          <select
            className="select"
            value={draft.domain_id}
            onChange={(e) => setDraft({ ...draft, domain_id: Number(e.target.value) })}
          >
            {domains.map((d) => (
              <option key={d.id} value={d.id}>
                {d.name}
              </option>
            ))}
          </select>
        </Field>

        <Field
          label="Certificate chain"
          help="The certificate followed by any intermediates, in PEM form."
        >
          <textarea
            className="textarea textarea--mono"
            value={draft.cert_pem}
            placeholder="-----BEGIN CERTIFICATE-----"
            onChange={(e) => setDraft({ ...draft, cert_pem: e.target.value })}
            required
          />
        </Field>

        <Field label="Private key" help="Unencrypted PEM. It is written with owner-only permissions.">
          <textarea
            className="textarea textarea--mono"
            value={draft.key_pem}
            placeholder="-----BEGIN PRIVATE KEY-----"
            onChange={(e) => setDraft({ ...draft, key_pem: e.target.value })}
            required
          />
        </Field>

        <Notice tone="warn">
          <span>
            Uploaded certificates are not renewed automatically. You will need to replace this
            one before it expires.
          </span>
        </Notice>
      </form>
    </Modal>
  );
}

export function Certificates() {
  const { data: certificates, initialLoading, reload } = useResource<Certificate[]>(
    "/api/v1/certificates/",
    15000,
  );
  const { data: accounts, reload: reloadAccounts } = useResource<AcmeAccount[]>(
    "/api/v1/certificates/acme-accounts/",
  );
  const { data: domains } = useResource<Domain[]>("/api/v1/domains/");

  const [addingAccount, setAddingAccount] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const [importing, setImporting] = useState(false);
  const [detail, setDetail] = useState<Certificate | null>(null);
  const toast = useToast();

  const [renew] = useAction(async (certificate: Certificate) => {
    await api.post(`/api/v1/certificates/${certificate.id}/renew/`);
    toast(`Renewing ${certificate.domain_name}.`);
    window.setTimeout(reload, 4000);
  });

  const [selfSign] = useAction(async (domainId: number) => {
    await api.post("/api/v1/certificates/self-signed/", { domain_id: domainId });
    toast("Generating a self-signed certificate.");
    window.setTimeout(reload, 2500);
  });

  const [toggleRenewal] = useAction(async (certificate: Certificate) => {
    await api.patch(`/api/v1/certificates/${certificate.id}/`, {
      auto_renew: !certificate.auto_renew,
    });
    toast(
      certificate.auto_renew
        ? `Automatic renewal turned off for ${certificate.domain_name}.`
        : `${certificate.domain_name} will renew automatically.`,
    );
    reload();
  });

  const [remove] = useAction(async (certificate: Certificate) => {
    await api.delete(`/api/v1/certificates/${certificate.id}/`);
    toast(`${certificate.domain_name} is back on plain HTTP.`);
    setDetail(null);
    reload();
  });

  if (initialLoading) return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;

  const hasAccount = (accounts?.length ?? 0) > 0;
  const hasDomain = (domains?.length ?? 0) > 0;
  const certs = certificates ?? [];
  const uncovered = (domains ?? []).filter((d) => !d.certificate);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Certificates</h1>
          <p>
            TLS for every hostname the gateway answers for. Certificates from an authority
            renew on their own, thirty days before they expire, without a reload you have to
            remember to run.
          </p>
        </div>
        <div className="page-actions">
          {hasAccount && hasDomain && (
            <button className="btn btn--primary" onClick={() => setRequesting(true)}>
              Get a certificate
            </button>
          )}
          {hasDomain && (
            <button className="btn" onClick={() => setImporting(true)}>
              Upload one
            </button>
          )}
        </div>
      </header>

      {!hasAccount && (
        <Notice tone="warn">
          <span>
            <strong>No certificate authority connected.</strong> Connect one and the gateway
            can request and renew certificates for every hostname on its own.{" "}
            <button
              className="btn btn--small"
              style={{ marginLeft: 8 }}
              onClick={() => setAddingAccount(true)}
            >
              Connect one
            </button>
          </span>
        </Notice>
      )}

      {hasAccount && accounts?.some((a) => a.is_staging) && (
        <Notice>
          <span>
            A staging account is configured. Certificates from it are not trusted by browsers —
            useful for confirming issuance works, but swap to production before going live.
          </span>
        </Notice>
      )}

      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <Panel
          title="Issued certificates"
          hint={certs.length ? `${certs.length} total` : undefined}
          flush
        >
          {certs.length === 0 ? (
            <Empty
              title="No certificates yet"
              body={
                hasDomain
                  ? "Request one from a certificate authority, or generate a self-signed certificate for an internal hostname a public authority will not validate."
                  : "Add a domain first, then request a certificate for it."
              }
              action={
                hasAccount && hasDomain ? (
                  <button className="btn btn--primary" onClick={() => setRequesting(true)}>
                    Get a certificate
                  </button>
                ) : undefined
              }
            />
          ) : (
            <div className="rows">
              {certs.map((certificate) => {
                const days = certificate.days_until_expiry;
                const state =
                  certificate.status === "failed"
                    ? "down"
                    : days !== null && days < 0
                      ? "down"
                      : days !== null && days < 14
                        ? "degraded"
                        : certificate.status === "active"
                          ? "healthy"
                          : "idle";

                return (
                  <Row
                    key={certificate.id}
                    state={state}
                    columns="minmax(0,1fr) auto auto auto"
                  >
                    <div>
                      <div className="row__primary">{certificate.domain_name}</div>
                      <div className="row__secondary">
                        {certificate.issuer_label}
                        {certificate.san_domains.length > 1 &&
                          ` · covers ${certificate.san_domains.length} names`}
                        {certificate.last_error && ` · ${certificate.last_error.slice(0, 80)}`}
                      </div>
                    </div>

                    <div style={{ display: "flex", gap: 6 }}>
                      {certificate.status === "failed" && <Tag tone="down">failed</Tag>}
                      {certificate.status === "requesting" && <Tag tone="signal">requesting</Tag>}
                      {certificate.status === "renewing" && <Tag tone="signal">renewing</Tag>}
                      {certificate.auto_renew && certificate.issuer === "acme" && (
                        <Tag tone="hit">renews itself</Tag>
                      )}
                      {certificate.issuer === "self_signed" && <Tag tone="warn">self-signed</Tag>}
                      {certificate.issuer === "manual" && <Tag>uploaded</Tag>}
                    </div>

                    <div
                      className="row__num"
                      style={{ fontSize: "0.78rem", color: "var(--text-faint)" }}
                    >
                      {expiryPhrase(days)}
                    </div>

                    <div className="row__actions">
                      <button className="btn btn--small" onClick={() => setDetail(certificate)}>
                        Details
                      </button>
                      {certificate.issuer === "acme" && (
                        <button className="btn btn--small" onClick={() => renew(certificate)}>
                          Renew now
                        </button>
                      )}
                    </div>
                  </Row>
                );
              })}
            </div>
          )}
        </Panel>

        {uncovered.length > 0 && (
          <Panel
            title="Hostnames without a certificate"
            hint="These are served over plain HTTP"
            flush
          >
            <div className="rows">
              {uncovered.map((domain) => (
                <Row key={domain.id} state="idle" columns="minmax(0,1fr) auto">
                  <div>
                    <div className="row__primary">{domain.name}</div>
                    <div className="row__secondary">
                      Already answering certificate challenges — nothing else to set up
                    </div>
                  </div>
                  <div className="row__actions">
                    {hasAccount && (
                      <button className="btn btn--small" onClick={() => setRequesting(true)}>
                        Get a certificate
                      </button>
                    )}
                    <button className="btn btn--small" onClick={() => selfSign(domain.id)}>
                      Self-sign
                    </button>
                  </div>
                </Row>
              ))}
            </div>
          </Panel>
        )}

        <Panel
          title="Certificate authorities"
          actions={
            <button className="btn btn--small" onClick={() => setAddingAccount(true)}>
              Connect another
            </button>
          }
          flush
        >
          {!hasAccount ? (
            <Empty
              title="No authority connected"
              body="Connecting one lets the gateway request certificates and keep them renewed without anyone having to remember."
              action={
                <button className="btn btn--primary" onClick={() => setAddingAccount(true)}>
                  Connect an authority
                </button>
              }
            />
          ) : (
            <div className="rows">
              {accounts!.map((account) => (
                <Row
                  key={account.id}
                  state={account.is_staging ? "degraded" : "healthy"}
                  columns="minmax(0,1fr) auto auto"
                >
                  <div>
                    <div className="row__primary">{account.email}</div>
                    <div className="row__secondary">
                      {account.directory_label} · {account.certificate_count}{" "}
                      {account.certificate_count === 1 ? "certificate" : "certificates"}
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    {account.is_default && <Tag tone="signal">default</Tag>}
                    {account.is_staging && <Tag tone="warn">not browser-trusted</Tag>}
                  </div>
                  <div
                    className="row__num"
                    style={{ fontSize: "0.78rem", color: "var(--text-faint)" }}
                  >
                    {account.registered_at
                      ? `registered ${relativeTime(account.registered_at)}`
                      : "not registered"}
                  </div>
                </Row>
              ))}
            </div>
          )}
        </Panel>
      </div>

      {addingAccount && (
        <AccountForm onClose={() => setAddingAccount(false)} onSaved={reloadAccounts} />
      )}

      {requesting && domains && accounts && (
        <RequestForm
          domains={domains}
          accounts={accounts}
          onClose={() => setRequesting(false)}
          onSaved={reload}
        />
      )}

      {importing && domains && (
        <ImportForm domains={domains} onClose={() => setImporting(false)} onSaved={reload} />
      )}

      {detail && (
        <Modal
          title={detail.domain_name}
          subtitle={`${detail.issuer_label} · ${detail.status_label}`}
          wide
          onClose={() => setDetail(null)}
          footer={
            <>
              <button className="btn btn--danger" onClick={() => remove(detail)}>
                Remove and revert to HTTP
              </button>
              <div style={{ flex: 1 }} />
              <button className="btn" onClick={() => setDetail(null)}>
                Close
              </button>
            </>
          }
        >
          {detail.last_error && (
            <Notice tone="error">
              <span>
                <strong>Last attempt failed.</strong> {detail.last_error}
              </span>
            </Notice>
          )}

          <dl
            style={{
              display: "grid",
              gridTemplateColumns: "auto 1fr",
              gap: "9px 18px",
              margin: 0,
              fontSize: "0.85rem",
            }}
          >
            <dt style={{ color: "var(--text-faint)" }}>Covers</dt>
            <dd className="mono" style={{ margin: 0 }}>
              {detail.san_domains.length ? detail.san_domains.join(", ") : detail.domain_name}
            </dd>

            <dt style={{ color: "var(--text-faint)" }}>Valid from</dt>
            <dd style={{ margin: 0 }}>{dateTime(detail.not_before)}</dd>

            <dt style={{ color: "var(--text-faint)" }}>Valid until</dt>
            <dd style={{ margin: 0 }}>
              {dateTime(detail.not_after)}{" "}
              <span style={{ color: "var(--text-faint)" }}>
                — {expiryPhrase(detail.days_until_expiry)}
              </span>
            </dd>

            <dt style={{ color: "var(--text-faint)" }}>Serial</dt>
            <dd className="mono" style={{ margin: 0, wordBreak: "break-all" }}>
              {detail.serial_number || "—"}
            </dd>

            <dt style={{ color: "var(--text-faint)" }}>Fingerprint</dt>
            <dd
              className="mono"
              style={{ margin: 0, wordBreak: "break-all", fontSize: "0.72rem" }}
            >
              {detail.fingerprint_sha256 || "—"}
            </dd>

            <dt style={{ color: "var(--text-faint)" }}>Last issued</dt>
            <dd style={{ margin: 0 }}>{relativeTime(detail.last_issued_at)}</dd>
          </dl>

          {detail.issuer === "acme" && (
            <div style={{ marginTop: 18 }}>
              <Check
                label="Renew automatically"
                help={`Renewal starts ${detail.renew_before_days} days before expiry and needs no reload you have to run yourself.`}
                checked={detail.auto_renew}
                onChange={() => toggleRenewal(detail)}
              />
            </div>
          )}

          {detail.issuer !== "acme" && (
            <Notice tone="warn">
              <span>
                This certificate did not come from a certificate authority, so the gateway
                cannot renew it. Replace it before {dateTime(detail.not_after)}.
              </span>
            </Notice>
          )}
        </Modal>
      )}
    </>
  );
}
