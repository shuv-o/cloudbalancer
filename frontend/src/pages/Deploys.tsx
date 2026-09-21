import { useState } from "react";
import { api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { DeployLog, GatewayStatus } from "../lib/types";
import { dateTime, relativeTime } from "../lib/format";
import { Empty, Modal, Notice, Panel, Row, Tag, useToast } from "../components/ui";

export function Deploys() {
  const { data: logs, initialLoading, reload } = useResource<DeployLog[]>(
    "/api/v1/routing/deploys/?limit=50",
    10000,
  );
  const { data: status } = useResource<GatewayStatus>("/api/v1/gateway/status/", 15000);
  const [detail, setDetail] = useState<DeployLog | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const toast = useToast();

  const [deploy, { busy: deploying }] = useAction(async () => {
    const response = await api.post<{ message: string }>("/api/v1/gateway/deploy/");
    toast(response.message);
    window.setTimeout(reload, 5000);
  });

  const [showPreview, { busy: previewing }] = useAction(async () => {
    const response = await api.get<{ config: string }>(
      "/api/v1/gateway/config/?format=combined",
    );
    setPreview(response.config);
  });

  const [openDetail] = useAction(async (log: DeployLog) => {
    setDetail(await api.get<DeployLog>(`/api/v1/routing/deploys/${log.id}/`));
  });

  if (initialLoading) return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;

  const history = logs ?? [];

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Deploys</h1>
          <p>
            Every change is written out, checked, and only then loaded. A configuration that
            fails the check is rolled back before the gateway ever sees it, so a mistake here
            cannot take traffic down.
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" onClick={() => showPreview()} disabled={previewing}>
            {previewing ? "Rendering…" : "Preview config"}
          </button>
          <button className="btn btn--primary" onClick={() => deploy()} disabled={deploying}>
            {deploying ? "Deploying…" : "Deploy now"}
          </button>
        </div>
      </header>

      {status && !status.config_valid && (
        <Notice tone="error">
          <span>
            <strong>The live configuration does not pass its own check.</strong> Nginx is
            still serving whatever it loaded last. Preview the config to see what changed.
          </span>
        </Notice>
      )}

      {status && (
        <Notice>
          <span>
            Changes are gathered for {status.debounce_seconds} seconds before being applied, so
            editing several rules costs one reload rather than one each. Every reload starts
            fresh workers with empty connection pools to the backends, which is why they are
            worth batching.
          </span>
        </Notice>
      )}

      <Panel title="History" flush>
        {history.length === 0 ? (
          <Empty
            title="Nothing deployed yet"
            body="The first deploy happens automatically as soon as you add a domain or a backend."
          />
        ) : (
          <div className="rows">
            {history.map((log) => (
              <Row
                key={log.id}
                state={
                  log.status === "deployed"
                    ? "healthy"
                    : log.status === "failed"
                      ? "down"
                      : "idle"
                }
                columns="auto minmax(0,1fr) auto auto auto"
              >
                <div className="row__num" style={{ color: "var(--text-faint)", minWidth: 40 }}>
                  #{log.id}
                </div>

                <div>
                  <div style={{ fontSize: "0.88rem", fontWeight: 500 }}>
                    {log.status === "deployed" && "Applied"}
                    {log.status === "failed" && "Rejected and rolled back"}
                    {log.status === "testing" && "Checking"}
                    {log.status === "pending" && "Starting"}
                    {log.status === "rolled_back" && "Rolled back"}
                  </div>
                  <div className="row__secondary">
                    {log.deployed_by_username
                      ? `by ${log.deployed_by_username}`
                      : "automatically"}
                    {log.error_output && ` · ${log.error_output.slice(0, 90)}`}
                  </div>
                </div>

                <div>
                  {log.status === "deployed" && <Tag tone="hit">live</Tag>}
                  {log.status === "failed" && <Tag tone="down">not applied</Tag>}
                </div>

                <div className="row__num" style={{ fontSize: "0.78rem", color: "var(--text-faint)" }}>
                  {log.duration_ms !== null ? `${(log.duration_ms / 1000).toFixed(1)}s` : "—"} ·{" "}
                  {relativeTime(log.created_at)}
                </div>

                <div className="row__actions">
                  <button className="btn btn--small" onClick={() => openDetail(log)}>
                    Config
                  </button>
                </div>
              </Row>
            ))}
          </div>
        )}
      </Panel>

      {detail && (
        <Modal
          title={`Deploy #${detail.id}`}
          subtitle={`${dateTime(detail.created_at)} · ${detail.status_label}`}
          wide
          onClose={() => setDetail(null)}
          footer={
            <button className="btn" onClick={() => setDetail(null)}>
              Close
            </button>
          }
        >
          {detail.error_output && (
            <Notice tone="error">
              <span>
                <strong>Why it was rejected:</strong>
                <pre
                  className="mono"
                  style={{ margin: "6px 0 0", whiteSpace: "pre-wrap", fontSize: "0.76rem" }}
                >
                  {detail.error_output}
                </pre>
              </span>
            </Notice>
          )}
          <pre className="code">{detail.config_snapshot}</pre>
        </Modal>
      )}

      {preview !== null && (
        <Modal
          title="Configuration preview"
          subtitle="What a deploy would write right now. Nothing has been changed."
          wide
          onClose={() => setPreview(null)}
          footer={
            <>
              <button className="btn" onClick={() => setPreview(null)}>
                Close
              </button>
              <button
                className="btn btn--primary"
                onClick={() => {
                  setPreview(null);
                  deploy();
                }}
              >
                Deploy this
              </button>
            </>
          }
        >
          <pre className="code">{preview}</pre>
        </Modal>
      )}
    </>
  );
}
