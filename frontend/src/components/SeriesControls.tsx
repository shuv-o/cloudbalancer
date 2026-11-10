/**
 * The domain and range pickers that sit above every chart.
 *
 * Both choices belong to the charts and nothing else on the page: the live
 * counters beside them are instantaneous and have no range to pick, and
 * narrowing them to one domain would contradict the totals printed next to
 * them. So this owns the two values and the URL they produce, and the page
 * owns nothing but where to put the control.
 */
import { useMemo, useState } from "react";
import { useResource } from "../lib/hooks";

interface Range {
  label: string;
  minutes: number;
}

interface Chartable {
  domains: string[];
  ranges: Range[];
}

export interface SeriesSelection {
  /** Empty string means every domain, which is not the same as none. */
  domain: string;
  minutes: number;
  /** The path to fetch, already encoded. */
  path: string;
}

/**
 * The ranges to offer before the server has said which it supports.
 *
 * The server's list is authoritative, because it knows how long Prometheus
 * retains data and must not offer a year it no longer holds. This is only so
 * the control is not empty on first paint.
 */
const FALLBACK_RANGES: Range[] = [
  { label: "1 hour", minutes: 60 },
  { label: "24 hours", minutes: 60 * 24 },
  { label: "30 days", minutes: 60 * 24 * 30 },
];

export function useSeriesSelection(defaultMinutes = 60): {
  selection: SeriesSelection;
  controls: React.ReactNode;
} {
  const { data } = useResource<Chartable>("/api/v1/monitoring/series/domains/");
  const [domain, setDomain] = useState("");
  const [minutes, setMinutes] = useState(defaultMinutes);

  const ranges = data?.ranges?.length ? data.ranges : FALLBACK_RANGES;
  const domains = data?.domains ?? [];

  const path = useMemo(() => {
    const params = new URLSearchParams({ minutes: String(minutes) });
    if (domain) params.set("domain", domain);
    return `/api/v1/monitoring/series/?${params}`;
  }, [domain, minutes]);

  const controls = (
    <div className="series-controls">
      <select
        className="select select--compact"
        value={domain}
        onChange={(event) => setDomain(event.target.value)}
        aria-label="Domain"
      >
        <option value="">All domains</option>
        {domains.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>

      <select
        className="select select--compact"
        value={minutes}
        onChange={(event) => setMinutes(Number(event.target.value))}
        aria-label="Time range"
      >
        {ranges.map((range) => (
          <option key={range.minutes} value={range.minutes}>
            {range.label}
          </option>
        ))}
      </select>
    </div>
  );

  return { selection: { domain, minutes, path }, controls };
}

/**
 * What the charts below are actually showing.
 *
 * Worth stating rather than assuming. A year-long chart is drawn at one point
 * per day from a four-day rate window, and a spike that lasted an afternoon
 * will not be visible in it. Someone reading a flat line needs to know that
 * before concluding the afternoon was quiet.
 */
export function SeriesCaption({
  step,
  window: rateWindow,
  domain,
  upstreams,
}: {
  step?: string;
  window?: string;
  domain?: string;
  upstreams?: string[];
}) {
  if (!step) return null;

  const scope = domain ? domain : "every domain";
  const serving =
    domain && upstreams
      ? upstreams.length
        ? ` · via ${upstreams.join(", ")}`
        : " · no active route, so nothing upstream to chart"
      : "";

  return (
    <p className="series-caption">
      {scope} · one point per {step}, averaged over {rateWindow}
      {serving}
    </p>
  );
}
