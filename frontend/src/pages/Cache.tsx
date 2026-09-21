import { FormEvent, useState } from "react";
import { api } from "../lib/api";
import { useAction, useResource } from "../lib/hooks";
import { CacheStats, Domain, RoutingRule, TimeSeries } from "../lib/types";
import { bytes, compact, percent } from "../lib/format";
import { Chart } from "../components/Chart";
import { Empty, Field, Modal, Notice, Panel, Row, Tag, useToast } from "../components/ui";

/** How each cache outcome should read to someone who did not write Nginx. */
const OUTCOMES: { key: string; label: string; note: string; tone: "hit" | "warn" | "neutral" }[] = [
  { key: "hit", label: "Served from cache", note: "The backend was never asked.", tone: "hit" },
  { key: "stale", label: "Served stale", note: "The backend was slow or down, so a slightly old copy went out instead.", tone: "hit" },
  { key: "revalidated", label: "Confirmed unchanged", note: "The backend answered 304, so nothing was re-transferred.", tone: "hit" },
  { key: "miss", label: "Not in cache", note: "Fetched from the backend and stored.", tone: "neutral" },
  { key: "expired", label: "Too old", note: "Was cached, had aged out, fetched again.", tone: "neutral" },
  { key: "updating", label: "Refreshing", note: "A stale copy went out while another request refreshed it.", tone: "neutral" },
  { key: "bypass", label: "Skipped", note: "Signed-in requests and anything the rules told the cache to leave alone.", tone: "warn" },
  { key: "scarce", label: "Not stored yet", note: "Has not been requested enough times to be worth storing.", tone: "neutral" },
];

function PurgeForm({
  domains,
  onClose,
}: {
  domains: Domain[];
  onClose: () => void;
}) {
  const [draft, setDraft] = useState({
    host: domains[0]?.name ?? "",
    uri: "/",
    scheme: "https",
    method: "GET",
  });
  const [result, setResult] = useState<string | null>(null);
  const toast = useToast();

  const [purge, { busy, error }] = useAction(async (event: FormEvent) => {
    event.preventDefault();
    const response = await api.post<{ purged: number; message: string }>(
      "/api/v1/gateway/cache/purge-url/",
      draft,
    );
    setResult(response.message);
    if (response.purged) toast(response.message);
  });

  return (
    <Modal
      title="Drop one URL from the cache"
      subtitle="Use this when one thing changed. Emptying the whole cache hands every request back to the backends at once."
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            Close
          </button>
          <button className="btn btn--primary" form="purge-form" disabled={busy || !draft.host}>
            {busy ? "Dropping…" : "Drop from cache"}
          </button>
        </>
      }
    >
      <form id="purge-form" onSubmit={purge}>
        {error && (
          <div className="notice notice--error" style={{ marginBottom: 14 }}>
            {error.message}
          </div>
        )}

        <div className="row-2">
          <Field label="Hostname">
            <select
              className="select"
              value={draft.host}
              onChange={(e) => setDraft({ ...draft, host: e.target.value })}
            >
              {domains.map((d) => (
                <option key={d.id} value={d.name}>
                  {d.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Scheme" help="Must match how the request originally arrived.">
            <select
              className="select"
              value={draft.scheme}
              onChange={(e) => setDraft({ ...draft, scheme: e.target.value })}
            >
              <option value="https">https</option>
              <option value="http">http</option>
            </select>
          </Field>
        </div>

        <Field label="Path" help="Including any query string, exactly as clients request it.">
          <input
            className="input input--mono"
            value={draft.uri}
            placeholder="/static/app.js"
            onChange={(e) => setDraft({ ...draft, uri: e.target.value })}
            required
          />
        </Field>

        {result && <Notice>{result}</Notice>}
      </form>
    </Modal>
  );
}

export function Cache() {
  const { data: cache, initialLoading, reload } = useResource<CacheStats>(
    "/api/v1/monitoring/cache/",
    5000,
  );
  const { data: series } = useResource<TimeSeries>(
    "/api/v1/monitoring/series/?minutes=180&step=60s",
    30000,
  );
  const { data: rules } = useResource<RoutingRule[]>("/api/v1/routing/rules/");
  const { data: domains } = useResource<Domain[]>("/api/v1/domains/");

  const [purging, setPurging] = useState(false);
  const [confirmEmpty, setConfirmEmpty] = useState(false);
  const toast = useToast();

  const [emptyCache, { busy: emptying }] = useAction(async () => {
    await api.post("/api/v1/gateway/cache/purge/");
    toast("Emptying the cache. Expect a burst of backend traffic while it refills.");
    setConfirmEmpty(false);
    window.setTimeout(reload, 3000);
  });

  if (initialLoading) return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;
  if (!cache) return null;

  const cachedRules = (rules ?? []).filter((r) => r.cache_enabled && r.is_active);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Cache</h1>
          <p>
            Every response the gateway answered without waking a backend. The{" "}
            <span className="mono">X-Cache-Status</span> header on any response tells you which
            of the outcomes below it took.
          </p>
        </div>
        <div className="page-actions">
          {(domains?.length ?? 0) > 0 && (
            <button className="btn" onClick={() => setPurging(true)}>
              Drop a URL
            </button>
          )}
          <button className="btn btn--danger" onClick={() => setConfirmEmpty(true)}>
            Empty the cache
          </button>
        </div>
      </header>

      {!cache.available && (
        <Notice tone="warn">
          <span>
            <strong>Cache counters unavailable.</strong> {cache.reason}
          </span>
        </Notice>
      )}

      {cache.available && (
        <>
          <div className="grid grid--3" style={{ marginBottom: 16 }}>
            <Panel title="Hit ratio" hint="Of requests the cache could have served">
              <div className="stage__value" style={{ fontSize: "var(--step-5)" }}>
                {percent(cache.hit_ratio, 1)}
              </div>
              <p style={{ margin: "8px 0 0", fontSize: "0.82rem", color: "var(--text-dim)" }}>
                {compact(cache.served_from_cache)} of {compact(cache.cacheable_requests)}{" "}
                cacheable requests. A further {compact(cache.bypassed)} were skipped on
                purpose and are not counted here.
              </p>
            </Panel>

            <Panel title="Work avoided" hint="Bytes served that a backend never had to produce">
              <div className="stage__value" style={{ fontSize: "var(--step-5)" }}>
                {bytes(cache.bytes_saved)}
              </div>
              <p style={{ margin: "8px 0 0", fontSize: "0.82rem", color: "var(--text-dim)" }}>
                {bytes(cache.bytes_to_clients)} went to clients; only{" "}
                {bytes(cache.bytes_from_backend)} came from backends.
              </p>
            </Panel>

            <Panel title="Disk in use" hint="Oldest entries are evicted when it fills">
              <div className="stage__value" style={{ fontSize: "var(--step-5)" }}>
                {percent(cache.disk_used_pct, 0)}
              </div>
              <p style={{ margin: "8px 0 0", fontSize: "0.82rem", color: "var(--text-dim)" }}>
                {bytes(cache.disk_used_bytes)} of {bytes(cache.disk_max_bytes)}.
              </p>
            </Panel>
          </div>

          <div className="grid grid--2" style={{ marginBottom: 16 }}>
            <Panel title="Hit ratio over time" hint="Last three hours">
              <Chart
                series={series?.cache_hit_ratio ?? []}
                formatValue={(v) => `${v.toFixed(0)}%`}
                emptyMessage="Prometheus has no cache samples for this range yet."
              />
            </Panel>

            <Panel title="What happened to each request" flush>
              <div className="rows">
                {OUTCOMES.map((outcome) => {
                  const count = cache.breakdown[outcome.key] ?? 0;
                  if (count === 0) return null;
                  return (
                    <Row
                      key={outcome.key}
                      state={
                        outcome.tone === "hit"
                          ? "healthy"
                          : outcome.tone === "warn"
                            ? "degraded"
                            : "idle"
                      }
                      columns="minmax(0,1fr) auto"
                    >
                      <div>
                        <div style={{ fontSize: "0.88rem", fontWeight: 500 }}>
                          {outcome.label}
                        </div>
                        <div className="row__secondary">{outcome.note}</div>
                      </div>
                      <div className="row__num">{compact(count)}</div>
                    </Row>
                  );
                })}
              </div>
            </Panel>
          </div>
        </>
      )}

      <Panel title="Routes that cache" hint={`${cachedRules.length} of ${rules?.length ?? 0} routes`} flush>
        {cachedRules.length === 0 ? (
          <Empty
            title="No route caches yet"
            body="Caching is turned on per route, so it never applies to something you did not choose. Open a route and switch it on there."
          />
        ) : (
          <div className="rows">
            {cachedRules.map((rule) => (
              <Row key={rule.id} state="healthy" columns="minmax(0,1fr) auto auto">
                <div>
                  <div className="row__primary">
                    {rule.domain_name}
                    {rule.match_value}
                  </div>
                  <div className="row__secondary">
                    Keeps for {rule.cache_ttl}s when the backend says nothing
                    {rule.cache_key_headers.length > 0 &&
                      ` · varies by ${rule.cache_key_headers.join(", ")}`}
                  </div>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {rule.cache_bypass_auth ? (
                    <Tag tone="hit">signed-in requests skipped</Tag>
                  ) : (
                    <Tag tone="down">caches signed-in responses</Tag>
                  )}
                  {rule.cache_ignore_upstream_control && (
                    <Tag tone="warn">overrides the backend</Tag>
                  )}
                </div>
                <div className="row__num">→ {rule.backend_name}</div>
              </Row>
            ))}
          </div>
        )}
      </Panel>

      {purging && domains && <PurgeForm domains={domains} onClose={() => setPurging(false)} />}

      {confirmEmpty && (
        <Modal
          title="Empty the whole cache?"
          onClose={() => setConfirmEmpty(false)}
          footer={
            <>
              <button className="btn" onClick={() => setConfirmEmpty(false)}>
                Cancel
              </button>
              <button className="btn btn--danger" disabled={emptying} onClick={() => emptyCache()}>
                {emptying ? "Emptying…" : "Empty the cache"}
              </button>
            </>
          }
        >
          <Notice tone="warn">
            <span>
              Every request will miss until the cache refills, so the backends take the full
              load for a moment. If you know which URL changed, drop that one instead.
            </span>
          </Notice>
        </Modal>
      )}
    </>
  );
}
