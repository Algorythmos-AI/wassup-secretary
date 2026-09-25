/** Single-series vertical bars: one hue, 4px rounded data-ends on the baseline, 2px gaps, a
 * recessive grid, a hover/focus tooltip per bar, and a table view for every chart. */
import { useId, useState } from "react";

export interface Bar {
  key: string;
  label: string; // axis label (may be blank to thin the axis)
  title: string; // full label for tooltip and table
  value: number;
}

interface Props {
  bars: Bar[];
  caption: string;
  valueLabel: string; // e.g. "calls"
  height?: number;
}

function niceMax(value: number): number {
  if (value <= 4) return Math.max(1, value);
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => value <= s * 4)!;
  return Math.ceil(value / step) * step;
}

export function BarChart({ bars, caption, valueLabel, height = 180 }: Props) {
  const id = useId();
  const [hover, setHover] = useState<number | null>(null);
  const width = 640;
  const pad = { top: 12, right: 8, bottom: 26, left: 34 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;
  const max = niceMax(Math.max(0, ...bars.map((b) => b.value)));
  const band = plotW / Math.max(1, bars.length);
  const barW = Math.max(2, Math.min(28, band - 2));
  const y = (v: number) => pad.top + plotH - (v / max) * plotH;
  const ticks = [0, max / 2, max];
  const active = hover == null ? null : bars[hover];

  return (
    <figure className="chart">
      <figcaption className="chart__caption" id={`${id}-cap`}>
        {caption}
      </figcaption>
      <div className="chart__plot">
        <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-labelledby={`${id}-cap`} preserveAspectRatio="none">
          {ticks.map((t) => (
            <g key={t}>
              <line className="chart__grid" x1={pad.left} x2={width - pad.right} y1={y(t)} y2={y(t)} />
              <text className="chart__tick" x={pad.left - 6} y={y(t) + 4} textAnchor="end">
                {Number.isInteger(t) ? t : t.toFixed(1)}
              </text>
            </g>
          ))}
          {bars.map((b, i) => {
            const x = pad.left + i * band + (band - barW) / 2;
            const top = y(b.value);
            const h = pad.top + plotH - top;
            const r = Math.min(4, barW / 2, h);
            const path =
              h <= 0
                ? ""
                : `M${x},${pad.top + plotH} V${top + r} Q${x},${top} ${x + r},${top} H${x + barW - r} Q${x + barW},${top} ${x + barW},${top + r} V${pad.top + plotH} Z`;
            return (
              <g key={b.key}>
                {/* Hit target taller and wider than the mark. */}
                <rect
                  className="chart__hit"
                  x={pad.left + i * band}
                  y={pad.top}
                  width={band}
                  height={plotH}
                  tabIndex={0}
                  aria-label={`${b.title}: ${b.value} ${valueLabel}`}
                  onMouseEnter={() => setHover(i)}
                  onMouseLeave={() => setHover(null)}
                  onFocus={() => setHover(i)}
                  onBlur={() => setHover(null)}
                />
                {path && <path className={`chart__bar${hover === i ? " chart__bar--active" : ""}`} d={path} />}
                {b.label && (
                  <text className="chart__label" x={pad.left + i * band + band / 2} y={height - 8} textAnchor="middle">
                    {b.label}
                  </text>
                )}
              </g>
            );
          })}
        </svg>
        {active && (
          <div
            className="chart__tooltip"
            style={{ left: `${((pad.left + (hover! + 0.5) * band) / width) * 100}%` }}
            role="status"
          >
            <strong>{active.value}</strong> {valueLabel}
            <span>{active.title}</span>
          </div>
        )}
      </div>
      <details className="chart__table">
        <summary>Show as a table</summary>
        <table>
          <thead>
            <tr>
              <th scope="col">Period</th>
              <th scope="col">{valueLabel[0]!.toUpperCase() + valueLabel.slice(1)}</th>
            </tr>
          </thead>
          <tbody>
            {bars.map((b) => (
              <tr key={b.key}>
                <td>{b.title}</td>
                <td>{b.value}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </figure>
  );
}

/** Ranked horizontal bars in HTML (long labels stay readable; the label is the identity). */
export function RankBars({ rows, caption, valueLabel }: { rows: { label: string; value: number }[]; caption: string; valueLabel: string }) {
  const max = Math.max(1, ...rows.map((r) => r.value));
  return (
    <figure className="chart">
      <figcaption className="chart__caption">{caption}</figcaption>
      {rows.length === 0 ? (
        <p className="chart__empty">No calls in this period.</p>
      ) : (
        <ol className="rank">
          {rows.map((r) => (
            <li key={r.label} className="rank__row">
              <span className="rank__label">{r.label}</span>
              <span className="rank__track" aria-hidden="true">
                <span className="rank__bar" style={{ width: `${(r.value / max) * 100}%` }} />
              </span>
              <span className="rank__value">
                {r.value} <span className="visually-hidden">{valueLabel}</span>
              </span>
            </li>
          ))}
        </ol>
      )}
    </figure>
  );
}
