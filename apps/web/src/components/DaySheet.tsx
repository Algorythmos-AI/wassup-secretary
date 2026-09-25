import type { CallSummary } from "../api/types";
import { buildDaySheet } from "../lib/daysheet";
import { OPEN_STATUSES, formatPhone, humanizeIntent } from "../lib/format";
import { formatDayLabel, formatHour, formatTime, todayIn } from "../lib/time";
import { StatusPill } from "./StatusPill";

interface Props {
  calls: readonly CallSummary[];
  timeZone: string;
  selectedId: string | null;
  freshIds: ReadonlySet<string>;
  onSelect: (id: string) => void;
}

/** Calls laid out like an appointment book: a clinic-local hour gutter, one ruled row per call. */
export function DaySheet({ calls, timeZone, selectedId, freshIds, onSelect }: Props) {
  const today = todayIn(timeZone);
  return (
    <div className="sheet">
      {buildDaySheet(calls, timeZone).map((day) => (
        <section className="sheet__day" key={day.date ?? "unknown"} aria-label={day.date ? formatDayLabel(day.date, today) : "Time unknown"}>
          <h2 className="sheet__date">{day.date ? formatDayLabel(day.date, today) : "Time unknown"}</h2>
          {day.hours.map((slot) => (
            <div className="sheet__hour" key={slot.hour}>
              <div className="sheet__gutter" aria-hidden="true">
                {slot.hour >= 0 ? formatHour(slot.hour) : ""}
              </div>
              <ol className="sheet__calls">
                {slot.calls.map((call) => {
                  const open = OPEN_STATUSES.has(call.workflow_status);
                  const classes = [
                    "call",
                    open ? "call--open" : "call--closed",
                    call.is_priority || call.has_urgent_message ? "call--priority" : "",
                    call.id === selectedId ? "call--selected" : "",
                    freshIds.has(call.id) ? "call--fresh" : "",
                  ].join(" ");
                  return (
                    <li key={call.id}>
                      <button type="button" className={classes} onClick={() => onSelect(call.id)} aria-current={call.id === selectedId}>
                        <span className="call__time">{call.started_at ? formatTime(call.started_at, timeZone) : "—"}</span>
                        <span className="call__main">
                          <span className="call__title">
                            {call.action_label ?? humanizeIntent(call.intent)}
                            {call.has_urgent_message && <span className="call__flag call__flag--urgent">Urgent message</span>}
                            {call.is_priority && <span className="call__flag">Priority</span>}
                          </span>
                          <span className="call__caller">{formatPhone(call.from_number)}</span>
                          {call.summary && <span className="call__summary">{call.summary}</span>}
                        </span>
                        <StatusPill status={call.workflow_status} />
                      </button>
                    </li>
                  );
                })}
              </ol>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}
