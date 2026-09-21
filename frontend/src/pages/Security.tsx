import { FormEvent, useState } from "react";
import { api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { Domain } from "../lib/types";
import { dateTime, relativeTime } from "../lib/format";
import {
  Check,
  Empty,
  Field,
  Modal,
  Notice,
  Panel,
  Row,
  Tag,
  useToast,
} from "../components/ui";

interface Policy {
  panel_domain: string;
  is_published: boolean;
  ip_allowlist: string[];
  normalised_allowlist: string[];
  require_mtls: boolean;
  mtls_ca_path: string;
  hsts_seconds: number;
  require_totp: boolean;
  login_rate_per_minute: number;
  api_rate_per_second: number;
  lockout_threshold: number;
  lockout_minutes: number;
  session_idle_minutes: number;
  session_max_hours: number;
  expose_django_admin: boolean;
  warnings: string[];
}

interface Summary {
  published: boolean;
  panel_domain: string;
  warnings: string[];
  operators: { total: number; with_second_factor: number; without_second_factor: number };
  last_24h: {
    successful_sign_ins: number;
    failed_attempts: number;
    distinct_addresses: number;
  };
  locked_accounts: { username: string; minutes_remaining: number }[];
  changes_last_24h: number;
}

interface Operator {
  id: number;
  username: string;
  email: string;
  is_superuser: boolean;
  last_login: string | null;
  has_second_factor: boolean;
  recovery_codes_remaining: number;
}

interface AuditEvent {
  id: number;
  actor_name: string;
  action: string;
  path: string;
  status_code: number;
  ip_address: string | null;
  summary: string;
  created_at: string;
}

interface Attempt {
  id: number;
  username: string;
  ip_address: string | null;
  outcome: string;
  outcome_label: string;
  created_at: string;
}

function PublishForm({
  policy,
  domains,
  onClose,
  onSaved,
}: {
  policy: Policy;
  domains: Domain[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState({
    panel_domain: policy.panel_domain,
    is_published: policy.is_published,
    ip_allowlist: policy.ip_allowlist,
    require_mtls: policy.require_mtls,
    mtls_ca_path: policy.mtls_ca_path,
    expose_django_admin: policy.expose_django_admin,
  });
  const toast = useToast();

  const [save, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    await api.patch("/api/v1/security/policy/", draft);
    toast(
      draft.is_published
        ? `Panel published at ${draft.panel_domain}. Deploying.`
        : "Panel is back on the loopback port only. Deploying.",
    );
    onSaved();
    onClose();
  });

  const chosen = domains.find((d) => d.name === draft.panel_domain);
  const readyForTls = Boolean(chosen?.ssl_enabled && chosen?.certificate);
  const wideOpen = draft.is_published && draft.ip_allowlist.length === 0 && !draft.require_mtls;

  return (
    <Modal
      title="Where the panel can be reached"
      subtitle="Reaching this panel is equivalent to controlling every domain the gateway serves. The loopback port stays available whatever you set here."
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn--primary" form="publish-form" disabled={busy}>
            {busy ? "Saving…" : "Save and deploy"}
          </button>
        </>
      }
    >
      <form id="publish-form" onSubmit={save}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <Field
          label="Hostname"
          help="Must already exist as a domain, with a certificate. The panel is served only over HTTPS."
        >
          <select
            className="select"
            value={draft.panel_domain}
            onChange={(e) => setDraft({ ...draft, panel_domain: e.target.value })}
          >
            <option value="">Not published</option>
            {domains.map((d) => (
              <option key={d.id} value={d.name}>
                {d.name}
                {d.ssl_enabled ? "" : " — no certificate yet"}
              </option>
            ))}
          </select>
        </Field>

        {draft.panel_domain && !readyForTls && (
          <Notice tone="warn">
            <span>
              <span className="mono">{draft.panel_domain}</span> has no certificate.
              Get one on the Certificates page first — publishing over plain HTTP
              would expose the session cookie that controls this gateway.
            </span>
          </Notice>
        )}

        <Check
          label="Serve the panel on that hostname"
          help="Off keeps it on the loopback port, reachable only through an SSH tunnel or from the machine itself."
          checked={draft.is_published}
          disabled={!readyForTls}
          onChange={(v) => setDraft({ ...draft, is_published: v })}
        />

        <h3 style={{ fontSize: "0.86rem", margin: "22px 0 10px", color: "var(--text-dim)" }}>
          Who can connect
        </h3>

        <Field
          label="Allowed addresses"
          help="One per line. A single address like 203.0.113.4, or a range like 203.0.113.0/24. Leave empty to accept connections from anywhere."
        >
          <textarea
            className="textarea textarea--mono"
            style={{ minHeight: 84 }}
            value={draft.ip_allowlist.join("\n")}
            placeholder={"203.0.113.0/24\n198.51.100.17"}
            onChange={(e) =>
              setDraft({
                ...draft,
                ip_allowlist: e.target.value.split("\n").filter((l) => l.trim()),
              })
            }
          />
        </Field>

        <Check
          label="Require a client certificate"
          help="The strongest control available. Without a certificate signed by your CA, a caller is refused during the TLS handshake and never reaches the sign-in form at all."
          checked={draft.require_mtls}
          onChange={(v) => setDraft({ ...draft, require_mtls: v })}
        />

        {draft.require_mtls && (
          <Field
            label="CA bundle path"
            help="A PEM file inside the gateway container that signs your operator certificates. Mount it at /etc/nginx/ssl/."
          >
            <input
              className="input input--mono"
              value={draft.mtls_ca_path}
              placeholder="/etc/nginx/ssl/panel-ca.pem"
              onChange={(e) => setDraft({ ...draft, mtls_ca_path: e.target.value })}
            />
          </Field>
        )}

        {wideOpen && (
          <Notice tone="warn">
            <span>
              <strong>Anyone on the internet will reach the sign-in form.</strong> That
              is survivable with a second factor and account lockout, but an address
              allowlist or a client certificate means an attacker never gets that far.
            </span>
          </Notice>
        )}

        <h3 style={{ fontSize: "0.86rem", margin: "22px 0 10px", color: "var(--text-dim)" }}>
          Surface
        </h3>

        <Check
          label="Serve the Django admin publicly"
          help="A much larger surface than this panel's own API, with the same powers. Off by default; reach it over the loopback port instead."
          checked={draft.expose_django_admin}
          onChange={(v) => setDraft({ ...draft, expose_django_admin: v })}
        />
      </form>
    </Modal>
  );
}

function HardeningForm({
  policy,
  onClose,
  onSaved,
}: {
  policy: Policy;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState({
    require_totp: policy.require_totp,
    login_rate_per_minute: policy.login_rate_per_minute,
    api_rate_per_second: policy.api_rate_per_second,
    lockout_threshold: policy.lockout_threshold,
    lockout_minutes: policy.lockout_minutes,
    session_idle_minutes: policy.session_idle_minutes,
    session_max_hours: policy.session_max_hours,
  });
  const toast = useToast();

  const [save, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    await api.patch("/api/v1/security/policy/", draft);
    toast("Security settings saved.");
    onSaved();
    onClose();
  });

  return (
    <Modal
      title="Sign-in and sessions"
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn--primary" form="hardening-form" disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </button>
        </>
      }
    >
      <form id="hardening-form" onSubmit={save}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <Check
          label="Require an authenticator app"
          help="Every operator is asked to enrol before they can use the panel. Turning this off leaves a password as the only thing protecting the gateway."
          checked={draft.require_totp}
          onChange={(v) => setDraft({ ...draft, require_totp: v })}
        />

        {!draft.require_totp && (
          <Notice tone="warn">
            <span>
              One reused or leaked password would then be full control of every domain
              this gateway serves.
            </span>
          </Notice>
        )}

        <div className="row-2">
          <Field
            label="Failures before locking"
            help="Counted per account, so a distributed attempt is caught too. 0 never locks."
          >
            <input
              className="input"
              type="number"
              min={0}
              value={draft.lockout_threshold}
              onChange={(e) =>
                setDraft({ ...draft, lockout_threshold: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="Locked for (minutes)">
            <input
              className="input"
              type="number"
              min={1}
              value={draft.lockout_minutes}
              onChange={(e) => setDraft({ ...draft, lockout_minutes: Number(e.target.value) })}
            />
          </Field>
        </div>

        <div className="row-2">
          <Field label="Sign-in attempts per minute" help="Per client address, enforced at the gateway.">
            <input
              className="input"
              type="number"
              min={1}
              value={draft.login_rate_per_minute}
              onChange={(e) =>
                setDraft({ ...draft, login_rate_per_minute: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="Panel requests per second" help="Per client address, across everything else.">
            <input
              className="input"
              type="number"
              min={1}
              value={draft.api_rate_per_second}
              onChange={(e) =>
                setDraft({ ...draft, api_rate_per_second: Number(e.target.value) })
              }
            />
          </Field>
        </div>

        <div className="row-2">
          <Field
            label="Sign out after idle (minutes)"
            help="A session left open on an unattended machine is the commonest way an admin panel is misused."
          >
            <input
              className="input"
              type="number"
              min={5}
              value={draft.session_idle_minutes}
              onChange={(e) =>
                setDraft({ ...draft, session_idle_minutes: Number(e.target.value) })
              }
            />
          </Field>
          <Field label="Maximum session length (hours)">
            <input
              className="input"
              type="number"
              min={1}
              value={draft.session_max_hours}
              onChange={(e) =>
                setDraft({ ...draft, session_max_hours: Number(e.target.value) })
              }
            />
          </Field>
        </div>
      </form>
    </Modal>
  );
}

export function Security() {
  const { data: policy, reload: reloadPolicy } = useResource<Policy>(
    "/api/v1/security/policy/",
  );
  const { data: summary, reload: reloadSummary } = useResource<Summary>(
    "/api/v1/security/summary/",
    20000,
  );
  const { data: operators, reload: reloadOperators } = useResource<{ operators: Operator[] }>(
    "/api/v1/security/operators/",
  );
  const { data: audit } = useResource<AuditEvent[]>("/api/v1/security/audit/?limit=40", 20000);
  const { data: attempts } = useResource<Attempt[]>(
    "/api/v1/security/attempts/?limit=25",
    20000,
  );
  const { data: domains } = useResource<Domain[]>("/api/v1/domains/");

  const [publishing, setPublishing] = useState(false);
  const [hardening, setHardening] = useState(false);
  const toast = useToast();

  const reloadAll = () => {
    reloadPolicy();
    reloadSummary();
    reloadOperators();
  };

  const [resetFactor] = useAction(async (operator: Operator) => {
    await api.post("/api/v1/security/totp/reset/", { user_id: operator.id });
    toast(`${operator.username} will set up a new authenticator at next sign-in.`);
    reloadOperators();
  });

  if (!policy || !summary) {
    return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;
  }

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Security</h1>
          <p>
            How the panel is reached, and who has been reaching it. Every control here
            fails independently of the others, so defeating one still leaves the rest.
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" onClick={() => setHardening(true)}>
            Sign-in settings
          </button>
          <button className="btn btn--primary" onClick={() => setPublishing(true)}>
            {policy.is_published ? "Change exposure" : "Publish the panel"}
          </button>
        </div>
      </header>

      {policy.warnings.map((warning) => (
        <Notice key={warning} tone="warn">
          <span>{warning}</span>
        </Notice>
      ))}

      {summary.locked_accounts.length > 0 && (
        <Notice tone="error">
          <span>
            <strong>
              {summary.locked_accounts.length}{" "}
              {summary.locked_accounts.length === 1 ? "account is" : "accounts are"} locked
              after repeated failures:
            </strong>{" "}
            {summary.locked_accounts
              .map((l) => `${l.username} (${l.minutes_remaining}m)`)
              .join(", ")}
          </span>
        </Notice>
      )}

      <div className="grid grid--2" style={{ marginBottom: 16 }}>
        <Panel
          title="Exposure"
          hint={
            policy.is_published
              ? `Served at ${policy.panel_domain}`
              : "Loopback port only — reachable through an SSH tunnel"
          }
          flush
        >
          <div className="rows">
            <Row
              state={policy.is_published ? "active" : "healthy"}
              columns="minmax(0,1fr) auto"
            >
              <div>
                <div style={{ fontSize: "0.88rem", fontWeight: 500 }}>Public hostname</div>
                <div className="row__secondary">
                  {policy.is_published
                    ? "Anyone who can reach this name meets the controls below"
                    : "Not reachable from outside the machine"}
                </div>
              </div>
              {policy.is_published ? (
                <Tag tone="signal">{policy.panel_domain}</Tag>
              ) : (
                <Tag tone="hit">loopback only</Tag>
              )}
            </Row>

            <Row
              state={policy.normalised_allowlist.length ? "healthy" : "degraded"}
              columns="minmax(0,1fr) auto"
            >
              <div>
                <div style={{ fontSize: "0.88rem", fontWeight: 500 }}>Allowed addresses</div>
                <div className="row__secondary mono">
                  {policy.normalised_allowlist.length
                    ? policy.normalised_allowlist.join(", ")
                    : "any address"}
                </div>
              </div>
              {policy.normalised_allowlist.length ? (
                <Tag tone="hit">{policy.normalised_allowlist.length} allowed</Tag>
              ) : (
                <Tag tone="warn">unrestricted</Tag>
              )}
            </Row>

            <Row state={policy.require_mtls ? "healthy" : "idle"} columns="minmax(0,1fr) auto">
              <div>
                <div style={{ fontSize: "0.88rem", fontWeight: 500 }}>Client certificate</div>
                <div className="row__secondary">
                  {policy.require_mtls
                    ? "Refused during the handshake without one"
                    : "Not required — the sign-in form is reachable by anyone allowed above"}
                </div>
              </div>
              <Tag tone={policy.require_mtls ? "hit" : "neutral"}>
                {policy.require_mtls ? "required" : "off"}
              </Tag>
            </Row>

            <Row state={policy.require_totp ? "healthy" : "down"} columns="minmax(0,1fr) auto">
              <div>
                <div style={{ fontSize: "0.88rem", fontWeight: 500 }}>Second factor</div>
                <div className="row__secondary">
                  {summary.operators.with_second_factor} of {summary.operators.total}{" "}
                  operators enrolled
                </div>
              </div>
              <Tag tone={policy.require_totp ? "hit" : "down"}>
                {policy.require_totp ? "required" : "off"}
              </Tag>
            </Row>

            <Row
              state={policy.expose_django_admin ? "degraded" : "healthy"}
              columns="minmax(0,1fr) auto"
            >
              <div>
                <div style={{ fontSize: "0.88rem", fontWeight: 500 }}>Django admin</div>
                <div className="row__secondary">
                  {policy.expose_django_admin
                    ? "Reachable on the public hostname"
                    : "Blocked publicly; use the loopback port"}
                </div>
              </div>
              <Tag tone={policy.expose_django_admin ? "warn" : "hit"}>
                {policy.expose_django_admin ? "exposed" : "blocked"}
              </Tag>
            </Row>
          </div>
        </Panel>

        <Panel title="Last 24 hours" flush>
          <div className="rows">
            <Row state="healthy" columns="minmax(0,1fr) auto">
              <div style={{ fontSize: "0.88rem" }}>Successful sign-ins</div>
              <div className="row__num">{summary.last_24h.successful_sign_ins}</div>
            </Row>
            <Row
              state={summary.last_24h.failed_attempts > 20 ? "down" : "idle"}
              columns="minmax(0,1fr) auto"
            >
              <div>
                <div style={{ fontSize: "0.88rem" }}>Failed attempts</div>
                {summary.last_24h.failed_attempts > 20 && (
                  <div className="row__secondary">
                    Sustained failures usually mean someone is guessing
                  </div>
                )}
              </div>
              <div className="row__num">{summary.last_24h.failed_attempts}</div>
            </Row>
            <Row state="idle" columns="minmax(0,1fr) auto">
              <div style={{ fontSize: "0.88rem" }}>Distinct addresses</div>
              <div className="row__num">{summary.last_24h.distinct_addresses}</div>
            </Row>
            <Row state="idle" columns="minmax(0,1fr) auto">
              <div style={{ fontSize: "0.88rem" }}>Changes made</div>
              <div className="row__num">{summary.changes_last_24h}</div>
            </Row>
            <Row
              state={summary.operators.without_second_factor ? "degraded" : "healthy"}
              columns="minmax(0,1fr) auto"
            >
              <div style={{ fontSize: "0.88rem" }}>Operators without a second factor</div>
              <div className="row__num">{summary.operators.without_second_factor}</div>
            </Row>
          </div>
        </Panel>
      </div>

      <div className="grid grid--2" style={{ marginBottom: 16 }}>
        <Panel title="Operators" flush>
          <div className="rows">
            {(operators?.operators ?? []).map((operator) => (
              <Row
                key={operator.id}
                state={operator.has_second_factor ? "healthy" : "degraded"}
                columns="minmax(0,1fr) auto auto"
              >
                <div>
                  <div className="row__primary">{operator.username}</div>
                  <div className="row__secondary">
                    {operator.last_login
                      ? `last signed in ${relativeTime(operator.last_login)}`
                      : "never signed in"}
                    {operator.has_second_factor &&
                      ` · ${operator.recovery_codes_remaining} recovery codes left`}
                  </div>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {operator.is_superuser && <Tag tone="signal">superuser</Tag>}
                  <Tag tone={operator.has_second_factor ? "hit" : "warn"}>
                    {operator.has_second_factor ? "2FA on" : "no 2FA"}
                  </Tag>
                </div>
                <div className="row__actions">
                  {operator.has_second_factor && (
                    <button
                      className="btn btn--small"
                      onClick={() => resetFactor(operator)}
                      title="For a lost device. They will enrol again at next sign-in."
                    >
                      Reset 2FA
                    </button>
                  )}
                </div>
              </Row>
            ))}
          </div>
        </Panel>

        <Panel title="Recent sign-in attempts" flush>
          {(attempts ?? []).length === 0 ? (
            <Empty title="Nothing yet" body="Sign-in attempts appear here as they happen." />
          ) : (
            <div className="rows">
              {(attempts ?? []).map((attempt) => (
                <Row
                  key={attempt.id}
                  state={attempt.outcome === "success" ? "healthy" : "down"}
                  columns="minmax(0,1fr) auto auto"
                >
                  <div>
                    <div className="row__primary">{attempt.username}</div>
                    <div className="row__secondary mono">{attempt.ip_address ?? "unknown"}</div>
                  </div>
                  <Tag tone={attempt.outcome === "success" ? "hit" : "down"}>
                    {attempt.outcome_label}
                  </Tag>
                  <div
                    className="row__num"
                    style={{ fontSize: "0.78rem", color: "var(--text-faint)" }}
                  >
                    {relativeTime(attempt.created_at)}
                  </div>
                </Row>
              ))}
            </div>
          )}
        </Panel>
      </div>

      <Panel
        title="Audit trail"
        hint="Every change, and who made it. Append-only — nothing here can be edited or removed."
        flush
      >
        {(audit ?? []).length === 0 ? (
          <Empty title="No changes recorded yet" body="Anything that alters the gateway appears here." />
        ) : (
          <div className="rows">
            {(audit ?? []).map((event) => (
              <Row
                key={event.id}
                state={event.status_code < 400 ? "active" : "down"}
                columns="minmax(0,1fr) auto auto auto"
              >
                <div>
                  <div style={{ fontSize: "0.88rem", fontWeight: 500 }}>
                    {event.summary || `${event.action} ${event.path}`}
                  </div>
                  <div className="row__secondary mono">
                    {event.actor_name || "anonymous"} · {event.path}
                  </div>
                </div>
                <div className="row__num">{event.status_code}</div>
                <div className="row__num mono" style={{ fontSize: "0.78rem" }}>
                  {event.ip_address ?? "—"}
                </div>
                <div
                  className="row__num"
                  style={{ fontSize: "0.78rem", color: "var(--text-faint)" }}
                  title={dateTime(event.created_at)}
                >
                  {relativeTime(event.created_at)}
                </div>
              </Row>
            ))}
          </div>
        )}
      </Panel>

      {publishing && domains && (
        <PublishForm
          policy={policy}
          domains={domains}
          onClose={() => setPublishing(false)}
          onSaved={reloadAll}
        />
      )}

      {hardening && (
        <HardeningForm
          policy={policy}
          onClose={() => setHardening(false)}
          onSaved={reloadAll}
        />
      )}
    </>
  );
}
