/**
 * Who is using this gateway, from where, and what they ask for.
 *
 * None of this can come from the metrics. Nginx's traffic-status module
 * aggregates per vhost and per upstream — it knows a domain served four
 * million requests and nothing about who made them. Every number on this
 * page is folded out of the access logs instead.
 *
 * The page is ordered by the question an operator actually arrives with.
 * "Is something wrong" is answered by the header; "who is doing this" by the
 * clients table; "when does it happen" by the grid; "what are they asking
 * for" by the paths. Geography is last because it is the one people look at
 * and the one that least often explains anything.
 */
import { useState } from "react";
import { api } from "../lib/api";
import { useResource } from "../lib/hooks";
import { bytes, compact, ms, percent, relativeTime } from "../lib/format";
import { useCan } from "../lib/session";
import { Heatmap, HeatCell } from "../components/Heatmap";
import {
  Empty,
  Notice,
  Outcome,
  Panel,
  Row,
  Tag,
  useToast,
} from "../components/ui";

interface Summary {
  days: number;
  domain: string;
  requests: number;
  bytes_sent: number;
  errors: number;
  error_rate: number | null;
  average_ms: number | null;
  distinct_clients: number;
  client_days: number;
  countries: number;
  networks: number;
  ingest: {
    files: number;
    lines: number;
    unparsed: number;
    last_read_at: string | null;
  };
}

interface AgentKind {
  kind: string;
  label: string;
  requests: number;
  share: number;
  example: string;
}

interface GeoDatabase {
  present: boolean;
  loaded: boolean;
  bytes: number;
}

interface Overview {
  summary: Summary;
  agents: { total: number; kinds: AgentKind[] };
  domains: string[];
  ranges: { label: string; days: number }[];
  geoip: {
    any: boolean;
    attribution: string;
    databases: Record<string, GeoDatabase>;
  };
}

interface Client {
  ip: string;
  country: string;
  country_name: string;
  city: string;
  asn: number | null;
  asn_name: string;
  requests: number;
  bytes_sent: number;
  errors: number;
  server_errors: number;
  error_rate: number | null;
  average_ms: number | null;
  domains: number;
  last_seen: string;
}

interface PathRow {
  path: string;
  requests: number;
  bytes_sent: number;
  errors: number;
  server_errors: number;
  error_rate: number | null;
  average_ms: number | null;
  worst_ms: number;
  cache_hit_ratio: number | null;
}

/** A country code as a flag, which is two regional-indicator code points. */
function flag(code: string): string {
  if (!code || code.length !== 2) return "🏳";
  return String.fromCodePoint(
    ...[...code.toUpperCase()].map((c) => 0x1f1e6 - 65 + c.charCodeAt(0)),
  );
}

function Bar({ share }: { share: number }) {
  return (
    <div className="share">
      <div className="share__fill" style={{ width: `${Math.min(share, 100)}%` }} />
    </div>
  );
}

export function Traffic() {
  const canSeeClients = useCan("operator");
  const isAdmin = useCan("admin");
  const [days, setDays] = useState(7);
  const [domain, setDomain] = useState("");
  const [pathView, setPathView] = useState<"busiest" | "slowest" | "failing">("busiest");
  const [heatMetric, setHeatMetric] = useState<"average_requests" | "errors">(
    "average_requests",
  );
  const toast = useToast();

  const query = `?days=${days}${domain ? `&domain=${encodeURIComponent(domain)}` : ""}`;

  const { data, reload } = useResource<Overview>(`/api/v1/analytics/${query}`, 60000);
  const { data: clients } = useResource<{
    clients: Client[];
    truncated_to_retention: boolean;
    retention_days: number;
  }>(canSeeClients ? `/api/v1/analytics/clients/${query}&limit=50` : null, 60000);
  const { data: geography } = useResource<{
    countries: { country: string; country_name: string; requests: number; clients: number }[];
    networks: { asn: number; asn_name: string; country: string; requests: number }[];
    unlocated_requests: number;
    attribution: string;
  }>(`/api/v1/analytics/geography/${query}`, 60000);
  const { data: paths } = useResource<{
    busiest: PathRow[];
    slowest: PathRow[];
    failing: PathRow[];
    retention_days: number;
  }>(`/api/v1/analytics/paths/${query}`, 60000);
  const { data: heat } = useResource<{ cells: HeatCell[]; peak: number }>(
    `/api/v1/analytics/heatmap/?days=${Math.max(days, 28)}${
      domain ? `&domain=${encodeURIComponent(domain)}` : ""
    }`,
    60000,
  );

  async function fetchDatabases() {
    await api.post("/api/v1/analytics/geoip/");
    toast("Downloading the geolocation databases. New traffic will be located.");
    window.setTimeout(reload, 4000);
  }

  if (!data) return <div style={{ color: "var(--text-faint)" }}>Loading…</div>;

  const { summary, agents, geoip } = data;
  const nothingYet = summary.requests === 0;
  const rows = paths ? paths[pathView] : [];
  const busiest = Math.max(...(rows ?? []).map((r) => r.requests), 1);

  return (
    <>
      <header className="page-head">
        <div>
          <h1>Traffic</h1>
          <p>
            Read from the access logs, which are the only place a client
            address, a path or a country exists.
          </p>
        </div>
        <div className="series-controls">
          <select
            className="select select--compact"
            value={domain}
            onChange={(e) => setDomain(e.target.value)}
            aria-label="Domain"
          >
            <option value="">All domains</option>
            {data.domains.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
          <select
            className="select select--compact"
            value={days}
            onChange={(e) => setDays(Number(e.target.value))}
            aria-label="Time range"
          >
            {data.ranges.map((range) => (
              <option key={range.days} value={range.days}>
                {range.label}
              </option>
            ))}
          </select>
        </div>
      </header>

      {nothingYet && (
        <Notice tone="warn">
          Nothing has been read yet. Logs are folded in every few minutes;
          if this stays empty, check that the control plane can see
          /var/log/nginx and that a domain has served a request.
        </Notice>
      )}

      {!geoip.any && (
        <Notice tone="info">
          <span>
            No geolocation database, so country and network are blank.
            Everything else works without one.
            {isAdmin && (
              <>
                {" "}
                <button className="btn btn--small" onClick={fetchDatabases}>
                  Download them
                </button>
              </>
            )}
          </span>
        </Notice>
      )}

      <div className="grid grid--4" style={{ margin: "16px 0" }}>
        <Panel title="Requests" hint={`Last ${summary.days} days`}>
          <div className="stage__value" style={{ fontSize: "var(--step-4)" }}>
            {compact(summary.requests)}
          </div>
          <p className="stat__note">{bytes(summary.bytes_sent)} served</p>
        </Panel>

        <Panel title="Errors" hint="4xx and 5xx">
          <div
            className="stage__value"
            style={{
              fontSize: "var(--step-4)",
              color: (summary.error_rate ?? 0) > 5 ? "var(--down)" : undefined,
            }}
          >
            {percent(summary.error_rate, 1)}
          </div>
          <p className="stat__note">{compact(summary.errors)} responses</p>
        </Panel>

        <Panel title="Clients" hint={`Addresses kept ${summary.client_days} days`}>
          <div className="stage__value" style={{ fontSize: "var(--step-4)" }}>
            {compact(summary.distinct_clients)}
          </div>
          <p className="stat__note">
            {summary.countries} countries · {summary.networks} networks
          </p>
        </Panel>

        <Panel title="Response time" hint="Mean, as the gateway saw it">
          <div className="stage__value" style={{ fontSize: "var(--step-4)" }}>
            {ms(summary.average_ms, 0)}
          </div>
          <p className="stat__note">
            {summary.ingest.last_read_at
              ? `logs read ${relativeTime(summary.ingest.last_read_at)}`
              : "logs not read yet"}
          </p>
        </Panel>
      </div>

      <Panel
        title="When the traffic arrives"
        hint="Averaged per hour, so a longer window is comparable rather than darker"
        actions={
          <select
            className="select select--compact"
            value={heatMetric}
            onChange={(e) =>
              setHeatMetric(e.target.value as "average_requests" | "errors")
            }
            aria-label="Heatmap metric"
          >
            <option value="average_requests">Requests</option>
            <option value="errors">Errors</option>
          </select>
        }
      >
        {heat && heat.cells.length > 0 ? (
          <Heatmap cells={heat.cells} peak={heat.peak} metric={heatMetric} />
        ) : (
          <Empty
            title="Not enough history"
            body="The grid fills in as the days pass. A week is enough to see a shape."
          />
        )}
      </Panel>

      {canSeeClients && (
        <Panel
          title="Busiest clients"
          hint={
            clients?.truncated_to_retention
              ? `Addresses are kept ${clients.retention_days} days, so this window is capped at that`
              : "Complete addresses, which is why this page is not open to viewers"
          }
          flush
        >
          {!clients || clients.clients.length === 0 ? (
            <Empty title="No clients recorded" body="Nothing has reached a configured domain yet." />
          ) : (
            <div className="rows">
              {clients.clients.map((client) => (
                <Row
                  key={client.ip}
                  state={
                    (client.error_rate ?? 0) > 25
                      ? "down"
                      : (client.error_rate ?? 0) > 5
                        ? "degraded"
                        : "healthy"
                  }
                  columns="minmax(0,1fr) auto auto auto"
                >
                  <div>
                    <div className="row__primary mono">{client.ip}</div>
                    <div className="row__secondary">
                      {client.country ? (
                        <>
                          {flag(client.country)} {client.city || client.country_name}
                        </>
                      ) : (
                        "location unknown"
                      )}
                      {client.asn_name && ` · ${client.asn_name}`}
                      {client.asn && ` (AS${client.asn})`}
                      {` · last seen ${relativeTime(client.last_seen)}`}
                    </div>
                  </div>
                  <div className="row__num" title="Requests">
                    {compact(client.requests)}
                  </div>
                  <div className="row__num" title="Average response time">
                    {ms(client.average_ms, 0)}
                  </div>
                  <div style={{ display: "flex", gap: 6 }}>
                    {client.server_errors > 0 && (
                      <Tag tone="down">{compact(client.server_errors)} 5xx</Tag>
                    )}
                    {client.errors > 0 && client.server_errors === 0 && (
                      <Tag tone="warn">{percent(client.error_rate, 0)} errors</Tag>
                    )}
                    {client.domains > 1 && <Tag tone="signal">{client.domains} domains</Tag>}
                  </div>
                </Row>
              ))}
            </div>
          )}
        </Panel>
      )}

      <Panel
        title="What is being asked for"
        hint={
          pathView === "slowest"
            ? "Ranked by time per request, not total — and paths seen fewer than five times are left out"
            : pathView === "failing"
              ? "Paths returning 4xx or 5xx"
              : "Numeric and identifier segments are collapsed, so /users/{n} is one row"
        }
        actions={
          <div className="series-controls">
            {(["busiest", "slowest", "failing"] as const).map((view) => (
              <button
                key={view}
                className={`btn btn--small${pathView === view ? " btn--primary" : ""}`}
                onClick={() => setPathView(view)}
              >
                {view}
              </button>
            ))}
          </div>
        }
        flush
      >
        {rows.length === 0 ? (
          <Empty title="Nothing recorded" body="Paths appear once requests have been read." />
        ) : (
          <div className="rows">
            {rows.slice(0, 20).map((row) => (
              <Row
                key={row.path}
                state={
                  row.server_errors > 0
                    ? "down"
                    : (row.error_rate ?? 0) > 10
                      ? "degraded"
                      : "healthy"
                }
                columns="minmax(0,1fr) auto auto auto"
              >
                <div>
                  <div className="row__primary mono">{row.path}</div>
                  <div className="row__secondary">
                    <Bar share={(row.requests / busiest) * 100} />
                  </div>
                </div>
                <div className="row__num" title="Requests">
                  {compact(row.requests)}
                </div>
                <div className="row__num" title="Average · worst">
                  {ms(row.average_ms, 0)}
                  <span style={{ color: "var(--text-faint)" }}>
                    {" "}
                    / {ms(row.worst_ms, 0)}
                  </span>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  {row.cache_hit_ratio !== null && row.cache_hit_ratio > 0 && (
                    <Tag tone="hit">{percent(row.cache_hit_ratio, 0)} cached</Tag>
                  )}
                  {row.errors > 0 && (
                    <Tag tone={row.server_errors ? "down" : "warn"}>
                      {compact(row.errors)} errors
                    </Tag>
                  )}
                </div>
              </Row>
            ))}
          </div>
        )}
      </Panel>

      <div className="grid grid--2" style={{ marginBottom: 16 }}>
        <Panel title="Where it comes from" hint="By country" flush>
          {!geography || geography.countries.length === 0 ? (
            <Empty
              title="No locations yet"
              body={
                geoip.any
                  ? "Traffic so far is from addresses the database does not place, which usually means a private network."
                  : "No geolocation database is installed."
              }
            />
          ) : (
            <div className="rows">
              {geography.countries.slice(0, 10).map((country) => (
                <Row key={country.country} state="healthy" columns="minmax(0,1fr) auto auto">
                  <div>
                    <div className="row__primary">
                      {flag(country.country)} {country.country_name || country.country}
                    </div>
                    <div className="row__secondary">
                      <Bar
                        share={
                          (country.requests / (geography.countries[0]?.requests || 1)) * 100
                        }
                      />
                    </div>
                  </div>
                  <div className="row__num">{compact(country.requests)}</div>
                  <div className="row__num" title="Distinct clients">
                    {compact(country.clients)}
                  </div>
                </Row>
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Which networks" hint="By autonomous system" flush>
          {!geography || geography.networks.length === 0 ? (
            <Empty
              title="No networks yet"
              body="An ASN database places traffic at the institution rather than the country, which is usually the more useful answer."
            />
          ) : (
            <div className="rows">
              {geography.networks.slice(0, 10).map((network) => (
                <Row key={network.asn} state="healthy" columns="minmax(0,1fr) auto">
                  <div>
                    <div className="row__primary">
                      {network.asn_name || `AS${network.asn}`}
                    </div>
                    <div className="row__secondary">
                      {flag(network.country)} AS{network.asn}
                    </div>
                  </div>
                  <div className="row__num">{compact(network.requests)}</div>
                </Row>
              ))}
            </div>
          )}
        </Panel>
      </div>

      <div className="grid grid--2" style={{ marginBottom: 16 }}>
        <Panel title="What kind of client" hint="A spike of crawlers is not a spike of users">
          {agents.total === 0 ? (
            <Empty title="Nothing yet" body="Client types appear once requests have been read." />
          ) : (
            <div className="rows">
              {agents.kinds.map((kind) => (
                <Row key={kind.kind} state="healthy" columns="minmax(0,1fr) auto auto">
                  <div>
                    <div className="row__primary">{kind.label}</div>
                    <div className="row__secondary">
                      <Bar share={kind.share} />
                    </div>
                  </div>
                  <div className="row__num">{compact(kind.requests)}</div>
                  <div className="row__num">{percent(kind.share, 0)}</div>
                </Row>
              ))}
            </div>
          )}
        </Panel>

        <Panel title="Where these numbers come from">
          <Outcome>
            Nginx's metrics aggregate per domain and per upstream. They cannot
            say who made a request, what it asked for, or where it came from —
            so this page is folded out of the access logs instead, every few
            minutes, and never stores a raw line.
          </Outcome>
          <div className="rows" style={{ marginTop: 10 }}>
            <Row state="healthy" columns="minmax(0,1fr) auto">
              <div style={{ fontSize: "0.85rem" }}>Log files read</div>
              <div className="row__num">{summary.ingest.files}</div>
            </Row>
            <Row state="healthy" columns="minmax(0,1fr) auto">
              <div style={{ fontSize: "0.85rem" }}>Lines folded in</div>
              <div className="row__num">{compact(summary.ingest.lines)}</div>
            </Row>
            <Row
              state={summary.ingest.unparsed > 0 ? "degraded" : "healthy"}
              columns="minmax(0,1fr) auto"
            >
              <div style={{ fontSize: "0.85rem" }}>Lines not understood</div>
              <div className="row__num">{compact(summary.ingest.unparsed)}</div>
            </Row>
            {geography && geography.unlocated_requests > 0 && (
              <Row state="degraded" columns="minmax(0,1fr) auto">
                <div style={{ fontSize: "0.85rem" }}>Requests with no location</div>
                <div className="row__num">{compact(geography.unlocated_requests)}</div>
              </Row>
            )}
          </div>
          {geoip.any && geography?.attribution && (
            <p className="stat__note" style={{ marginTop: 10 }}>
              {geography.attribution}
            </p>
          )}
        </Panel>
      </div>
    </>
  );
}
