import { useEffect, useState } from "react";
import { Navigate, useOutletContext } from "react-router";
import type { ClinicAccess, UsageReport } from "../api/types";
import { canSeeUsage, useSession } from "../auth/session";
import { addDays, formatLongDate, todayIn } from "../lib/time";
import "../styles/analytics.css";

function monthRange(today: string, monthsBack: number): { from: string; to: string; label: string } {
  const [y, m] = today.split("-").map(Number) as [number, number];
  const first = new Date(Date.UTC(y, m - 1 - monthsBack, 1));
  const from = first.toISOString().slice(0, 10);
  const nextFirst = new Date(Date.UTC(first.getUTCFullYear(), first.getUTCMonth() + 1, 1)).toISOString().slice(0, 10);
  const to = monthsBack === 0 ? today : addDays(nextFirst, -1);
  const label = new Intl.DateTimeFormat("en-AU", { month: "long", year: "numeric", timeZone: "UTC" }).format(first);
  return { from, to, label };
}

export function Usage() {
  const clinic = useOutletContext<ClinicAccess>();
  const { api } = useSession();
  const today = todayIn(clinic.timezone);
  const [monthsBack, setMonthsBack] = useState(0);
  const [data, setData] = useState<UsageReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const range = monthRange(today, monthsBack);

  const allowed = canSeeUsage(clinic.role);

  useEffect(() => {
    if (!allowed) return;
    let cancelled = false;
    setData(null);
    api
      .usage(clinic.id, { from: range.from, to: range.to })
      .then((d) => !cancelled && (setData(d), setError(null)))
      .catch(() => !cancelled && setError("Couldn't load usage. Try again in a moment."));
    return () => {
      cancelled = true;
    };
  }, [allowed, api, clinic.id, range.from, range.to]);

  if (!allowed) return <Navigate to={`/c/${clinic.id}/inbox`} replace />;

  return (
    <div className="usage">
      <header className="page-header">
        <h1>Usage</h1>
        <div className="tabs" role="tablist" aria-label="Month">
          {[0, 1, 2].map((back) => (
            <button key={back} role="tab" type="button" aria-selected={monthsBack === back} onClick={() => setMonthsBack(back)}>
              {monthRange(today, back).label}
            </button>
          ))}
        </div>
      </header>
      <p className="analytics__range">
        Calls answered by the AI receptionist and their voice cost, by day in {clinic.name} time. Updated hourly.
      </p>
      {error && <p className="notice notice--error">{error}</p>}
      {data && (
        <>
          <section className="tiles" aria-label="Month totals">
            <div className="tile">
              <span className="tile__value">{data.totals.calls}</span>
              <span className="tile__label">calls</span>
            </div>
            <div className="tile">
              <span className="tile__value">{data.totals.minutes}</span>
              <span className="tile__label">minutes</span>
            </div>
            <div className="tile">
              <span className="tile__value">US${data.totals.provider_cost_usd}</span>
              <span className="tile__label">voice cost</span>
            </div>
          </section>
          {data.days.length === 0 ? (
            <p className="analytics__range">No calls in {range.label}.</p>
          ) : (
            <table className="usage__table">
              <thead>
                <tr>
                  <th scope="col">Day</th>
                  <th scope="col">Calls</th>
                  <th scope="col">Minutes</th>
                  <th scope="col">Voice cost</th>
                </tr>
              </thead>
              <tbody>
                {data.days.map((d) => (
                  <tr key={d.date}>
                    <td>{formatLongDate(d.date)}</td>
                    <td>{d.calls}</td>
                    <td>{d.minutes}</td>
                    <td>US${d.provider_cost_usd}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
