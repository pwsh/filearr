// UI-T11 — pure cron generate/parse layer for the friendly schedule builder.
//
// Storage stays a 5-field cron string evaluated in UTC by the backend
// (schedule.py `cron_is_due`, T5 decision). This module is the ONLY place that
// knows how to turn a human "run daily at 14:00 local" choice into that UTC cron
// and back again — ScheduleField.svelte is a thin view over these functions.
//
// Timezone contract: the builder speaks LOCAL wall-clock; cron is fixed-UTC.
// `offsetMinutes` is JS `Date.prototype.getTimezoneOffset()` semantics —
// positive WEST of UTC (e.g. New York EST = +300), so `UTC = local + offset`.
// The conversion can move the day (a late-evening local run lands next-day UTC,
// an early-morning local run lands previous-day UTC); for Weekly/Monthly we shift
// the cron's day-of-week / day-of-month field to match. Because cron is stored at
// a FIXED UTC offset, the local wall-time drifts ±1h across DST — that caveat is
// surfaced in the UI, not corrected here (correcting it would need a tz database).
//
// Contract pinned language-neutrally by shared/schedule-vectors.json +
// backend/tests/test_schedule_vectors_uit11.py (validated with cronsim and
// cron_is_due). Round-trip invariant: generateCron(parseCron(x)) === x for every
// cron this module generates.

export type ScheduleMode =
  | "off"
  | "hourly"
  | "daily"
  | "weekly"
  | "monthly"
  | "advanced";

export interface Schedule {
  mode: ScheduleMode;
  /** hourly: minute-past-the-hour (local). */
  minute?: number;
  /** daily/weekly/monthly: local hour (0-23). */
  hour?: number;
  /** daily/weekly/monthly: local minute (0-59) — also `minute` on hourly. */
  daysOfWeek?: number[]; // weekly: 0=Sun … 6=Sat (local)
  dayOfMonth?: number; // monthly: 1-31 (local)
  /** advanced: raw cron string as typed (UTC, opaque). */
  cron?: string;
}

/** Current local UTC offset in getTimezoneOffset() convention (+west). */
export function getOffsetMinutes(): number {
  return new Date().getTimezoneOffset();
}

const mod = (n: number, m: number): number => ((n % m) + m) % m;

/** day-of-month wrapped into the cron range 1..31. */
const wrapDom = (d: number): number => mod(d - 1, 31) + 1;

/** Strict single non-negative integer within [min,max]; null otherwise. */
function intField(s: string, min: number, max: number): number | null {
  if (!/^\d+$/.test(s)) return null;
  const n = Number(s);
  return n >= min && n <= max ? n : null;
}

interface UtcTime {
  utcHour: number;
  utcMinute: number;
  dayShift: number; // days added going local -> UTC (-1, 0, or +1)
}

/** Local wall-clock (hour:minute) -> UTC, tracking any day rollover. */
export function localTimeToUtc(
  hour: number,
  minute: number,
  offsetMinutes: number,
): UtcTime {
  const raw = hour * 60 + minute + offsetMinutes;
  const dayShift = Math.floor(raw / 1440);
  const tod = mod(raw, 1440);
  return { utcHour: Math.floor(tod / 60), utcMinute: tod % 60, dayShift };
}

interface LocalTime {
  hour: number;
  minute: number;
  dayShift: number; // days added going UTC -> local (== -localTimeToUtc.dayShift)
}

/** UTC (hour:minute) -> local wall-clock, tracking any day rollover. */
export function utcTimeToLocal(
  hour: number,
  minute: number,
  offsetMinutes: number,
): LocalTime {
  const raw = hour * 60 + minute - offsetMinutes;
  const dayShift = Math.floor(raw / 1440);
  const tod = mod(raw, 1440);
  return { hour: Math.floor(tod / 60), minute: tod % 60, dayShift };
}

const sortedUniq = (xs: number[]): number[] =>
  [...new Set(xs)].sort((a, b) => a - b);

/**
 * Build the UTC cron string for a LOCAL schedule. Returns null for "off" (and
 * for an empty Weekly / blank Advanced, which mean the same thing). Emits a
 * canonical form (plain integers, ascending unique DOW) so the round-trip
 * invariant holds.
 */
export function generateCron(
  s: Schedule,
  offsetMinutes: number = getOffsetMinutes(),
): string | null {
  switch (s.mode) {
    case "off":
      return null;
    case "hourly": {
      const m = s.minute ?? 0;
      const utcMinute = mod(m + offsetMinutes, 60);
      return `${utcMinute} * * * *`;
    }
    case "daily": {
      const { utcHour, utcMinute } = localTimeToUtc(
        s.hour ?? 0,
        s.minute ?? 0,
        offsetMinutes,
      );
      return `${utcMinute} ${utcHour} * * *`;
    }
    case "weekly": {
      const days = s.daysOfWeek ?? [];
      if (days.length === 0) return null;
      const { utcHour, utcMinute, dayShift } = localTimeToUtc(
        s.hour ?? 0,
        s.minute ?? 0,
        offsetMinutes,
      );
      const utcDows = sortedUniq(days.map((d) => mod(d + dayShift, 7)));
      return `${utcMinute} ${utcHour} * * ${utcDows.join(",")}`;
    }
    case "monthly": {
      const { utcHour, utcMinute, dayShift } = localTimeToUtc(
        s.hour ?? 0,
        s.minute ?? 0,
        offsetMinutes,
      );
      const utcDom = wrapDom((s.dayOfMonth ?? 1) + dayShift);
      return `${utcMinute} ${utcHour} ${utcDom} * *`;
    }
    case "advanced": {
      const raw = (s.cron ?? "").trim();
      return raw ? raw : null;
    }
  }
}

/**
 * Classify a stored UTC cron back into a LOCAL builder schedule. Anything that
 * is not one of the shapes generateCron emits (custom steps/ranges/lists, a
 * non-`*` month, five-plus fields, unparseable ints) falls through to Advanced,
 * carrying the raw string verbatim so the user still sees exactly what runs.
 */
export function parseCron(
  cron: string | null,
  offsetMinutes: number = getOffsetMinutes(),
): Schedule {
  const raw = (cron ?? "").trim();
  if (!raw) return { mode: "off" };

  const advanced: Schedule = { mode: "advanced", cron: raw };
  const parts = raw.split(/\s+/);
  if (parts.length !== 5) return advanced;
  const [miF, hoF, domF, monF, dowF] = parts;

  // The builder never touches the month field.
  if (monF !== "*") return advanced;

  // Hourly: `M * * * *`
  if (hoF === "*" && domF === "*" && dowF === "*") {
    const mi = intField(miF, 0, 59);
    if (mi === null) return advanced;
    return { mode: "hourly", minute: mod(mi - offsetMinutes, 60) };
  }

  const mi = intField(miF, 0, 59);
  const ho = intField(hoF, 0, 23);
  if (mi === null || ho === null) return advanced;

  // Daily: `M H * * *`
  if (domF === "*" && dowF === "*") {
    const { hour, minute } = utcTimeToLocal(ho, mi, offsetMinutes);
    return { mode: "daily", hour, minute };
  }

  // Weekly: `M H * * D[,D...]`
  if (domF === "*" && dowF !== "*") {
    const rawDows: number[] = [];
    for (const tok of dowF.split(",")) {
      const d = intField(tok, 0, 7);
      if (d === null) return advanced;
      rawDows.push(d === 7 ? 0 : d); // cron allows 7 == Sunday
    }
    const { hour, minute, dayShift } = utcTimeToLocal(ho, mi, offsetMinutes);
    const local = sortedUniq(rawDows.map((d) => mod(d + dayShift, 7)));
    return { mode: "weekly", hour, minute, daysOfWeek: local };
  }

  // Monthly: `M H D * *`
  if (dowF === "*" && domF !== "*") {
    const dom = intField(domF, 1, 31);
    if (dom === null) return advanced;
    const { hour, minute, dayShift } = utcTimeToLocal(ho, mi, offsetMinutes);
    return {
      mode: "monthly",
      hour,
      minute,
      dayOfMonth: wrapDom(dom + dayShift),
    };
  }

  return advanced;
}

const DOW_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const pad2 = (n: number): string => String(n).padStart(2, "0");

/** "14:00" formatter for a local hour/minute. */
export function fmtTime(hour: number, minute: number): string {
  return `${pad2(hour)}:${pad2(minute)}`;
}

/**
 * One-line, human, LOCAL-time summary of a builder schedule. Advanced echoes the
 * raw cron and is labelled UTC. Never interpolates untrusted markup — callers
 * render the returned string as text.
 */
export function describeSchedule(s: Schedule): string {
  switch (s.mode) {
    case "off":
      return "No automatic scans (manual only).";
    case "hourly":
      return `Runs every hour at :${pad2(s.minute ?? 0)} (local).`;
    case "daily":
      return `Runs daily at ${fmtTime(s.hour ?? 0, s.minute ?? 0)} (local).`;
    case "weekly": {
      const days = sortedUniq(s.daysOfWeek ?? []);
      if (days.length === 0) return "Pick at least one day.";
      const names = days.map((d) => DOW_LABELS[d] ?? "?").join(", ");
      return `Runs weekly on ${names} at ${fmtTime(s.hour ?? 0, s.minute ?? 0)} (local).`;
    }
    case "monthly":
      return `Runs monthly on day ${s.dayOfMonth ?? 1} at ${fmtTime(s.hour ?? 0, s.minute ?? 0)} (local).`;
    case "advanced": {
      const raw = (s.cron ?? "").trim();
      return raw ? `Runs on cron "${raw}" (UTC).` : "No automatic scans (manual only).";
    }
  }
}
