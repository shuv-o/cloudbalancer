import { CacheStats, HealthGridBackend, TrafficStats, DashboardSummary } from "../lib/types";
import { compact, ms, percent } from "../lib/format";

/**
 * The hero.
 *
 * A gateway is a pipeline: a request arrives, a rule picks a backend, the
 * cache either answers it or does not, and an upstream instance serves what is
 * left. Four separate stat tiles would carry the same four numbers and throw
 * away the thing that makes them readable — that each stage feeds the next,
 * and that a number looking wrong at one stage is usually explained by the
 * stage before it.
 *
 * The wire is drawn, not implied. It traces once on load and then holds still.
 */

function Bars({ values }: { values: number[] }) {
  const max = Math.max(...values, 1);
  return (
    <div className="bars" aria-hidden="true">
      {values.map((value, index) => (
        <div
          key={index}
          className={index === values.length - 1 ? "bars__bar bars__bar--now" : "bars__bar"}
          style={{ height: `${Math.max((value / max) * 100, 6)}%` }}
        />
      ))}
    </div>
  );
}

function Meter({ ratio }: { ratio: number | null }) {
  const filled = ratio === null ? 0 : Math.round((ratio / 100) * 8);
  return (
    <div className="meter" aria-hidden="true">
      {Array.from({ length: 8 }, (_, index) => (
        <div
          key={index}
          className={index < filled ? "meter__seg meter__seg--on" : "meter__seg"}
          style={{ height: `${10 + index * 2}px` }}
        />
      ))}
    </div>
  );
}

function Dots({ health }: { health: HealthGridBackend[] }) {
  const instances = health.flatMap((backend) => backend.instances);
  if (instances.length === 0) return <div className="dots" />;

  return (
    <div className="dots" aria-hidden="true">
      {instances.slice(0, 24).map((instance) => (
        <span
          key={instance.id}
          className={
            instance.state === "healthy" ? "dot" : `dot dot--${instance.state}`
          }
          title={`${instance.address} — ${instance.state}`}
        />
      ))}
    </div>
  );
}

function Stage({
  label,
  value,
  unit,
  note,
  live,
  children,
}: {
  label: string;
  value: string;
  unit?: string;
  note: string;
  live: boolean;
  children?: React.ReactNode;
}) {
  return (
    <div className={live ? "stage stage--live" : "stage"}>
      <div className="stage__label">{label}</div>
      <div className="stage__node" />
      <div className={value === "—" ? "stage__value stage__value--empty" : "stage__value"}>
        {value === "—" ? "no reading" : value}
        {unit && value !== "—" && <span className="stage__unit">{unit}</span>}
      </div>
      <div className="stage__note">{note}</div>
      {children}
    </div>
  );
}

export function SignalPath({
  summary,
  traffic,
  cache,
  health,
  history,
}: {
  summary: DashboardSummary;
  traffic: TrafficStats;
  cache: CacheStats;
  health: HealthGridBackend[];
  history: number[];
}) {
  const live = traffic.available && traffic.total_requests > 0;

  const requests = traffic.available ? traffic.total_requests : null;
  const activeConnections = traffic.connections?.active ?? 0;

  const instances = health.flatMap((backend) => backend.instances);
  const serving = instances.filter((i) => i.state === "healthy").length;

  const probeMs = summary.backends.avg_probe_ms;

  return (
    <div className="signal">
      <div className="signal__track">
        <div className="signal__wire" />
        {live && <div className="signal__pulse" />}

        <Stage
          label="Arriving"
          value={requests === null ? "—" : compact(requests)}
          note={
            traffic.available
              ? `${activeConnections} connections open`
              : "Traffic counters unavailable"
          }
          live={live}
        >
          <Bars values={history.length ? history : [0, 0, 0, 0, 0, 0, 0, 0]} />
        </Stage>

        <Stage
          label="Routed"
          value={String(summary.routing.active_rules)}
          unit={summary.routing.active_rules === 1 ? "rule" : "rules"}
          note={`across ${summary.domains.active} ${
            summary.domains.active === 1 ? "domain" : "domains"
          }`}
          live={summary.routing.active_rules > 0}
        >
          <div className="bars" aria-hidden="true">
            {Array.from({ length: Math.min(summary.routing.active_rules, 10) }, (_, i) => (
              <div key={i} className="bars__bar" style={{ height: "40%" }} />
            ))}
          </div>
        </Stage>

        <Stage
          label="Cached"
          value={cache.available ? percent(cache.hit_ratio, 0) : "—"}
          note={
            cache.available && cache.hit_ratio !== null
              ? `${compact(cache.served_from_cache)} served without touching a backend`
              : `${summary.routing.cached_rules} routes cache`
          }
          live={Boolean(cache.available && cache.hit_ratio)}
        >
          <Meter ratio={cache.available ? cache.hit_ratio : null} />
        </Stage>

        <Stage
          label="Upstream"
          value={String(serving)}
          unit={`/ ${instances.length}`}
          note={probeMs !== null ? `${ms(probeMs, 1)} to answer a probe` : "no probes yet"}
          live={serving > 0}
        >
          <Dots health={health} />
        </Stage>
      </div>
    </div>
  );
}
