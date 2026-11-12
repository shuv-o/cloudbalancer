/**
 * Requests by weekday and hour, as a grid.
 *
 * The question it answers is one a line chart cannot: not "how much traffic"
 * but "when". A week of line chart shows seven peaks and leaves you counting
 * them; the same data as a grid shows at a glance that Tuesday afternoon is
 * the busy window and that something runs at 03:00 on Sundays.
 *
 * Colour is on a square-root scale rather than linear. Traffic is heavily
 * skewed — one peak hour is often ten times the median — and a linear ramp
 * renders everything except the peak as the same near-empty shade.
 */
import { Fragment } from "react";

import { compact } from "../lib/format";

export interface HeatCell {
  weekday: number;
  hour: number;
  requests: number;
  average_requests: number;
  errors: number;
  average_ms: number | null;
}

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function Heatmap({
  cells,
  peak,
  metric = "average_requests",
}: {
  cells: HeatCell[];
  peak: number;
  metric?: "average_requests" | "errors";
}) {
  const byKey = new Map(cells.map((cell) => [`${cell.weekday}:${cell.hour}`, cell]));

  const ceiling =
    metric === "errors"
      ? Math.max(...cells.map((c) => c.errors), 1)
      : Math.max(peak, 1);

  function intensity(cell: HeatCell | undefined): number {
    if (!cell) return 0;
    const value = metric === "errors" ? cell.errors : cell.average_requests;
    if (value <= 0) return 0;
    // Square root, because traffic is skewed and a linear ramp renders
    // everything but the peak as the same empty shade.
    return Math.min(Math.sqrt(value / ceiling), 1);
  }

  return (
    <div className="heatmap">
      <div className="heatmap__grid">
        <div className="heatmap__corner" />
        {Array.from({ length: 24 }, (_, hour) => (
          <div key={hour} className="heatmap__hour">
            {hour % 3 === 0 ? hour : ""}
          </div>
        ))}

        {DAYS.map((label, weekday) => (
          // A keyed Fragment, not <>: each iteration emits a row label and
          // twenty-four cells as siblings of the grid, and React needs the
          // group itself identified or it rebuilds every cell on each render.
          <Fragment key={label}>
            <div className="heatmap__day">{label}</div>
            {Array.from({ length: 24 }, (_, hour) => {
              const cell = byKey.get(`${weekday}:${hour}`);
              const level = intensity(cell);
              return (
                <div
                  key={`${weekday}-${hour}`}
                  className="heatmap__cell"
                  style={{
                    // A faint floor so an hour with no traffic is still a
                    // cell rather than a hole in the grid.
                    background:
                      level === 0
                        ? "var(--ink-900)"
                        : `color-mix(in srgb, var(${
                            metric === "errors" ? "--down" : "--signal"
                          }) ${Math.round(level * 100)}%, var(--ink-900))`,
                  }}
                  title={
                    cell
                      ? `${DAYS[weekday]} ${String(hour).padStart(2, "0")}:00 — ` +
                        `${compact(cell.average_requests)} req/h avg` +
                        (cell.errors ? `, ${compact(cell.errors)} errors` : "") +
                        (cell.average_ms !== null ? `, ${cell.average_ms}ms` : "")
                      : `${DAYS[weekday]} ${String(hour).padStart(2, "0")}:00 — nothing`
                  }
                />
              );
            })}
          </Fragment>
        ))}
      </div>

      <div className="heatmap__scale">
        <span>less</span>
        {[0, 0.25, 0.5, 0.75, 1].map((level) => (
          <span
            key={level}
            className="heatmap__swatch"
            style={{
              background:
                level === 0
                  ? "var(--ink-900)"
                  : `color-mix(in srgb, var(${
                      metric === "errors" ? "--down" : "--signal"
                    }) ${Math.round(level * 100)}%, var(--ink-900))`,
            }}
          />
        ))}
        <span>more</span>
        <span className="heatmap__peak">
          peak {compact(ceiling)} {metric === "errors" ? "errors" : "req/h"}
        </span>
      </div>
    </div>
  );
}
