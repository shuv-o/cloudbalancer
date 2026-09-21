import { Series } from "../lib/types";

/**
 * A small time-series chart.
 *
 * Hand-drawn SVG rather than a charting library: an ops screen needs four
 * shapes, and the alternative is several hundred kilobytes of JavaScript to
 * draw a line on a page that is already polling every few seconds.
 */

const PALETTE = ["var(--signal)", "var(--cool)", "var(--hit)", "var(--warn)", "var(--down)"];

interface Props {
  series: Series[];
  height?: number;
  unit?: string;
  /** Draw a horizontal reference line, e.g. the one-millisecond budget. */
  threshold?: { value: number; label: string };
  emptyMessage?: string;
  formatValue?: (value: number) => string;
}

export function Chart({
  series,
  height = 170,
  unit = "",
  threshold,
  emptyMessage = "No data for this range yet.",
  formatValue,
}: Props) {
  const withPoints = series.filter((s) => s.points.some((p) => p.v !== null));

  if (withPoints.length === 0) {
    return <div className="chart__empty">{emptyMessage}</div>;
  }

  const allValues = withPoints.flatMap((s) =>
    s.points.map((p) => p.v).filter((v): v is number => v !== null),
  );
  const allTimes = withPoints.flatMap((s) => s.points.map((p) => p.t));

  const minT = Math.min(...allTimes);
  const maxT = Math.max(...allTimes);
  const maxV = Math.max(...allValues, threshold?.value ?? 0) * 1.15 || 1;

  const width = 700;
  const padLeft = 46;
  const padBottom = 20;
  const padTop = 8;
  const plotW = width - padLeft - 8;
  const plotH = height - padBottom - padTop;

  const x = (t: number) => padLeft + (maxT === minT ? 0 : ((t - minT) / (maxT - minT)) * plotW);
  const y = (v: number) => padTop + plotH - (v / maxV) * plotH;

  const format = formatValue ?? ((v: number) => (v >= 100 ? v.toFixed(0) : v.toFixed(2)));
  const gridLines = [0, 0.5, 1];

  return (
    <svg
      className="chart"
      viewBox={`0 0 ${width} ${height}`}
      height={height}
      preserveAspectRatio="none"
      role="img"
      aria-label={`Time series chart${unit ? ` in ${unit}` : ""}`}
    >
      {gridLines.map((fraction) => {
        const value = maxV * fraction;
        return (
          <g key={fraction}>
            <line
              className="chart__grid"
              x1={padLeft}
              x2={width - 8}
              y1={y(value)}
              y2={y(value)}
            />
            <text className="chart__axis" x={4} y={y(value) + 3}>
              {format(value)}
            </text>
          </g>
        );
      })}

      {threshold && threshold.value <= maxV && (
        <g>
          <line
            x1={padLeft}
            x2={width - 8}
            y1={y(threshold.value)}
            y2={y(threshold.value)}
            stroke="var(--down)"
            strokeWidth={1}
            strokeDasharray="4 4"
            opacity={0.65}
          />
          <text
            className="chart__axis"
            x={width - 10}
            y={y(threshold.value) - 5}
            textAnchor="end"
            fill="var(--down)"
          >
            {threshold.label}
          </text>
        </g>
      )}

      {withPoints.map((s, index) => {
        const color = PALETTE[index % PALETTE.length];
        const points = s.points.filter((p) => p.v !== null);
        const path = points
          .map((p, i) => `${i === 0 ? "M" : "L"}${x(p.t).toFixed(1)},${y(p.v!).toFixed(1)}`)
          .join(" ");
        const area = `${path} L${x(points[points.length - 1].t).toFixed(1)},${(
          padTop + plotH
        ).toFixed(1)} L${x(points[0].t).toFixed(1)},${(padTop + plotH).toFixed(1)} Z`;

        return (
          <g key={index}>
            {withPoints.length === 1 && <path d={area} fill={color} opacity={0.1} />}
            <path d={path} fill="none" stroke={color} strokeWidth={1.8} />
          </g>
        );
      })}
    </svg>
  );
}

export function ChartLegend({ series }: { series: Series[] }) {
  const named = series.filter((s) => Object.keys(s.labels).length > 0);
  if (named.length <= 1) return null;

  return (
    <div style={{ display: "flex", gap: 14, flexWrap: "wrap", marginTop: 10 }}>
      {named.map((s, index) => (
        <span
          key={index}
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            fontSize: "0.76rem",
            color: "var(--text-dim)",
          }}
        >
          <span
            style={{
              width: 10,
              height: 2,
              background: PALETTE[index % PALETTE.length],
              borderRadius: 1,
            }}
          />
          <span className="mono">{Object.values(s.labels).join(" ")}</span>
        </span>
      ))}
    </div>
  );
}
