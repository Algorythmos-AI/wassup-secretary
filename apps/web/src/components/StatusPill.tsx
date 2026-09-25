import type { WorkflowStatus } from "../api/types";
import { STATUS_LABEL } from "../lib/format";

export function StatusPill({ status }: { status: WorkflowStatus }) {
  return <span className={`pill pill--${status}`}>{STATUS_LABEL[status]}</span>;
}
