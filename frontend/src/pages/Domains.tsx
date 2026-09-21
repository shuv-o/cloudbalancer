import { FormEvent, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { Domain } from "../lib/types";
import { expiryPhrase, relativeTime } from "../lib/format";
import { Check, Empty, Field, Modal, Notice, Panel, Row, Tag, useToast } from "../components/ui";

interface DraftDomain {
  name: string;
  description: string;
  force_ssl_redirect: boolean;
  is_active: boolean;
}

const BLANK: DraftDomain = {
  name: "",
  description: "",
  force_ssl_redirect: true,
  is_active: true,
};

function DomainForm({
  initial,
  existing,
  onClose,
  onSaved,
}: {
  initial: DraftDomain;
  existing: Domain | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const toast = useToast();

  const [save, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    if (existing) {
      await api.patch(`/api/v1/domains/${existing.id}/`, draft);
      toast(`Saved ${draft.name}.`);
    } else {
      await api.post("/api/v1/domains/", draft);
      toast(`Added ${draft.name}. It is now answering on port 80.`);
    }
    onSaved();
    onClose();
  });

  return (
    <Modal
      title={existing ? `Edit ${existing.name}` : "Add a domain"}
      subtitle={
        existing
          ? undefined
          : "The gateway starts accepting this hostname as soon as you save, and answers certificate challenges for it straight away."
      }
      onClose={onClose}
      footer={
        <>
          <button className="btn" onClick={onClose} type="button">
            Cancel
          </button>
          <button className="btn btn--primary" form="domain-form" disabled={busy || !draft.name}>
            {busy ? "Saving…" : existing ? "Save changes" : "Add domain"}
          </button>
        </>
      }
    >
      <form id="domain-form" onSubmit={save}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <Field
          label="Hostname"
          help="The name clients will use, e.g. api.example.com. One hostname per domain."
          error={error?.fields.name?.[0]}
        >
          <input
            className="input input--mono"
            value={draft.name}
            placeholder="api.example.com"
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            autoFocus
            required
          />
        </Field>

        <Field label="Notes" help="Optional. What this hostname is for.">
          <input
            className="input"
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          />
        </Field>

        <Check
          label="Redirect HTTP to HTTPS"
          help="Applies once a certificate exists. Challenge requests are always answered over plain HTTP, so renewals keep working."
          checked={draft.force_ssl_redirect}
          onChange={(v) => setDraft({ ...draft, force_ssl_redirect: v })}
        />

        <Check
          label="Accept traffic"
          help="Turn this off to take the hostname out of the gateway without deleting its routes."
          checked={draft.is_active}
          onChange={(v) => setDraft({ ...draft, is_active: v })}
        />
      </form>
    </Modal>
  );
}

export function Domains() {
  const { data, initialLoading, reload } = useResource<Domain[]>("/api/v1/domains/", 15000);
  const [editing, setEditing] = useState<Domain | null>(null);
  const [adding, setAdding] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState<Domain | null>(null);
  const toast = useToast();

  const [remove, { busy: removing }] = useAction(async (domain: Domain) => {
    await api.delete(`/api/v1/domains/${domain.id}/`);
    toast(`Removed ${domain.name} and its routes.`);
    setConfirmDelete(null);
    reload();
  });

  if (initialLoading) return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;

  const domains = data ?? [];

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Domains</h1>
          <p>
            Each hostname the gateway answers for. A domain decides which server block a
            request lands in; the routes inside it decide where it goes next.
          </p>
        </div>
        <div className="page-actions">
          <button className="btn btn--primary" onClick={() => setAdding(true)}>
            Add domain
          </button>
        </div>
      </header>

      <Panel title="Configured hostnames" flush>
        {domains.length === 0 ? (
          <Empty
            title="No domains yet"
            body="Add the first hostname the gateway should answer for. You can request a certificate for it straight afterwards."
            action={
              <button className="btn btn--primary" onClick={() => setAdding(true)}>
                Add domain
              </button>
            }
          />
        ) : (
          <div className="rows">
            {domains.map((domain) => {
              const cert = domain.certificate;
              const state = !domain.is_active
                ? "idle"
                : cert?.status === "failed"
                  ? "down"
                  : cert && cert.days_until_expiry !== null && cert.days_until_expiry < 14
                    ? "degraded"
                    : domain.ssl_enabled
                      ? "healthy"
                      : "active";

              return (
                <Row key={domain.id} state={state} columns="minmax(0,1fr) auto auto auto">
                  <div>
                    <div className="row__primary">{domain.name}</div>
                    <div className="row__secondary">
                      {domain.active_rule_count ?? 0}{" "}
                      {domain.active_rule_count === 1 ? "route" : "routes"}
                      {domain.description && ` · ${domain.description}`}
                    </div>
                  </div>

                  <div style={{ display: "flex", gap: 6 }}>
                    {!domain.is_active && <Tag>paused</Tag>}
                    {domain.ssl_enabled ? (
                      cert ? (
                        <Tag
                          tone={
                            cert.status === "failed"
                              ? "down"
                              : cert.days_until_expiry !== null && cert.days_until_expiry < 14
                                ? "warn"
                                : "hit"
                          }
                        >
                          {cert.issuer === "self_signed" ? "self-signed" : "HTTPS"}
                        </Tag>
                      ) : (
                        <Tag tone="hit">HTTPS</Tag>
                      )
                    ) : (
                      <Tag>HTTP only</Tag>
                    )}
                  </div>

                  <div
                    className="row__num"
                    style={{ color: "var(--text-faint)", fontSize: "0.78rem" }}
                  >
                    {cert
                      ? expiryPhrase(cert.days_until_expiry)
                      : `added ${relativeTime(domain.created_at)}`}
                  </div>

                  <div className="row__actions">
                    {!domain.ssl_enabled && (
                      <Link className="btn btn--small" to="/certificates">
                        Get a certificate
                      </Link>
                    )}
                    <button className="btn btn--small" onClick={() => setEditing(domain)}>
                      Edit
                    </button>
                    <button
                      className="btn btn--small btn--danger"
                      onClick={() => setConfirmDelete(domain)}
                    >
                      Delete
                    </button>
                  </div>
                </Row>
              );
            })}
          </div>
        )}
      </Panel>

      {adding && (
        <DomainForm
          initial={BLANK}
          existing={null}
          onClose={() => setAdding(false)}
          onSaved={reload}
        />
      )}

      {editing && (
        <DomainForm
          initial={{
            name: editing.name,
            description: editing.description,
            force_ssl_redirect: editing.force_ssl_redirect,
            is_active: editing.is_active,
          }}
          existing={editing}
          onClose={() => setEditing(null)}
          onSaved={reload}
        />
      )}

      {confirmDelete && (
        <Modal
          title={`Delete ${confirmDelete.name}?`}
          onClose={() => setConfirmDelete(null)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmDelete(null)}>
                Keep it
              </button>
              <button
                className="btn btn--danger"
                disabled={removing}
                onClick={() => remove(confirmDelete)}
              >
                {removing ? "Deleting…" : "Delete domain"}
              </button>
            </>
          }
        >
          <Notice tone="warn">
            <span>
              This removes the hostname and its {confirmDelete.active_rule_count ?? 0} routes.
              Traffic to <span className="mono">{confirmDelete.name}</span> will stop being
              accepted at the next deploy.
              {confirmDelete.certificate &&
                " The certificate stays on disk and can be reattached later."}
            </span>
          </Notice>
        </Modal>
      )}
    </>
  );
}
