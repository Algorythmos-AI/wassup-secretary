import { useEffect, useState } from "react";
import { useOutletContext } from "react-router";
import type { AnalyticsSummary, ClinicAccess } from "../api/types";
import { canSeeUsage, useSession } from "../auth/session";
import { BarChart, RankBars, type Bar } from "../components/BarChart";
import { STATUS_LABEL, humanizeIntent } from "../lib/format";
import { addDays, formatDayLabel, formatDuration, formatHour, formatLongDate, todayIn } from "../lib/time";
import "../styles/analytics.css";

const RANGES = [
  { days: 7, label: "Last 7 days" },
  { days: 30, label: "Last 30 days" },
  { days: 90, label: "Last 90 days" },
];
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

export function Analytics() {
  const clinic = useOutletContext<ClinicAccess>();
  const { api } = useSession();
  const [days, setDays] = useState(30);
  const [data, setData] = useState<AnalyticsSummary | null>(null);
  const [error, setError] = useState<string | null>(null);
  const today = todayIn(clinic.timezone);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    api
      .analytics(clinic.id, { from: addDays(today, -(days - 1)), to: today })
      .then((d) => !cancelled && (setData(d), setError(null)))
      .catch(() => !cancelled && setError("Couldn't load analytics. Try again in a moment."));
    return () => {
      cancelled = true;
    };
  }, [api, clinic.id, days, today]);

  const byDay: Bar[] =
    data?.by_day.map((d, i, all) => ({
      key: d.date,
      title: formatLongDate(d.date),
      label: all.length <= 10 || i % Math.ceil(all.length / 8) === 0 ? formatDayLabel(d.date) : "",
      value: d.calls,
    })) ?? [];
  const byHour: Bar[] =
    data?.by_hour.map((h) => ({ key: String(h.hour), title: `${formatHour(h.hour)} to ${formatHour((h.hour + 1) % 24)}`, label: h.hour % 3 === 0 ? formatHour(h.hour) : "", value: h.calls })) ?? [];
  const byWeekday: Bar[] =
    data?.by_weekday.map((w) => ({ key: String(w.weekday), title: WEEKDAYS[w.weekday]!, label: WEEKDAYS[w.weekday]!, value: w.calls })) ?? [];

  return (
    <div className="analytics">
      <header className="page-header">
        <h1>Analytics</h1>
        <div className="tabs" role="tablist" aria-label="Period">
          {RANGES.map((r) => (
            <button key={r.days} role="tab" type="button" aria-selected={days === r.days} onClick={() => setDays(r.days)}>
              {r.label}
            </button>
          ))}
        </div>
        {data && (
          <span className="analytics__range">
            {formatLongDate(data.from)} to {formatLongDate(data.to)}, {clinic.name} time
          </span>
        )}
      </header>
      {error && <p className="notice notice--error">{error}</p>}
      {!data && !error && <p className="analytics__loading">Loading…</p>}
      {data && (
        <>
          <section className="tiles" aria-label="Totals">
            <div className="tile">
              <span className="tile__value">{data.totals.calls}</span>
              <span className="tile__label">calls answered</span>
            </div>
            <div className="tile">
              <span className="tile__value">{formatDuration(data.totals.avg_duration_seconds) || "–"}</span>
              <span className="tile__label">average call</span>
            </div>
            <div className={(data.workflow.pending ?? 0) + (data.workflow.following_up ?? 0) > 0 ? "tile tile--todo" : "tile"}>
              <span className="tile__value">{(data.workflow.pending ?? 0) + (data.workflow.following_up ?? 0)}</span>
              <span className="tile__label">still to do</span>
            </div>
            <div className={data.totals.priority > 0 ? "tile tile--priority" : "tile"}>
              <span className="tile__value">{data.totals.priority}</span>
              <span className="tile__label">marked priority</span>
            </div>
            {canSeeUsage(clinic.role) && (
              <div className="tile">
                <span className="tile__value">US${data.totals.cost_usd}</span>
                <span className="tile__label">voice cost</span>
              </div>
            )}
          </section>

          <div className="analytics__grid">
            <BarChart bars={byDay} caption="Calls each day" valueLabel="calls" />
            <BarChart bars={byHour} caption="When calls come in (clinic time)" valueLabel="calls" />
            <BarChart bars={byWeekday} caption="Calls by day of the week" valueLabel="calls" height={160} />
            <RankBars
              caption="What callers wanted"
              valueLabel="calls"
              rows={data.top_intents.map((i) => ({ label: humanizeIntent(i.intent), value: i.calls }))}
            />
            <RankBars
              caption="How callers sounded"
              valueLabel="calls"
              rows={Object.entries(data.sentiment)
                .sort(([, a], [, b]) => b - a)
                .map(([k, v]) => ({ label: k === "unknown" ? "Not assessed" : k[0]!.toUpperCase() + k.slice(1), value: v }))}
            />
            <RankBars
              caption="Where follow-ups stand"
              valueLabel="calls"
              rows={(Object.keys(STATUS_LABEL) as (keyof typeof STATUS_LABEL)[]).map((s) => ({ label: STATUS_LABEL[s], value: data.workflow[s] ?? 0 }))}
            />
          </div>
        </>
      )}
    </div>
  );
}
