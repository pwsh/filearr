// One-shot cross-page handoffs for the visual filter builder (user-requested).
//
// Hash routing carries no state payload, and a DSL string is awkward to stuff in
// a query string, so a builder->reports / reports->builder navigation hands its
// payload through this tiny module-level buffer: the source sets it, navigates by
// hash, and the destination page CONSUMES it once on mount (take* clears it, so a
// later manual visit to the page is unaffected). Pure, DOM-free, no reactivity.

export interface ReportPrefill {
  /** Prefill the custom-report create form's query box (the compiled DSL). */
  query: string;
  /** Prefill the selected projection columns (falls back to the form default). */
  columns?: string[];
  name?: string;
}

let _reportPrefill: ReportPrefill | null = null;

export function setReportPrefill(p: ReportPrefill): void {
  _reportPrefill = p;
}
export function takeReportPrefill(): ReportPrefill | null {
  const p = _reportPrefill;
  _reportPrefill = null;
  return p;
}

export interface BuilderPrefill {
  /** A DSL string to parse back into builder rows ("Edit in builder"). */
  query: string;
}

let _builderPrefill: BuilderPrefill | null = null;

export function setBuilderPrefill(p: BuilderPrefill): void {
  _builderPrefill = p;
}
export function takeBuilderPrefill(): BuilderPrefill | null {
  const p = _builderPrefill;
  _builderPrefill = null;
  return p;
}
