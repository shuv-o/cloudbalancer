/**
 * What is wrong, and whether anyone was told.
 *
 * The second half is the part worth building a page for. An alerting system
 * that records everything and delivers nothing looks identical to a healthy
 * one from here, right up until the incident it was installed for — so the
 * header leads with whether channels exist and whether they are working,
 * not with how many alerts are firing.
 */
import { useState } from "react";
import { ApiError, api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { dateTime, relativeTime } from "../lib/format";
import { useCan } from "../lib/session";
import {
  Empty,
  Field,
  Modal,
  Notice,
  Outcome,
  Panel,
  Row,
  Tag,
  useToast,
} from "../components/ui";

interface Alert {
  id: number;
  severity: "info" | "warning" | "critical";
  severity_label: string;
  status: "firing" | "resolved";
  source_label: string;
  title: string;
  body: string;
  runbook: string;
  labels: Record<string, string | number>;
  started_at: string;
  resolved_at: string | null;
  occurrences: number;
  last_notified_at: string | null;
  acknowledged_at: string | null;
  acknowledged_by_username: string | null;
  is_acknowledged: boolean;
  duration_seconds: number;
}

interface Channel {
  id: number;
  name: string;
  kind: string;
  kind_label: string;
  target: string;
  is_enabled: boolean;
  min_severity: string;
  is_healthy: boolean;
  last_error: string;
  last_success_at: string | null;
  consecutive_failures: number;
}

interface Summary {
  firing: number;
  critical: number;
  unacknowledged: number;
  channels: number;
  warnings: string[];
}

const TONE: Record<string, "down" | "warn" | "signal"> = {
  critical: "down",
  warning: "warn",
  info: "signal",
};

function AddChannel({
  emailConfigured,
  onAdded,
}: {
  emailConfigured: boolean;
  onAdded: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState({
    name: "",
    kind: "slack",
    url: "",
    to: "",
    token: "",
    min_severity: "warning",
  });
  const [error, setError] = useState<string | null>(null);
  const toast = useToast();

  const [submit, { busy }] = useAction(async () => {
    setError(null);
    const config =
      draft.kind === "email"
        ? { to: draft.to.split(",").map((s) => s.trim()).filter(Boolean) }
        : draft.token
          ? { url: draft.url.trim(), token: draft.token.trim() }
          : { url: draft.url.trim() };

    try {
      await api.post("/api/v1/alerts/channels/", {
        name: draft.name,
        kind: draft.kind,
        config,
        min_severity: draft.min_severity,
      });
      toast(`${draft.name} added. Send it a test before relying on it.`);
      setOpen(false);
      setDraft({ name: "", kind: "slack", url: "", to: "", token: "", min_severity: "warning" });
      onAdded();
    } catch (err) {
      setError((err as ApiError).message);
    }
  });

  return (
    <>
      <button className="btn btn--small" onClick={() => setOpen(true)}>
        Add channel
      </button>

      {open && (
        <Modal
          title="Where should alerts go?"
          onClose={() => setOpen(false)}
          footer={
            <>
              <button className="btn" onClick={() => setOpen(false)}>
                Cancel
              </button>
              <button
                className="btn btn--primary"
                form="add-channel"
                disabled={busy || !draft.name}
              >
                {busy ? "Adding…" : "Add channel"}
              </button>
            </>
          }
        >
          <form
            id="add-channel"
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

            <Field label="Name" help="How this appears in the delivery history.">
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
                onChange={(e) => setDraft({ ...draft, kind: e.target.value })}
              >
                <option value="slack">Slack or Mattermost</option>
                <option value="webhook">Webhook</option>
                <option value="email">Email</option>
              </select>
            </Field>

            {draft.kind === "email" ? (
              <>
                <Field label="Recipients" help="Comma separated.">
                  <input
                    className="input"
                    value={draft.to}
                    placeholder="ops@example.org, oncall@example.org"
                    onChange={(e) => setDraft({ ...draft, to: e.target.value })}
                  />
                </Field>
                {!emailConfigured && (
                  <Notice tone="warn">
                    No mail server is configured, so this channel will fail
                    every delivery until EMAIL_HOST is set. A webhook needs no
                    server-side setup.
                  </Notice>
                )}
              </>
            ) : (
              <>
                <Field
                  label="URL"
                  help={
                    draft.kind === "slack"
                      ? "The incoming-webhook URL. Treat it as a credential — anyone holding it can post as you."
                      : "Where to POST the alert as JSON."
                  }
                >
                  <input
                    className="input input--mono"
                    value={draft.url}
                    placeholder="https://hooks.slack.com/services/…"
                    onChange={(e) => setDraft({ ...draft, url: e.target.value })}
                  />
                </Field>
                {draft.kind === "webhook" && (
                  <Field
                    label="Bearer token"
                    help="Optional. Sent as an Authorization header so your endpoint can tell these apart from anyone else's POST."
                  >
                    <input
                      className="input input--mono"
                      value={draft.token}
                      onChange={(e) => setDraft({ ...draft, token: e.target.value })}
                    />
                  </Field>
                )}
              </>
            )}

            <Field
              label="Send"
              help="This severity and anything worse. Information is noisy by design."
            >
              <select
                className="select"
                value={draft.min_severity}
                onChange={(e) => setDraft({ ...draft, min_severity: e.target.value })}
              >
                <option value="critical">Critical only</option>
                <option value="warning">Warnings and critical</option>
                <option value="info">Everything</option>
              </select>
            </Field>
          </form>
        </Modal>
      )}
    </>
  );
}

export function Alerts() {
  const isAdmin = useCan("admin");
  const canAcknowledge = useCan("operator");
  const [showResolved, setShowResolved] = useState(false);
  const [expanded, setExpanded] = useState<number | null>(null);
  const toast = useToast();

  const { data, reload } = useResource<{ summary: Summary; alerts: Alert[] }>(
    `/api/v1/alerts/${showResolved ? "" : "?status=firing"}`,
    15000,
  );
  const { data: channelData, reload: reloadChannels } = useResource<{
    channels: Channel[];
    email_configured: boolean;
  }>(isAdmin ? "/api/v1/alerts/channels/" : null);

  const [acknowledge] = useAction(async (alert: Alert) => {
    await api.post(`/api/v1/alerts/${alert.id}/acknowledge/`);
    toast(`Acknowledged. Reminders for this one stop; it stays until it clears.`);
    reload();
  });

  const [testChannel] = useAction(async (channel: Channel) => {
    try {
      await api.post(`/api/v1/alerts/channels/${channel.id}/test/`);
      toast(`Sent. If it does not arrive, the channel is not working.`);
    } catch (err) {
      toast((err as ApiError).message, "error");
    }
    reloadChannels();
  });

  const [removeChannel] = useAction(async (channel: Channel) => {
    if (!window.confirm(`Stop sending alerts to ${channel.name}?`)) return;
    await api.delete(`/api/v1/alerts/channels/${channel.id}/`);
    toast(`${channel.name} removed.`);
    reloadChannels();
  });

  if (!data) return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;

  const { summary, alerts } = data;

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Alerts</h1>
          <p>
            What Prometheus sees about traffic, and what the control plane knows
            about certificates, backups and deploys — in one place, because an
            incident is a bad time to read two.
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" onClick={() => setShowResolved(!showResolved)}>
            {showResolved ? "Firing only" : "Include resolved"}
          </button>
        </div>
      </header>

      {summary.warnings.map((warning) => (
        <Notice key={warning} tone="warn">
          {warning}
        </Notice>
      ))}

      <div className="grid grid--3" style={{ margin: "16px 0" }}>
        <Panel title="Firing" hint="Unresolved right now">
          <div
            className="stage__value"
            style={{
              fontSize: "var(--step-5)",
              color: summary.critical ? "var(--down)" : undefined,
            }}
          >
            {summary.firing}
          </div>
          <p style={{ margin: "8px 0 0", fontSize: "0.82rem", color: "var(--text-dim)" }}>
            {summary.critical} critical · {summary.unacknowledged} nobody has picked up
          </p>
        </Panel>

        <Panel title="Channels" hint="Where these actually go">
          <div
            className="stage__value"
            style={{
              fontSize: "var(--step-5)",
              color: summary.channels ? undefined : "var(--down)",
            }}
          >
            {summary.channels}
          </div>
          <p style={{ margin: "8px 0 0", fontSize: "0.82rem", color: "var(--text-dim)" }}>
            {summary.channels
              ? "enabled"
              : "nothing here leaves this page"}
          </p>
        </Panel>

        <Panel title="Acknowledging" hint="Not the same as resolving">
          <Outcome>
            Acknowledging stops the reminders for an alert. The problem stays
            listed until it actually clears — the repeating was there to find
            somebody, and somebody has been found.
          </Outcome>
        </Panel>
      </div>

      <Panel title={showResolved ? "Alerts" : "Firing"} flush>
        {alerts.length === 0 ? (
          <Empty
            title={showResolved ? "Nothing recorded" : "Nothing is firing"}
            body={
              summary.channels
                ? "Checks run every few minutes. Anything that goes wrong appears here and is sent to your channels."
                : "Nothing is wrong — but with no channels configured, that would look the same if something were."
            }
          />
        ) : (
          <div className="rows">
            {alerts.map((alert) => (
              <div key={alert.id}>
                <Row
                  state={
                    alert.status === "resolved"
                      ? "healthy"
                      : alert.severity === "critical"
                        ? "down"
                        : "degraded"
                  }
                  columns="minmax(0,1fr) auto auto"
                >
                  <div>
                    <div className="row__primary">{alert.title}</div>
                    <div className="row__secondary">
                      {alert.status === "resolved"
                        ? `resolved ${relativeTime(alert.resolved_at!)} · lasted ${Math.round(alert.duration_seconds / 60)} min`
                        : `since ${relativeTime(alert.started_at)}`}
                      {alert.occurrences > 1 && ` · seen ${alert.occurrences} times`}
                      {" · "}
                      {alert.source_label}
                      {!alert.last_notified_at && alert.status === "firing" && (
                        <> · not sent anywhere</>
                      )}
                    </div>
                  </div>

                  <div style={{ display: "flex", gap: 6 }}>
                    {alert.is_acknowledged && (
                      <Tag tone="signal">
                        {alert.acknowledged_by_username
                          ? `${alert.acknowledged_by_username} has it`
                          : "acknowledged"}
                      </Tag>
                    )}
                    <Tag tone={alert.status === "resolved" ? "hit" : TONE[alert.severity]}>
                      {alert.status === "resolved" ? "resolved" : alert.severity_label}
                    </Tag>
                  </div>

                  <div className="row__actions">
                    <button
                      className="btn btn--small"
                      onClick={() => setExpanded(expanded === alert.id ? null : alert.id)}
                    >
                      {expanded === alert.id ? "Less" : "Details"}
                    </button>
                    {canAcknowledge &&
                      alert.status === "firing" &&
                      !alert.is_acknowledged && (
                        <button
                          className="btn btn--small"
                          onClick={() => acknowledge(alert)}
                        >
                          I'm on it
                        </button>
                      )}
                  </div>
                </Row>

                {expanded === alert.id && (
                  <div className="panel__body" style={{ paddingTop: 0 }}>
                    {alert.body && <Outcome>{alert.body}</Outcome>}

                    {alert.runbook && (
                      <Notice tone="info">
                        <span>{alert.runbook}</span>
                      </Notice>
                    )}

                    {Object.keys(alert.labels).length > 0 && (
                      <div className="rows" style={{ marginTop: 10 }}>
                        {Object.entries(alert.labels).map(([name, value]) => (
                          <Row key={name} state="healthy" columns="minmax(0,1fr) auto">
                            <div style={{ fontSize: "0.85rem" }}>{name}</div>
                            <div className="row__num mono">{String(value)}</div>
                          </Row>
                        ))}
                      </div>
                    )}

                    <p
                      style={{
                        marginTop: 10,
                        fontSize: "0.75rem",
                        color: "var(--text-faint)",
                      }}
                    >
                      started {dateTime(alert.started_at)}
                      {alert.last_notified_at
                        ? ` · last sent ${relativeTime(alert.last_notified_at)}`
                        : " · never sent to anyone"}
                    </p>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Panel>

      {isAdmin && channelData && (
        <Panel
          title="Channels"
          hint="A channel that has been failing quietly is worse than no channel"
          actions={
            <AddChannel
              emailConfigured={channelData.email_configured}
              onAdded={reloadChannels}
            />
          }
          flush
        >
          {channelData.channels.length === 0 ? (
            <Empty
              title="Alerts go nowhere"
              body="Everything above is recorded and shown on this page only. Add a Slack or webhook channel so somebody hears about it without opening the panel."
            />
          ) : (
            <div className="rows">
              {channelData.channels.map((channel) => (
                <Row
                  key={channel.id}
                  state={
                    !channel.is_enabled
                      ? "degraded"
                      : channel.is_healthy
                        ? "healthy"
                        : "down"
                  }
                  columns="minmax(0,1fr) auto auto"
                >
                  <div>
                    <div className="row__primary">{channel.name}</div>
                    <div className="row__secondary">
                      {channel.kind_label} · <span className="mono">{channel.target}</span>
                      {channel.last_success_at
                        ? ` · last delivered ${relativeTime(channel.last_success_at)}`
                        : " · nothing delivered yet"}
                      {!channel.is_healthy && channel.last_error && (
                        <>
                          {" · "}
                          <span style={{ color: "var(--down)" }}>{channel.last_error}</span>
                        </>
                      )}
                    </div>
                  </div>

                  <div style={{ display: "flex", gap: 6 }}>
                    {!channel.is_enabled && <Tag tone="warn">disabled</Tag>}
                    <Tag tone={channel.is_healthy ? "hit" : "down"}>
                      {channel.is_healthy
                        ? channel.min_severity
                        : `${channel.consecutive_failures} failures`}
                    </Tag>
                  </div>

                  <div className="row__actions">
                    <button className="btn btn--small" onClick={() => testChannel(channel)}>
                      Send a test
                    </button>
                    <button className="btn btn--small" onClick={() => removeChannel(channel)}>
                      Remove
                    </button>
                  </div>
                </Row>
              ))}
            </div>
          )}
        </Panel>
      )}
    </>
  );
}
