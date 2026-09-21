import { FormEvent, useMemo, useState } from "react";
import { api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { Backend, Domain, RoutingRule } from "../lib/types";
import { Check, Empty, Field, Modal, Notice, Panel, Row, Tag, useToast } from "../components/ui";

const MATCH_TYPES = [
  {
    value: "path_prefix",
    label: "Path starts with",
    note: "Matched against a compiled tree, so it costs effectively nothing per request.",
  },
  {
    value: "exact_path",
    label: "Path is exactly",
    note: "The fastest match there is. Use it for single endpoints like /health.",
  },
  {
    value: "regex",
    label: "Path matches pattern",
    note: "Patterns are tested one by one, in priority order, on every request. Reach for a prefix first.",
  },
] as const;

interface DraftRule {
  domain_id: number;
  backend_id: number;
  match_type: string;
  match_value: string;
  priority: number;
  cache_enabled: boolean;
  cache_ttl: number;
  cache_bypass_auth: boolean;
  cache_allow_authenticated: boolean;
  cache_ignore_upstream_control: boolean;
  cache_key_headers: string[];
  cache_min_uses: number;
  strip_prefix: boolean;
  proxy_buffering: boolean;
  proxy_read_timeout: number;
  rate_limit_enabled: boolean;
  rate_limit_rps: number;
  rate_limit_burst: number;
  is_active: boolean;
}

function blankRule(domainId: number, backendId: number): DraftRule {
  return {
    domain_id: domainId,
    backend_id: backendId,
    match_type: "path_prefix",
    match_value: "/",
    priority: 100,
    cache_enabled: false,
    cache_ttl: 600,
    cache_bypass_auth: true,
    cache_allow_authenticated: false,
    cache_ignore_upstream_control: false,
    cache_key_headers: ["Accept-Encoding"],
    cache_min_uses: 1,
    strip_prefix: false,
    proxy_buffering: true,
    proxy_read_timeout: 60,
    rate_limit_enabled: false,
    rate_limit_rps: 100,
    rate_limit_burst: 200,
    is_active: true,
  };
}

function RuleForm({
  initial,
  existing,
  domains,
  backends,
  onClose,
  onSaved,
}: {
  initial: DraftRule;
  existing: RoutingRule | null;
  domains: Domain[];
  backends: Backend[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const toast = useToast();

  const [save, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    if (existing) {
      await api.patch(`/api/v1/routing/rules/${existing.id}/`, draft);
      toast("Route saved. Deploying.");
    } else {
      await api.post("/api/v1/routing/rules/", draft);
      toast("Route added. Deploying.");
    }
    onSaved();
    onClose();
  });

  const matchType = MATCH_TYPES.find((m) => m.value === draft.match_type);
  const domain = domains.find((d) => d.id === draft.domain_id);

  // The one cache configuration that leaks data between users. The server
  // refuses it too; saying so here means nobody has to discover it by failing.
  const cachingAuthenticated =
    draft.cache_enabled && !draft.cache_bypass_auth && !draft.cache_allow_authenticated;

  return (
    <Modal
      title={existing ? "Edit route" : "Add a route"}
      subtitle="A route decides which backend answers a request. Rules are checked from lowest priority number upward."
      wide
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn--primary"
            form="rule-form"
            disabled={busy || cachingAuthenticated || !draft.match_value}
          >
            {busy ? "Saving…" : existing ? "Save route" : "Add route"}
          </button>
        </>
      }
    >
      <form id="rule-form" onSubmit={save}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <div className="row-2">
          <Field label="On hostname">
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

          <Field label="Send to">
            <select
              className="select"
              value={draft.backend_id}
              onChange={(e) => setDraft({ ...draft, backend_id: Number(e.target.value) })}
            >
              {backends.map((b) => (
                <option key={b.id} value={b.id}>
                  {b.name}
                  {b.total_instance_count === 0 ? " (no instances)" : ""}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <div className="row-2">
          <Field label="When the request" help={matchType?.note}>
            <select
              className="select"
              value={draft.match_type}
              onChange={(e) => setDraft({ ...draft, match_type: e.target.value })}
            >
              {MATCH_TYPES.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </Field>

          <Field
            label={draft.match_type === "regex" ? "Pattern" : "Path"}
            error={error?.fields.match_value?.[0]}
          >
            <input
              className="input input--mono"
              value={draft.match_value}
              placeholder={draft.match_type === "regex" ? "^/v[0-9]+/" : "/api/"}
              onChange={(e) => setDraft({ ...draft, match_value: e.target.value })}
              required
            />
          </Field>
        </div>

        <div className="row-2">
          <Field
            label="Priority"
            help="Lower numbers are checked first. Give specific paths a lower number than catch-alls."
          >
            <input
              className="input"
              type="number"
              value={draft.priority}
              onChange={(e) => setDraft({ ...draft, priority: Number(e.target.value) })}
            />
          </Field>

          <Field
            label={
              draft.proxy_buffering ? "Wait up to (seconds)" : "Close after idle (seconds)"
            }
            help={
              draft.proxy_buffering
                ? "How long to wait for the backend to respond."
                : "How long the open connection may go quiet before it is closed. A WebSocket with a slower heartbeat than this will be dropped."
            }
          >
            <input
              className="input"
              type="number"
              min={1}
              value={draft.proxy_read_timeout}
              onChange={(e) => setDraft({ ...draft, proxy_read_timeout: Number(e.target.value) })}
            />
          </Field>
        </div>

        {draft.match_type === "path_prefix" && draft.match_value !== "/" && (
          <Check
            label={`Remove ${draft.match_value} before forwarding`}
            help={`The backend receives /rest instead of ${draft.match_value}rest.`}
            checked={draft.strip_prefix}
            onChange={(v) => setDraft({ ...draft, strip_prefix: v })}
          />
        )}

        <h3 style={{ fontSize: "0.86rem", margin: "22px 0 10px", color: "var(--text-dim)" }}>
          Caching
        </h3>

        <Check
          label="Cache responses at the gateway"
          help="Only GET and HEAD are ever cached. Whatever the backend says in Cache-Control, Expires or ETag still takes precedence over the settings here."
          checked={draft.cache_enabled}
          onChange={(v) => setDraft({ ...draft, cache_enabled: v })}
        />

        {draft.cache_enabled && (
          <>
            <div className="row-2">
              <Field
                label="Keep for (seconds)"
                help="Used only when the backend sends no caching headers of its own."
              >
                <input
                  className="input"
                  type="number"
                  min={0}
                  value={draft.cache_ttl}
                  onChange={(e) => setDraft({ ...draft, cache_ttl: Number(e.target.value) })}
                />
              </Field>

              <Field
                label="Store after this many requests"
                help="Raise it above 1 so one-off URLs do not fill the cache."
              >
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={draft.cache_min_uses}
                  onChange={(e) => setDraft({ ...draft, cache_min_uses: Number(e.target.value) })}
                />
              </Field>
            </div>

            <Check
              label="Skip the cache for signed-in requests"
              help="Requests carrying an Authorization header or a session cookie go straight to the backend, and their responses are never stored."
              checked={draft.cache_bypass_auth}
              onChange={(v) =>
                setDraft({
                  ...draft,
                  cache_bypass_auth: v,
                  cache_allow_authenticated: v ? false : draft.cache_allow_authenticated,
                })
              }
            />

            {!draft.cache_bypass_auth && (
              <Notice tone="error">
                <span>
                  <strong>This caches signed-in responses.</strong> One user's data will be
                  served to the next person who asks for the same URL. Only correct if every
                  response on this path is identical for every caller.
                </span>
              </Notice>
            )}

            {!draft.cache_bypass_auth && (
              <Check
                label="I understand, cache them anyway"
                checked={draft.cache_allow_authenticated}
                onChange={(v) => setDraft({ ...draft, cache_allow_authenticated: v })}
              />
            )}

            <Check
              label="Cache even when the backend says not to"
              help="Ignores Cache-Control: no-cache from the backend. It overrides the backend's own judgement, so leave it off unless you know why you are turning it on."
              checked={draft.cache_ignore_upstream_control}
              onChange={(v) => setDraft({ ...draft, cache_ignore_upstream_control: v })}
            />

            <Field
              label="Also vary the cache by these headers"
              help="One entry per line. Accept-Encoding is always included. Any header this route switches on is added automatically."
            >
              <textarea
                className="textarea textarea--mono"
                style={{ minHeight: 72 }}
                value={draft.cache_key_headers.join("\n")}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    cache_key_headers: e.target.value.split("\n").filter((h) => h.trim()),
                  })
                }
              />
            </Field>
          </>
        )}

        <h3 style={{ fontSize: "0.86rem", margin: "22px 0 10px", color: "var(--text-dim)" }}>
          Limits and streaming
        </h3>

        <Check
          label="Limit requests per client"
          checked={draft.rate_limit_enabled}
          onChange={(v) => setDraft({ ...draft, rate_limit_enabled: v })}
        />

        {draft.rate_limit_enabled && (
          <div className="row-2">
            <Field label="Requests per second" help="Per client IP address.">
              <input
                className="input"
                type="number"
                min={1}
                value={draft.rate_limit_rps}
                onChange={(e) => setDraft({ ...draft, rate_limit_rps: Number(e.target.value) })}
              />
            </Field>
            <Field
              label="Allow a burst of"
              help="How many requests may arrive above the rate before any are refused with a 429."
            >
              <input
                className="input"
                type="number"
                min={0}
                value={draft.rate_limit_burst}
                onChange={(e) => setDraft({ ...draft, rate_limit_burst: Number(e.target.value) })}
              />
            </Field>
          </div>
        )}

        <Check
          label="Stream the response"
          help="Turn this on for WebSocket, server-sent events or long downloads. Responses pass straight through, are never cached, and the upgrade handshake is forwarded."
          checked={!draft.proxy_buffering}
          onChange={(v) =>
            setDraft({
              ...draft,
              proxy_buffering: !v,
              cache_enabled: v ? false : draft.cache_enabled,
              // The timeout changes meaning with the mode, so it moves with it.
              // Leaving the request-response default would close idle sockets
              // after a minute, which is the usual way WebSocket support
              // appears to work and then does not.
              proxy_read_timeout:
                v && draft.proxy_read_timeout === 60
                  ? 3600
                  : !v && draft.proxy_read_timeout === 3600
                    ? 60
                    : draft.proxy_read_timeout,
            })
          }
        />

        {!draft.proxy_buffering && (
          <Notice>
            <span>
              The connection is closed after{" "}
              <strong>{draft.proxy_read_timeout} seconds</strong> of silence. Set this
              longer than your application's heartbeat, or the socket will drop between
              beats and reconnect.
            </span>
          </Notice>
        )}

        <Check
          label="Route is live"
          checked={draft.is_active}
          onChange={(v) => setDraft({ ...draft, is_active: v })}
        />

        {domain && !domain.is_active && (
          <Notice tone="warn">
            <span>
              <span className="mono">{domain.name}</span> is paused, so this route will not
              take traffic until the hostname is accepting again.
            </span>
          </Notice>
        )}
      </form>
    </Modal>
  );
}

function HeaderRouteForm({
  rule,
  backends,
  onClose,
  onSaved,
}: {
  rule: RoutingRule;
  backends: Backend[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState({
    backend_id: backends[0]?.id ?? 0,
    header_name: "X-Route",
    header_value: "",
    description: "",
    is_active: true,
  });
  const toast = useToast();

  const [save, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    await api.post(`/api/v1/routing/rules/${rule.id}/header-routes/`, draft);
    toast(`Requests with ${draft.header_name}: ${draft.header_value} now go elsewhere.`);
    onSaved();
    onClose();
  });

  return (
    <Modal
      title="Send one header value elsewhere"
      subtitle={`Requests to ${rule.domain_name}${rule.match_value} normally reach ${rule.backend_name}. This sends the ones carrying a particular header value to a different backend instead.`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn--primary"
            form="header-route-form"
            disabled={busy || !draft.header_value}
          >
            {busy ? "Saving…" : "Add override"}
          </button>
        </>
      }
    >
      <form id="header-route-form" onSubmit={save}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <div className="row-2">
          <Field label="Header" help="Case does not matter.">
            <input
              className="input input--mono"
              value={draft.header_name}
              onChange={(e) => setDraft({ ...draft, header_name: e.target.value })}
            />
          </Field>
          <Field label="Value">
            <input
              className="input input--mono"
              value={draft.header_value}
              placeholder="canary"
              onChange={(e) => setDraft({ ...draft, header_value: e.target.value })}
              autoFocus
              required
            />
          </Field>
        </div>

        <Field label="Send those to">
          <select
            className="select"
            value={draft.backend_id}
            onChange={(e) => setDraft({ ...draft, backend_id: Number(e.target.value) })}
          >
            {backends.map((b) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </Field>

        <Field label="Notes" help="What this override is for.">
          <input
            className="input"
            value={draft.description}
            placeholder="Opt-in canary pool"
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          />
        </Field>

        {rule.cache_enabled && (
          <Notice>
            <span>
              This route caches, so <span className="mono">{draft.header_name}</span> is added
              to the cache key automatically. Without that, the override's response would be
              served to everyone.
            </span>
          </Notice>
        )}
      </form>
    </Modal>
  );
}

export function RoutesPage() {
  const { data: rules, initialLoading, reload } = useResource<RoutingRule[]>(
    "/api/v1/routing/rules/",
    15000,
  );
  const { data: domains } = useResource<Domain[]>("/api/v1/domains/");
  const { data: backends } = useResource<Backend[]>("/api/v1/backends/");

  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<RoutingRule | null>(null);
  const [headerFor, setHeaderFor] = useState<RoutingRule | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<RoutingRule | null>(null);
  const toast = useToast();

  const [remove, { busy: removing }] = useAction(async (rule: RoutingRule) => {
    await api.delete(`/api/v1/routing/rules/${rule.id}/`);
    toast("Route deleted. Deploying.");
    setConfirmDelete(null);
    reload();
  });

  const [removeHeaderRoute] = useAction(async (id: number) => {
    await api.delete(`/api/v1/routing/header-routes/${id}/`);
    toast("Override removed.");
    reload();
  });

  const byDomain = useMemo(() => {
    const groups = new Map<string, RoutingRule[]>();
    for (const rule of rules ?? []) {
      const list = groups.get(rule.domain_name) ?? [];
      list.push(rule);
      groups.set(rule.domain_name, list);
    }
    for (const list of groups.values()) list.sort((a, b) => a.priority - b.priority);
    return [...groups.entries()].sort(([a], [b]) => a.localeCompare(b));
  }, [rules]);

  if (initialLoading) return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;

  const canAdd = (domains?.length ?? 0) > 0 && (backends?.length ?? 0) > 0;

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Routes</h1>
          <p>
            Which requests reach which backend. Within each hostname, rules are checked from
            the lowest priority number upward, and the first match wins.
          </p>
        </div>
        <div className="page-actions">
          <button className="btn btn--primary" onClick={() => setAdding(true)} disabled={!canAdd}>
            Add route
          </button>
        </div>
      </header>

      {!canAdd && (
        <Notice tone="warn">
          <span>
            A route needs a hostname to match and a backend to send to. Add{" "}
            {(domains?.length ?? 0) === 0 && "a domain"}
            {(domains?.length ?? 0) === 0 && (backends?.length ?? 0) === 0 && " and "}
            {(backends?.length ?? 0) === 0 && "a backend"} first.
          </span>
        </Notice>
      )}

      {byDomain.length === 0 ? (
        <Panel title="Routes" flush>
          <Empty
            title="No routes yet"
            body="Connect a hostname to a backend and the gateway starts forwarding. Until then every request is answered with a 404."
            action={
              canAdd ? (
                <button className="btn btn--primary" onClick={() => setAdding(true)}>
                  Add route
                </button>
              ) : undefined
            }
          />
        </Panel>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {byDomain.map(([domainName, domainRules]) => (
            <Panel
              key={domainName}
              title={domainName}
              hint={`${domainRules.length} ${domainRules.length === 1 ? "route" : "routes"}, checked in this order`}
              flush
            >
              <div className="rows">
                {domainRules.map((rule) => (
                  <div key={rule.id}>
                    <Row
                      state={rule.is_active ? "active" : "idle"}
                      columns="auto minmax(0,1fr) auto auto"
                    >
                      <div
                        className="row__num"
                        style={{ color: "var(--text-faint)", minWidth: 32 }}
                        title="Priority"
                      >
                        {rule.priority}
                      </div>

                      <div>
                        <div className="row__primary">
                          {rule.match_type === "exact_path" && "= "}
                          {rule.match_type === "regex" && "~ "}
                          {rule.match_value}
                          <span style={{ color: "var(--text-faint)" }}> → </span>
                          {rule.backend_name}
                        </div>
                        <div className="row__secondary">
                          {rule.match_type_label}
                          {rule.strip_prefix && " · prefix stripped"}
                          {!rule.proxy_buffering && " · streamed"}
                        </div>
                      </div>

                      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
                        {!rule.is_active && <Tag>paused</Tag>}
                        {rule.cache_enabled && (
                          <Tag tone="hit">
                            cached {rule.cache_ttl}s
                            {!rule.cache_bypass_auth && " · signed-in too"}
                          </Tag>
                        )}
                        {rule.rate_limit_enabled && (
                          <Tag tone="warn">{rule.rate_limit_rps}/s</Tag>
                        )}
                        {rule.match_type === "regex" && <Tag tone="warn">pattern</Tag>}
                      </div>

                      <div className="row__actions">
                        <button
                          className="btn btn--small"
                          onClick={() => setHeaderFor(rule)}
                          disabled={(backends?.length ?? 0) === 0}
                        >
                          Header override
                        </button>
                        <button className="btn btn--small" onClick={() => setEditing(rule)}>
                          Edit
                        </button>
                        <button
                          className="btn btn--small btn--danger"
                          onClick={() => setConfirmDelete(rule)}
                        >
                          Delete
                        </button>
                      </div>
                    </Row>

                    {rule.header_routes.length > 0 && (
                      <div style={{ paddingLeft: 28 }}>
                        {rule.header_routes.map((override) => (
                          <Row
                            key={override.id}
                            state="active"
                            columns="minmax(0,1fr) auto auto"
                          >
                            <div>
                              <div className="row__primary" style={{ fontSize: "0.82rem" }}>
                                {override.header_name}: {override.header_value}
                                <span style={{ color: "var(--text-faint)" }}> → </span>
                                {override.backend_name}
                              </div>
                              {override.description && (
                                <div className="row__secondary">{override.description}</div>
                              )}
                            </div>
                            <Tag tone="cool">override</Tag>
                            <div className="row__actions">
                              <button
                                className="btn btn--small btn--danger"
                                onClick={() => removeHeaderRoute(override.id)}
                              >
                                Remove
                              </button>
                            </div>
                          </Row>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </Panel>
          ))}
        </div>
      )}

      {adding && domains && backends && (
        <RuleForm
          initial={blankRule(domains[0].id, backends[0].id)}
          existing={null}
          domains={domains}
          backends={backends}
          onClose={() => setAdding(false)}
          onSaved={reload}
        />
      )}

      {editing && domains && backends && (
        <RuleForm
          initial={{
            domain_id: editing.domain,
            backend_id: editing.backend,
            match_type: editing.match_type,
            match_value: editing.match_value,
            priority: editing.priority,
            cache_enabled: editing.cache_enabled,
            cache_ttl: editing.cache_ttl,
            cache_bypass_auth: editing.cache_bypass_auth,
            cache_allow_authenticated: editing.cache_allow_authenticated,
            cache_ignore_upstream_control: editing.cache_ignore_upstream_control,
            cache_key_headers: editing.cache_key_headers,
            cache_min_uses: editing.cache_min_uses,
            strip_prefix: editing.strip_prefix,
            proxy_buffering: editing.proxy_buffering,
            proxy_read_timeout: editing.proxy_read_timeout,
            rate_limit_enabled: editing.rate_limit_enabled,
            rate_limit_rps: editing.rate_limit_rps,
            rate_limit_burst: editing.rate_limit_burst,
            is_active: editing.is_active,
          }}
          existing={editing}
          domains={domains}
          backends={backends}
          onClose={() => setEditing(null)}
          onSaved={reload}
        />
      )}

      {headerFor && backends && (
        <HeaderRouteForm
          rule={headerFor}
          backends={backends}
          onClose={() => setHeaderFor(null)}
          onSaved={reload}
        />
      )}

      {confirmDelete && (
        <Modal
          title="Delete this route?"
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
                {removing ? "Deleting…" : "Delete route"}
              </button>
            </>
          }
        >
          <Notice tone="warn">
            <span>
              Requests to <span className="mono">{confirmDelete.domain_name}
              {confirmDelete.match_value}</span> will fall through to a lower-priority rule,
              or be answered with a 404 if none matches.
            </span>
          </Notice>
        </Modal>
      )}
    </>
  );
}
