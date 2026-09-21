import { FormEvent, useState } from "react";
import { api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { Backend, BackendInstance } from "../lib/types";
import { ms, relativeTime } from "../lib/format";
import { Check, Empty, Field, Modal, Notice, Panel, Row, Tag, useToast } from "../components/ui";

const LB_METHODS = [
  {
    value: "round_robin",
    label: "Round robin",
    note: "Each instance takes the next request in turn. Right when instances are alike and requests cost about the same.",
  },
  {
    value: "least_conn",
    label: "Least connections",
    note: "Sends each request to whichever instance is busiest with fewest. Right when request durations vary a lot.",
  },
  {
    value: "ip_hash",
    label: "Client IP",
    note: "The same client always reaches the same instance. Only pick this if the backend keeps per-client state it cannot share.",
  },
] as const;

interface DraftBackend {
  name: string;
  description: string;
  lb_method: string;
  keepalive_connections: number;
  keepalive_requests: number;
  health_check_enabled: boolean;
  health_check_path: string;
  health_check_timeout: number;
  drain_unhealthy: boolean;
  unhealthy_threshold: number;
  healthy_threshold: number;
  is_active: boolean;
}

const BLANK: DraftBackend = {
  name: "",
  description: "",
  lb_method: "round_robin",
  keepalive_connections: 32,
  keepalive_requests: 1000,
  health_check_enabled: true,
  health_check_path: "/health",
  health_check_timeout: 5,
  drain_unhealthy: false,
  unhealthy_threshold: 3,
  healthy_threshold: 2,
  is_active: true,
};

function BackendForm({
  initial,
  existing,
  onClose,
  onSaved,
}: {
  initial: DraftBackend;
  existing: Backend | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const toast = useToast();

  const [save, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    if (existing) {
      await api.patch(`/api/v1/backends/${existing.id}/`, draft);
      toast(`Saved ${draft.name}.`);
    } else {
      await api.post("/api/v1/backends/", draft);
      toast(`Added ${draft.name}. Add an instance to start sending it traffic.`);
    }
    onSaved();
    onClose();
  });

  const method = LB_METHODS.find((m) => m.value === draft.lb_method);

  return (
    <Modal
      title={existing ? `Edit ${existing.name}` : "Add a backend"}
      subtitle="A backend is one service. Its instances are the copies of that service traffic is spread across."
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn--primary" form="backend-form" disabled={busy || !draft.name}>
            {busy ? "Saving…" : existing ? "Save changes" : "Add backend"}
          </button>
        </>
      }
    >
      <form id="backend-form" onSubmit={save}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <div className="row-2">
          <Field label="Name" help="How you will refer to this service." error={error?.fields.name?.[0]}>
            <input
              className="input input--mono"
              value={draft.name}
              placeholder="user-service"
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              autoFocus
              required
            />
          </Field>

          <Field label="Spread traffic by" help={method?.note}>
            <select
              className="select"
              value={draft.lb_method}
              onChange={(e) => setDraft({ ...draft, lb_method: e.target.value })}
            >
              {LB_METHODS.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.label}
                </option>
              ))}
            </select>
          </Field>
        </div>

        <Field label="Notes">
          <input
            className="input"
            value={draft.description}
            onChange={(e) => setDraft({ ...draft, description: e.target.value })}
          />
        </Field>

        <h3 style={{ fontSize: "0.86rem", margin: "20px 0 10px", color: "var(--text-dim)" }}>
          Connection reuse
        </h3>

        <div className="row-2">
          <Field
            label="Idle connections per worker"
            help="Set this to the peak number of requests one worker has in flight to this backend. Too low and most requests pay for a fresh TCP handshake."
          >
            <input
              className="input"
              type="number"
              min={1}
              value={draft.keepalive_connections}
              onChange={(e) =>
                setDraft({ ...draft, keepalive_connections: Number(e.target.value) })
              }
            />
          </Field>

          <Field
            label="Requests per connection"
            help="How many requests reuse one connection before it is reopened."
          >
            <input
              className="input"
              type="number"
              min={1}
              value={draft.keepalive_requests}
              onChange={(e) => setDraft({ ...draft, keepalive_requests: Number(e.target.value) })}
            />
          </Field>
        </div>

        <h3 style={{ fontSize: "0.86rem", margin: "20px 0 10px", color: "var(--text-dim)" }}>
          Health checks
        </h3>

        <Notice>
          <span>
            Nginx already fails over to another instance the moment one stops responding, per
            request and in real time. These probes are for telling you what is happening —
            they only steer traffic if you turn on draining below.
          </span>
        </Notice>

        <Check
          label="Probe instances"
          help="Sends a request to each instance on a schedule and records the result."
          checked={draft.health_check_enabled}
          onChange={(v) => setDraft({ ...draft, health_check_enabled: v })}
        />

        {draft.health_check_enabled && (
          <>
            <div className="row-2">
              <Field label="Probe path">
                <input
                  className="input input--mono"
                  value={draft.health_check_path}
                  onChange={(e) => setDraft({ ...draft, health_check_path: e.target.value })}
                />
              </Field>
              <Field label="Give up after (seconds)">
                <input
                  className="input"
                  type="number"
                  min={1}
                  value={draft.health_check_timeout}
                  onChange={(e) =>
                    setDraft({ ...draft, health_check_timeout: Number(e.target.value) })
                  }
                />
              </Field>
            </div>

            <Check
              label="Take failing instances out of rotation"
              help="Rewrites the gateway config to mark the instance down. Each change costs a reload, so it waits for several failures in a row before acting."
              checked={draft.drain_unhealthy}
              onChange={(v) => setDraft({ ...draft, drain_unhealthy: v })}
            />

            {draft.drain_unhealthy && (
              <div className="row-2">
                <Field label="Failures before removing">
                  <input
                    className="input"
                    type="number"
                    min={1}
                    value={draft.unhealthy_threshold}
                    onChange={(e) =>
                      setDraft({ ...draft, unhealthy_threshold: Number(e.target.value) })
                    }
                  />
                </Field>
                <Field label="Successes before restoring">
                  <input
                    className="input"
                    type="number"
                    min={1}
                    value={draft.healthy_threshold}
                    onChange={(e) =>
                      setDraft({ ...draft, healthy_threshold: Number(e.target.value) })
                    }
                  />
                </Field>
              </div>
            )}
          </>
        )}

        <Check
          label="Accept traffic"
          checked={draft.is_active}
          onChange={(v) => setDraft({ ...draft, is_active: v })}
        />
      </form>
    </Modal>
  );
}

function InstanceForm({
  backend,
  existing,
  onClose,
  onSaved,
}: {
  backend: Backend;
  existing: BackendInstance | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [draft, setDraft] = useState({
    address: existing?.address ?? "",
    port: existing?.port ?? 8080,
    weight: existing?.weight ?? 1,
    max_fails: existing?.max_fails ?? 3,
    fail_timeout: existing?.fail_timeout ?? 10,
    is_active: existing?.is_active ?? true,
  });
  const toast = useToast();

  const [save, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    const base = `/api/v1/backends/${backend.id}/instances/`;
    if (existing) {
      await api.patch(`${base}${existing.id}/`, draft);
      toast(`Saved ${draft.address}:${draft.port}.`);
    } else {
      await api.post(base, draft);
      toast(`Added ${draft.address}:${draft.port} to ${backend.name}.`);
    }
    onSaved();
    onClose();
  });

  return (
    <Modal
      title={existing ? "Edit instance" : `Add an instance to ${backend.name}`}
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Cancel
          </button>
          <button
            className="btn btn--primary"
            form="instance-form"
            disabled={busy || !draft.address}
          >
            {busy ? "Saving…" : existing ? "Save changes" : "Add instance"}
          </button>
        </>
      }
    >
      <form id="instance-form" onSubmit={save}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <div className="row-2">
          <Field label="Address" help="IP address on the local network." error={error?.fields.address?.[0]}>
            <input
              className="input input--mono"
              value={draft.address}
              placeholder="10.0.1.11"
              onChange={(e) => setDraft({ ...draft, address: e.target.value })}
              autoFocus
              required
            />
          </Field>
          <Field label="Port">
            <input
              className="input input--mono"
              type="number"
              min={1}
              max={65535}
              value={draft.port}
              onChange={(e) => setDraft({ ...draft, port: Number(e.target.value) })}
            />
          </Field>
        </div>

        <div className="row-2">
          <Field
            label="Weight"
            help="Relative share of traffic. Raise it for an instance with more capacity."
          >
            <input
              className="input"
              type="number"
              min={1}
              value={draft.weight}
              onChange={(e) => setDraft({ ...draft, weight: Number(e.target.value) })}
            />
          </Field>
          <Field
            label="Failures before skipping"
            help="How many failed requests in a row before Nginx stops sending here temporarily."
          >
            <input
              className="input"
              type="number"
              min={0}
              value={draft.max_fails}
              onChange={(e) => setDraft({ ...draft, max_fails: Number(e.target.value) })}
            />
          </Field>
        </div>

        <Check
          label="Accept traffic"
          help="Turn this off to drain the instance for maintenance without deleting it."
          checked={draft.is_active}
          onChange={(v) => setDraft({ ...draft, is_active: v })}
        />
      </form>
    </Modal>
  );
}

export function Backends() {
  const { data, initialLoading, reload } = useResource<Backend[]>("/api/v1/backends/", 10000);
  const [addingBackend, setAddingBackend] = useState(false);
  const [editingBackend, setEditingBackend] = useState<Backend | null>(null);
  const [instanceFor, setInstanceFor] = useState<{
    backend: Backend;
    instance: BackendInstance | null;
  } | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Backend | null>(null);
  const toast = useToast();

  const [probe] = useAction(async (backend: Backend) => {
    await api.post(`/api/v1/backends/${backend.id}/health-check/`);
    toast(`Probing ${backend.name}.`);
    window.setTimeout(reload, 2500);
  });

  const [removeBackend, { busy: removing }] = useAction(async (backend: Backend) => {
    await api.delete(`/api/v1/backends/${backend.id}/`);
    toast(`Removed ${backend.name}.`);
    setConfirmDelete(null);
    reload();
  });

  const [removeInstance] = useAction(
    async (backend: Backend, instance: BackendInstance) => {
      await api.delete(`/api/v1/backends/${backend.id}/instances/${instance.id}/`);
      toast(`Removed ${instance.address}:${instance.port}.`);
      reload();
    },
  );

  if (initialLoading) return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;

  const backends = data ?? [];

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Backends</h1>
          <p>
            The services behind the gateway. Each one holds the instances traffic is spread
            across — which is a separate decision from which requests reach it at all.
          </p>
        </div>
        <div className="page-actions">
          <button className="btn btn--primary" onClick={() => setAddingBackend(true)}>
            Add backend
          </button>
        </div>
      </header>

      {backends.length === 0 ? (
        <Panel title="Services" flush>
          <Empty
            title="No backends yet"
            body="A backend is one service — an API, a static host, whatever sits behind the gateway. Add one, then give it the addresses of the machines running it."
            action={
              <button className="btn btn--primary" onClick={() => setAddingBackend(true)}>
                Add backend
              </button>
            }
          />
        </Panel>
      ) : (
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          {backends.map((backend) => {
            const status =
              backend.total_instance_count === 0
                ? "idle"
                : backend.healthy_instance_count === backend.total_instance_count
                  ? "healthy"
                  : backend.healthy_instance_count > 0
                    ? "degraded"
                    : "down";

            return (
              <Panel
                key={backend.id}
                title={backend.name}
                hint={
                  <>
                    {backend.lb_method_label} · keepalive {backend.keepalive_connections}
                    {backend.description && ` · ${backend.description}`}
                  </>
                }
                actions={
                  <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                    {!backend.is_active && <Tag>paused</Tag>}
                    <Tag
                      tone={
                        status === "healthy"
                          ? "hit"
                          : status === "degraded"
                            ? "warn"
                            : status === "down"
                              ? "down"
                              : "neutral"
                      }
                    >
                      {backend.total_instance_count === 0
                        ? "no instances"
                        : `${backend.healthy_instance_count} of ${backend.total_instance_count} up`}
                    </Tag>
                    <button className="btn btn--small" onClick={() => probe(backend)}>
                      Probe now
                    </button>
                    <button
                      className="btn btn--small"
                      onClick={() => setInstanceFor({ backend, instance: null })}
                    >
                      Add instance
                    </button>
                    <button className="btn btn--small" onClick={() => setEditingBackend(backend)}>
                      Edit
                    </button>
                    <button
                      className="btn btn--small btn--danger"
                      onClick={() => setConfirmDelete(backend)}
                    >
                      Delete
                    </button>
                  </div>
                }
                flush
              >
                {backend.instances.length === 0 ? (
                  <Empty
                    title="No instances"
                    body="Routes pointing at this backend are skipped until it has somewhere to send traffic."
                    action={
                      <button
                        className="btn btn--primary"
                        onClick={() => setInstanceFor({ backend, instance: null })}
                      >
                        Add instance
                      </button>
                    }
                  />
                ) : (
                  <div className="rows">
                    {backend.instances.map((instance) => (
                      <Row
                        key={instance.id}
                        state={
                          instance.state === "healthy"
                            ? "healthy"
                            : instance.state === "draining"
                              ? "degraded"
                              : instance.state === "failing"
                                ? "down"
                                : "idle"
                        }
                        columns="minmax(0,1fr) auto auto auto auto"
                      >
                        <div>
                          <div className="row__primary">
                            {instance.address}:{instance.port}
                          </div>
                          <div className="row__secondary">
                            {instance.state === "healthy" && "answering probes"}
                            {instance.state === "failing" &&
                              `${instance.consecutive_failures} failed ${
                                instance.consecutive_failures === 1 ? "probe" : "probes"
                              } in a row`}
                            {instance.state === "draining" && "taken out of rotation"}
                            {instance.state === "disabled" && "paused by an operator"}
                            {instance.weight !== 1 && ` · weight ${instance.weight}`}
                          </div>
                        </div>

                        <div className="row__num">
                          {instance.last_health_status_code ?? "—"}
                        </div>
                        <div className="row__num">{ms(instance.last_health_response_ms, 1)}</div>
                        <div
                          className="row__num"
                          style={{ color: "var(--text-faint)", fontSize: "0.78rem" }}
                        >
                          {relativeTime(instance.last_health_check)}
                        </div>

                        <div className="row__actions">
                          <button
                            className="btn btn--small"
                            onClick={() => setInstanceFor({ backend, instance })}
                          >
                            Edit
                          </button>
                          <button
                            className="btn btn--small btn--danger"
                            onClick={() => removeInstance(backend, instance)}
                          >
                            Remove
                          </button>
                        </div>
                      </Row>
                    ))}
                  </div>
                )}
              </Panel>
            );
          })}
        </div>
      )}

      {addingBackend && (
        <BackendForm
          initial={BLANK}
          existing={null}
          onClose={() => setAddingBackend(false)}
          onSaved={reload}
        />
      )}

      {editingBackend && (
        <BackendForm
          initial={{
            name: editingBackend.name,
            description: editingBackend.description,
            lb_method: editingBackend.lb_method,
            keepalive_connections: editingBackend.keepalive_connections,
            keepalive_requests: editingBackend.keepalive_requests,
            health_check_enabled: editingBackend.health_check_enabled,
            health_check_path: editingBackend.health_check_path,
            health_check_timeout: editingBackend.health_check_timeout,
            drain_unhealthy: editingBackend.drain_unhealthy,
            unhealthy_threshold: editingBackend.unhealthy_threshold,
            healthy_threshold: editingBackend.healthy_threshold,
            is_active: editingBackend.is_active,
          }}
          existing={editingBackend}
          onClose={() => setEditingBackend(null)}
          onSaved={reload}
        />
      )}

      {instanceFor && (
        <InstanceForm
          backend={instanceFor.backend}
          existing={instanceFor.instance}
          onClose={() => setInstanceFor(null)}
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
                onClick={() => removeBackend(confirmDelete)}
              >
                {removing ? "Deleting…" : "Delete backend"}
              </button>
            </>
          }
        >
          <Notice tone="warn">
            <span>
              This removes {confirmDelete.total_instance_count}{" "}
              {confirmDelete.total_instance_count === 1 ? "instance" : "instances"} and every
              routing rule that points here. Requests those rules were handling will stop
              being served.
            </span>
          </Notice>
        </Modal>
      )}
    </>
  );
}
