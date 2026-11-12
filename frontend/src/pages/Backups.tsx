/**
 * Backups, and whether any of them would actually work.
 *
 * The page leads with the verification state rather than the newest archive,
 * because "a backup ran last night" is the reassuring half of the answer and
 * the useless half. A backup nobody has read back is a hypothesis, and the
 * failure that hurts is the one that completes every night for months and
 * turns out to be unrestorable on the day it is needed.
 */
import { useState } from "react";
import { api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { bytes, dateTime, relativeTime } from "../lib/format";
import { Empty, Notice, Outcome, Panel, Row, Tag, useToast } from "../components/ui";

interface BackupRun {
  id: number;
  status: "running" | "complete" | "failed";
  trigger: string;
  trigger_label: string;
  started_by_username: string | null;
  filename: string;
  size_bytes: number;
  sha256: string;
  is_encrypted: boolean;
  components: Record<string, { bytes: number; sha256: string; files?: number }>;
  verification: "pending" | "passed" | "failed";
  verification_label: string;
  verified_at: string | null;
  verification_detail: string;
  error: string;
  started_at: string;
  finished_at: string | null;
  duration_seconds: number | null;
  is_restorable: boolean;
}

interface Health {
  directory: string;
  latest: { started_at: string; status: string } | null;
  newest_verified_at: string | null;
  total_on_disk: number;
  bytes_on_disk: number;
  disk_free_bytes: number;
  disk_total_bytes: number;
  encrypted: boolean;
  keep_days: number;
  keep_weeks: number;
  warnings: string[];
}

function stateOf(run: BackupRun): "healthy" | "degraded" | "down" {
  if (run.status === "failed" || run.verification === "failed") return "down";
  if (run.is_restorable) return "healthy";
  return "degraded";
}

export function Backups() {
  const { data, reload } = useResource<{ health: Health; backups: BackupRun[] }>(
    "/api/v1/backups/",
    10000,
  );
  const [expanded, setExpanded] = useState<number | null>(null);
  const toast = useToast();

  const [takeBackup, { busy: taking }] = useAction(async () => {
    await api.post("/api/v1/backups/");
    toast("Backup started. It will appear here when it finishes.");
    window.setTimeout(reload, 2000);
  });

  const [verify] = useAction(async (run: BackupRun) => {
    await api.post(`/api/v1/backups/${run.id}/verify/`);
    toast("Restoring it into a scratch database to check it.");
    window.setTimeout(reload, 2000);
  });

  const [remove] = useAction(async (run: BackupRun) => {
    const sure = window.confirm(
      `Delete ${run.filename}? The archive is removed from disk and cannot be recovered.`,
    );
    if (!sure) return;
    await api.delete(`/api/v1/backups/${run.id}/`);
    toast(`${run.filename} deleted.`);
    reload();
  });

  if (!data) return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;

  const { health, backups } = data;
  const restorable = backups.filter((b) => b.is_restorable).length;

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Backups</h1>
          <p>
            The database, every certificate and its private key, and the
            generated configuration. Kept in{" "}
            <span className="mono">{health.directory}</span>.
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn btn--primary"
            onClick={() => takeBackup()}
            disabled={taking}
          >
            {taking ? "Starting…" : "Back up now"}
          </button>
        </div>
      </header>

      {health.warnings.map((warning) => (
        <Notice key={warning} tone="warn">
          {warning}
        </Notice>
      ))}

      <div className="grid grid--3" style={{ margin: "16px 0" }}>
        <Panel
          title="Known to restore"
          hint="Archives read back into a scratch database"
        >
          <div
            className="stage__value"
            style={{
              fontSize: "var(--step-5)",
              color: restorable ? undefined : "var(--down)",
            }}
          >
            {restorable}
          </div>
          <p style={{ margin: "8px 0 0", fontSize: "0.82rem", color: "var(--text-dim)" }}>
            {health.newest_verified_at
              ? `newest verified ${relativeTime(health.newest_verified_at)}`
              : "none has been read back yet"}
          </p>
        </Panel>

        <Panel title="On disk" hint={`${health.keep_days} daily, then ${health.keep_weeks} weekly`}>
          <div className="stage__value" style={{ fontSize: "var(--step-5)" }}>
            {bytes(health.bytes_on_disk)}
          </div>
          <p style={{ margin: "8px 0 0", fontSize: "0.82rem", color: "var(--text-dim)" }}>
            {health.total_on_disk} archive{health.total_on_disk === 1 ? "" : "s"} ·{" "}
            {bytes(health.disk_free_bytes)} free
          </p>
        </Panel>

        <Panel title="At rest" hint="Archives hold private keys either way">
          <div style={{ marginTop: 4 }}>
            <Tag tone={health.encrypted ? "hit" : "warn"}>
              {health.encrypted ? "encrypted" : "not encrypted"}
            </Tag>
          </div>
          <p style={{ margin: "12px 0 0", fontSize: "0.82rem", color: "var(--text-dim)" }}>
            {health.encrypted
              ? "Keep the key somewhere other than this host."
              : "Set BACKUP_ENCRYPTION_KEY before copying an archive anywhere."}
          </p>
        </Panel>
      </div>

      <Panel title="History" flush>
        {backups.length === 0 ? (
          <Empty
            title="No backups yet"
            body="One runs nightly. Take one now if you would rather not wait — it is also the only way to find out whether backups work on this host before you need them to."
          />
        ) : (
          <div className="rows">
            {backups.map((run) => (
              <div key={run.id}>
                <Row state={stateOf(run)} columns="minmax(0,1fr) auto auto auto">
                  <div>
                    <div className="row__primary mono">
                      {run.filename || "(failed before it was named)"}
                    </div>
                    <div className="row__secondary">
                      {relativeTime(run.started_at)} · {run.trigger_label}
                      {run.started_by_username && ` by ${run.started_by_username}`}
                      {run.duration_seconds !== null &&
                        ` · took ${run.duration_seconds.toFixed(1)}s`}
                    </div>
                  </div>

                  <div style={{ display: "flex", gap: 6 }}>
                    {run.is_encrypted && <Tag tone="signal">encrypted</Tag>}
                    <Tag
                      tone={
                        run.verification === "passed"
                          ? "hit"
                          : run.verification === "failed"
                            ? "down"
                            : "warn"
                      }
                    >
                      {run.status === "failed" ? "failed" : run.verification_label}
                    </Tag>
                  </div>

                  <div className="row__num">{run.size_bytes ? bytes(run.size_bytes) : "—"}</div>

                  <div className="row__actions">
                    <button
                      className="btn btn--small"
                      onClick={() => setExpanded(expanded === run.id ? null : run.id)}
                    >
                      {expanded === run.id ? "Less" : "Details"}
                    </button>
                    {run.status === "complete" && (
                      <button className="btn btn--small" onClick={() => verify(run)}>
                        Check it restores
                      </button>
                    )}
                    <button className="btn btn--small" onClick={() => remove(run)}>
                      Delete
                    </button>
                  </div>
                </Row>

                {expanded === run.id && (
                  <div className="panel__body" style={{ paddingTop: 0 }}>
                    <Outcome>
                      {run.error ||
                        run.verification_detail ||
                        "Not checked yet. Until something reads it back, this is an archive rather than a backup."}
                    </Outcome>

                    <div className="rows" style={{ marginTop: 10 }}>
                      {Object.entries(run.components).map(([name, detail]) => (
                        <Row key={name} state="healthy" columns="minmax(0,1fr) auto">
                          <div style={{ fontSize: "0.85rem" }}>
                            {name.replace("_", " ")}
                            {detail.files !== undefined && ` · ${detail.files} files`}
                          </div>
                          <div className="row__num">{bytes(detail.bytes)}</div>
                        </Row>
                      ))}
                    </div>

                    <p
                      className="mono"
                      style={{
                        marginTop: 10,
                        fontSize: "0.72rem",
                        color: "var(--text-faint)",
                        wordBreak: "break-all",
                      }}
                    >
                      started {dateTime(run.started_at)}
                      {run.sha256 && <> · sha256 {run.sha256}</>}
                    </p>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Panel title="Restoring" hint="Deliberately not a button">
        <Outcome>
          Putting a database back underneath a running panel means stopping the
          panel first, so this is a command rather than a control here — a page
          that offered to do it would be sawing the branch it sits on.
        </Outcome>
        <pre
          className="mono"
          style={{
            marginTop: 12,
            padding: 12,
            background: "var(--ink-900)",
            border: "1px solid var(--line)",
            borderRadius: "var(--radius)",
            fontSize: "0.78rem",
            overflowX: "auto",
          }}
        >
{`make backup-list                    # what exists, and what is verified
make restore ARCHIVE=<filename>     # unpack it and print the steps`}
        </pre>
      </Panel>
    </>
  );
}
