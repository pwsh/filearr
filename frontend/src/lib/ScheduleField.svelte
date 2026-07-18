<script lang="ts">
  // UI-T11 — friendly scan-schedule builder. A pure view over lib/schedule.ts:
  // it presents LOCAL times (Off / Hourly / Daily / Weekly / Monthly) and, for
  // anything the builder can't express, an Advanced raw-cron escape hatch with
  // the existing cron help text. Storage stays a UTC cron string — the local->UTC
  // conversion (incl. day-of-week / day-of-month shifts when the day rolls over)
  // lives entirely in schedule.ts, pinned by shared/schedule-vectors.json.
  //
  // Props: `value` (current cron or null, UTC) seeded ONCE; emits the regenerated
  // cron-or-null through `onChange`. All dynamic strings render as text.
  import { untrack } from "svelte";
  import {
    describeSchedule,
    generateCron,
    getOffsetMinutes,
    parseCron,
    type Schedule,
    type ScheduleMode,
  } from "./schedule";
  import { HELP } from "./help";
  import Help from "./Help.svelte";

  let {
    value,
    onChange,
  }: {
    value: string | null;
    onChange: (cron: string | null) => void;
  } = $props();

  // Offset + initial classification are captured once; the modal/form remounts
  // this component per edit target, so seeding from the prop is intentional.
  const offset = getOffsetMinutes();
  const init = untrack(() => parseCron(value, offset));

  const MODES: { id: ScheduleMode; label: string }[] = [
    { id: "off", label: "Off" },
    { id: "hourly", label: "Hourly" },
    { id: "daily", label: "Daily" },
    { id: "weekly", label: "Weekly" },
    { id: "monthly", label: "Monthly" },
    { id: "advanced", label: "Advanced (cron)" },
  ];
  const DOW = [
    { d: 0, label: "Sun" },
    { d: 1, label: "Mon" },
    { d: 2, label: "Tue" },
    { d: 3, label: "Wed" },
    { d: 4, label: "Thu" },
    { d: 5, label: "Fri" },
    { d: 6, label: "Sat" },
  ];

  const pad2 = (n: number) => String(n).padStart(2, "0");

  let mode = $state<ScheduleMode>(init.mode);
  // Hourly minute-past-the-hour.
  let hourlyMinute = $state(init.mode === "hourly" ? (init.minute ?? 0) : 0);
  // Shared local time-of-day for daily/weekly/monthly, as an <input type=time>.
  let timeStr = $state(
    init.hour != null ? `${pad2(init.hour)}:${pad2(init.minute ?? 0)}` : "03:00",
  );
  let daysOfWeek = $state<number[]>(
    init.mode === "weekly" ? [...(init.daysOfWeek ?? [])] : [1],
  );
  let dayOfMonth = $state(init.mode === "monthly" ? (init.dayOfMonth ?? 1) : 1);
  let advancedCron = $state(
    untrack(() => (init.mode === "advanced" ? (init.cron ?? "") : (value ?? ""))),
  );

  const tzName = (() => {
    try {
      return Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
    } catch {
      return "local time";
    }
  })();

  function parseTimeStr(s: string): [number, number] {
    const m = /^(\d{1,2}):(\d{2})$/.exec(s.trim());
    if (!m) return [0, 0];
    const h = Math.min(23, Math.max(0, Number(m[1])));
    const mi = Math.min(59, Math.max(0, Number(m[2])));
    return [h, mi];
  }

  // The builder's current LOCAL schedule, recomputed reactively from the inputs.
  const schedule = $derived.by<Schedule>(() => {
    switch (mode) {
      case "off":
        return { mode: "off" };
      case "hourly":
        return { mode: "hourly", minute: Math.min(59, Math.max(0, hourlyMinute || 0)) };
      case "daily": {
        const [h, mi] = parseTimeStr(timeStr);
        return { mode: "daily", hour: h, minute: mi };
      }
      case "weekly": {
        const [h, mi] = parseTimeStr(timeStr);
        return { mode: "weekly", hour: h, minute: mi, daysOfWeek };
      }
      case "monthly": {
        const [h, mi] = parseTimeStr(timeStr);
        return { mode: "monthly", hour: h, minute: mi, dayOfMonth };
      }
      case "advanced":
        return { mode: "advanced", cron: advancedCron };
    }
  });

  const summary = $derived(describeSchedule(schedule));
  const cron = $derived(generateCron(schedule, offset));
  // The DST caveat only matters for wall-clock-anchored modes.
  const showTzNote = $derived(
    mode === "daily" || mode === "weekly" || mode === "monthly",
  );

  // Emit on every change, but not on the initial mount (parent already holds the
  // equivalent value). A fresh generate can canonicalise a hand-written cron; we
  // let that flow through only after a real interaction.
  let mounted = false;
  $effect(() => {
    const next = cron; // track
    if (!mounted) {
      mounted = true;
      return;
    }
    untrack(() => onChange(next));
  });

  function toggleDay(d: number) {
    daysOfWeek = daysOfWeek.includes(d)
      ? daysOfWeek.filter((x) => x !== d)
      : [...daysOfWeek, d];
  }
</script>

<div class="flex flex-col gap-2">
  <div class="flex flex-wrap gap-1">
    {#each MODES as m}
      <button
        type="button"
        class="rounded-full border px-3 py-1 text-xs {mode === m.id
          ? 'border-transparent bg-[var(--accent)] text-white'
          : 'border-slate-300 dark:border-slate-700'}"
        onclick={() => (mode = m.id)}>{m.label}</button>
    {/each}
  </div>

  {#if mode === "hourly"}
    <label class="flex items-center gap-2 text-xs text-slate-500">
      At minute
      <input
        type="number"
        min="0"
        max="59"
        class="w-20 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700"
        bind:value={hourlyMinute} />
      past every hour
    </label>
  {:else if mode === "daily"}
    <label class="flex items-center gap-2 text-xs text-slate-500">
      At
      <input
        type="time"
        class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700"
        bind:value={timeStr} />
      ({tzName})
    </label>
  {:else if mode === "weekly"}
    <div class="flex flex-wrap items-center gap-1">
      {#each DOW as day}
        <button
          type="button"
          class="rounded-full border px-2 py-1 text-xs {daysOfWeek.includes(day.d)
            ? 'border-transparent bg-[var(--accent)] text-white'
            : 'border-slate-300 dark:border-slate-700'}"
          onclick={() => toggleDay(day.d)}>{day.label}</button>
      {/each}
    </div>
    <label class="flex items-center gap-2 text-xs text-slate-500">
      At
      <input
        type="time"
        class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700"
        bind:value={timeStr} />
      ({tzName})
    </label>
  {:else if mode === "monthly"}
    <label class="flex items-center gap-2 text-xs text-slate-500">
      On day
      <select
        class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700"
        bind:value={dayOfMonth}>
        {#each Array.from({ length: 31 }, (_, i) => i + 1) as d}
          <option value={d}>{d}</option>
        {/each}
      </select>
      at
      <input
        type="time"
        class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700"
        bind:value={timeStr} />
      ({tzName})
    </label>
    <p class="text-xs text-slate-400">
      Day 29-31 is skipped in months without that day.
    </p>
  {:else if mode === "advanced"}
    <label class="flex items-center gap-1 text-xs text-slate-500">
      Cron expression <Help text={HELP.scan_cron} label="scan cron" />
    </label>
    <input
      class="w-64 rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700"
      placeholder="e.g. 0 4 * * * (UTC)"
      bind:value={advancedCron} />
  {/if}

  <p class="text-xs text-slate-500">{summary}</p>
  {#if showTzNote}
    <p class="text-xs text-slate-400">
      Runs at {parseTimeStr(timeStr)[0].toString().padStart(2, "0")}:{parseTimeStr(
        timeStr,
      )[1]
        .toString()
        .padStart(2, "0")}
      {tzName} (stored as UTC; shifts ±1h across DST).
    </p>
  {/if}
</div>
