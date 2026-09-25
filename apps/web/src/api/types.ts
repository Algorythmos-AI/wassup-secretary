import type { components } from "./schema";

type S = components["schemas"];
export type Me = S["Me"];
export type ClinicAccess = S["ClinicAccess"];
export type Role = ClinicAccess["role"];
export type CallSummary = S["CallSummary"];
export type CallPage = S["CallPage"];
export type CallDetail = S["CallDetail"];
export type CallRecord = S["CallRecord"];
export type WorkflowStatus = CallSummary["workflow_status"];
export type WorkflowResult = S["WorkflowResult"];
export type AnalyticsSummary = S["AnalyticsSummary"];
export type UsageReport = S["UsageReport"];
export type Team = S["Team"];
export type TeamChange = S["TeamChange"];
export type Member = S["Member"];
export type Invitation = S["Invitation"];
