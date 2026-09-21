import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useResource } from "../lib/hooks";
import { Overview as OverviewData, TimeSeries } from "../lib/types";
import { bytes, compact, ms, percent, relativeTime } from "../lib/format";
import { SignalPath } from "../components/SignalPath";
import { Chart, ChartLegend } from "../components/Chart";
import { Empty, Notice, Panel, Row, Tag } from "../components/ui";

/** Keeps a short rolling history of request counts for the inline bar chart. */
function useRequestHistory(total: number | undefined, length = 12): number[] {
  const [history, setHistory] = useState<number[]>([]);
  const previous = useRef<number | null>(null);

  useEffect(() => {
    if (total === undefined) return;
    if (previous.current !== null) {
      const delta = Math.max(total - previous.current, 0);
      setHistory((current) => [...current, delta].slice(-length));
    }
    previous.current = total;
  }, [total, length]);

  return history;
}

export function Overview() {
  const { data, error, initialLoading } = useResource<OverviewData>(
    "/api/v1/monitoring/overview/",
    5000,
  );
  const { data: series } = useResource<TimeSeries>(
    "/api/v1/monitoring/series/?minutes=60&step=30s",
    30000,
  );
  const history = useRequestHistory(data?.traffic.total_requests);

  if (initialLoading) {
    return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;
  }

  if (error && !data) {
    return (
      <Notice tone="error">
        <span>{error.message}</span>
      </Notice>
    );
  }

  if (!data) return null;

  const { summary, traffic, cache, upstreams, health } = data;
  const nothingConfigured = summary.domains.total === 0 && summary.backends.total === 0;

  if (nothingConfigured) {
    return (
      <>
        <header className="page-head">
          <div>
            <h1>Overview</h1>
            <p>Nothing is routed yet.</p>
          </div>
        </header>
        <section className="panel">
          <Empty
            title="Set up the first route"
            body="A route needs somewhere to send traffic and a hostname to accept it on. Add a backend with at least one instance, add a domain, then connect the two with a rule."
            action={
              <div style={{ display: "flex", gap: 8, justifyContent: "center" }}>
                <Link className="btn btn--primary" to="/backends">
                  Add a backend
                </Link>
                <Link className="btn" to="/domains">
                  Add a domain
                </Link>
              </div>
            }
          />
        </section>
      </>
    );
  }

  const certs = summary.certificates;
  const expiringSoon = certs.expiring_within_14d + certs.expired;
  const degraded = health.filter((b) => b.status !== "healthy");

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Overview</h1>
          <p>
            {traffic.available
              ? `Nginx ${traffic.nginx_version}, up ${relativeTime(
                  new Date(Date.now() - traffic.uptime_seconds * 1000).toISOString(),
                ).replace(" ago", "")}.`
              : "Live traffic counters are not reachable. Configuration below is still accurate."}
          </p>
        </div>
      </header>

      {!traffic.available && (
        <Notice tone="warn">
          <span>
            <strong>No live counters.</strong> {traffic.reason} Nginx serves these on port
            8080; check that the traffic-status module loaded and that this container can
            reach it.
          </span>
        </Notice>
      )}

      {certs.failed > 0 && (
        <Notice tone="error">
          <span>
            <strong>
              {certs.failed} certificate {certs.failed === 1 ? "request" : "requests"} failed.
            </strong>{" "}
            <Link to="/certificates">See why</Link>.
          </span>
        </Notice>
      )}

      {expiringSoon > 0 && (
        <Notice tone="warn">
          <span>
            <strong>
              {expiringSoon} {expiringSoon === 1 ? "certificate needs" : "certificates need"}{" "}
              attention.
            </strong>{" "}
            {certs.next_expiry && (
              <>
                The next is <span className="mono">{certs.next_expiry.domain__name}</span>.{" "}
              </>
            )}
            <Link to="/certificates">Review them</Link>.
          </span>
        </Notice>
      )}

      {summary.deploys.last_24h_failed > 0 && (
        <Notice tone="warn">
          <span>
            <strong>
              {summary.deploys.last_24h_failed} failed{" "}
              {summary.deploys.last_24h_failed === 1 ? "deploy" : "deploys"} in the last day.
            </strong>{" "}
            The gateway kept running the previous configuration.{" "}
            <Link to="/deploys">Read the error</Link>.
          </span>
        </Notice>
      )}

      <SignalPath
        summary={summary}
        traffic={traffic}
        cache={cache}
        health={health}
        history={history}
      />

      <div className="grid grid--2" style={{ marginBottom: 16 }}>
        <Panel
          title="Gateway added latency"
          hint="Total request time minus upstream time — the proxy's own cost, with the backend subtracted out"
        >
          <Chart
            series={series?.gateway_added_ms ?? []}
            unit="ms"
            threshold={{ value: 1, label: "1 ms budget" }}
            formatValue={(v) => v.toFixed(2)}
            emptyMessage="Prometheus has no samples yet. It needs a minute of traffic before this fills in."
          />
        </Panel>

        <Panel title="Requests per second" hint="Measured across every configured domain">
          <Chart
            series={series?.requests_per_second ?? []}
            formatValue={(v) => v.toFixed(0)}
            emptyMessage="Prometheus has no samples yet."
          />
        </Panel>
      </div>

      <div className="grid grid--2" style={{ marginBottom: 16 }}>
        <Panel title="Cache hit ratio" hint="Share of cacheable requests served without a backend">
          <Chart
            series={series?.cache_hit_ratio ?? []}
            formatValue={(v) => `${v.toFixed(0)}%`}
            emptyMessage="No cache activity recorded yet."
          />
        </Panel>

        <Panel title="Backend response time" hint="What the backends themselves cost, per upstream">
          <Chart
            series={series?.upstream_latency_ms ?? []}
            formatValue={(v) => v.toFixed(1)}
            emptyMessage="No upstream samples yet."
          />
          <ChartLegend series={series?.upstream_latency_ms ?? []} />
        </Panel>
      </div>

      <div className="grid grid--2">
        <Panel
          title="Traffic by domain"
          hint={traffic.available ? "Since Nginx last started" : undefined}
          flush
        >
          {traffic.domains.length === 0 ? (
            <Empty
              title="No requests yet"
              body="Once traffic reaches a configured domain, it appears here with its response mix and cache ratio."
            />
          ) : (
            <div className="rows">
              {traffic.domains.slice(0, 8).map((domain) => (
                <Row
                  key={domain.domain}
                  state={
                    domain.error_rate === null
                      ? "idle"
                      : domain.error_rate > 5
                        ? "down"
                        : domain.error_rate > 1
                          ? "degraded"
                          : "healthy"
                  }
                  columns="minmax(0,1fr) auto auto auto"
                >
                  <div>
                    <div className="row__primary">{domain.domain}</div>
                    <div className="row__secondary">
                      {domain.responses["2xx"]} ok · {domain.responses["4xx"]} client ·{" "}
                      {domain.responses["5xx"]} server
                    </div>
                  </div>
                  <div className="row__num" title="Requests">
                    {compact(domain.requests)}
                  </div>
                  <div className="row__num" title="Average response time">
                    {ms(domain.avg_response_ms, 1)}
                  </div>
                  <div className="row__num" title="Cache hit ratio">
                    {percent(domain.cache_hit_ratio, 0)}
                  </div>
                </Row>
              ))}
            </div>
          )}
        </Panel>

        <Panel
          title="Backend health"
          hint={degraded.length ? `${degraded.length} needs attention` : "All instances answering"}
          flush
        >
          {health.length === 0 ? (
            <Empty
              title="No backends yet"
              body="Add a backend and at least one instance, and its health will be probed from here."
            />
          ) : (
            <div className="rows">
              {health.map((backend) => (
                <Row key={backend.id} state={backend.status} columns="minmax(0,1fr) auto auto">
                  <div>
                    <div className="row__primary">{backend.name}</div>
                    <div className="row__secondary">
                      {backend.instances
                        .slice(0, 4)
                        .map((i) => i.address)
                        .join(", ")}
                      {backend.instances.length > 4 && ` +${backend.instances.length - 4}`}
                    </div>
                  </div>
                  <div>
                    {backend.status === "healthy" ? (
                      <Tag tone="hit">{backend.healthy_count} up</Tag>
                    ) : backend.status === "degraded" ? (
                      <Tag tone="warn">
                        {backend.healthy_count} of {backend.total_count} up
                      </Tag>
                    ) : (
                      <Tag tone="down">all down</Tag>
                    )}
                  </div>
                  <div className="row__num">
                    {ms(
                      backend.instances.find((i) => i.response_ms !== null)?.response_ms ?? null,
                      1,
                    )}
                  </div>
                </Row>
              ))}
            </div>
          )}
        </Panel>
      </div>

      {upstreams.available && upstreams.upstreams.length > 0 && (
        <div style={{ marginTop: 16 }}>
          <Panel
            title="Load distribution"
            hint="Uneven request counts across instances of one backend mean the balancer is not balancing"
            flush
          >
            <div className="rows">
              {upstreams.upstreams.flatMap((upstream) =>
                upstream.servers.map((server) => (
                  <Row
                    key={`${upstream.upstream}-${server.server}`}
                    state={server.down ? "down" : "healthy"}
                    columns="minmax(0,1fr) auto auto auto"
                  >
                    <div>
                      <div className="row__primary">{server.server}</div>
                      <div className="row__secondary">
                        {upstream.upstream}
                        {server.weight !== 1 && ` · weight ${server.weight}`}
                      </div>
                    </div>
                    <div className="row__num">{compact(server.requests)}</div>
                    <div className="row__num">{ms(server.avg_response_ms, 1)}</div>
                    <div className="row__num">{bytes(server.bytes_out)}</div>
                  </Row>
                )),
              )}
            </div>
          </Panel>
        </div>
      )}
    </>
  );
}
