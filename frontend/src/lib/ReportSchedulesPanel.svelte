<script lang="ts">
  import { onMount } from "svelte";
  import ScheduleField from "./ScheduleField.svelte";
  import {
    listReportSchedules,
    createReportSchedule,
    updateReportSchedule,
    deleteReportSchedule,
    listReports,
    listCustomReports,
    listAlertChannels,
    EXPORT_FORMATS,
    type ReportSchedule,
    type ReportMeta,
    type ReportDefinition,
    type AlertChannel,
    type ExportFormat,
  } from "./api";

  // P11-T9 — schedule manager: list/create/toggle/delete scheduled report
  // deliveries. The cron is authored with the shared friendly ScheduleField
  // (Off/Hourly/Daily/…/Advanced), the source is a canned OR custom report, and
  // delivery rides a Phase-8 alert channel.
  let schedules = $state<ReportSchedule[]>([]);
  let canned = $state<ReportMeta[]>([]);
  let custom = $state<ReportDefinition[]>([]);
  let channels = $state<AlertChannel[]>([]);
  let error = $state("");
  let creating = $state(false);

  // create form
  let name = $state("");
  let source = $state(""); // "canned:<id>" | "custom:<id>"
  let format = $state<ExportFormat>("csv");
  let cron = $state<string | null>("0 6 * * *");
  let channelId = $state("");

  async function refresh() {
    try {
      [schedules, canned, custom, channels] = await Promise.all([
        listReportSchedules(),
        listReports(),
        listCustomReports(),
        listAlertChannels().catch(() => [] as AlertChannel[]),
      ]);
      error = "";
    } catch (e) {
      error = String(e);
    }
  }

  function sourceLabel(s: ReportSchedule): string {
    if (s.canned_report_key) return s.canned_report_key;
    const c = custom.find((x) => x.id === s.report_definition_id);
    return c ? c.name : "custom report";
  }

  function channelLabel(id: string | null): string {
    if (!id) return "no channel";
    const c = channels.find((x) => x.id === id);
    return c ? c.name : "unknown channel";
  }

  async function create() {
    if (!name.trim() || !source || !cron) {
      error = "Name, report, and a schedule are required.";
      return;
    }
    creating = true;
    error = "";
    try {
      const [kind, id] = source.split(":");
      await createReportSchedule({
        name: name.trim(),
        canned_report_key: kind === "canned" ? id : null,
        report_definition_id: kind === "custom" ? id : null,
        format,
        cron,
        channel_id: channelId || null,
        enabled: true,
      });
      name = "";
      source = "";
      channelId = "";
      await refresh();
    } catch (e) {
      error = String(e);
    } finally {
      creating = false;
    }
  }

  async function toggle(s: ReportSchedule) {
    try {
      await updateReportSchedule(s.id, { enabled: !s.enabled });
      await refresh();
    } catch (e) {
      error = String(e);
    }
  }

  async function remove(s: ReportSchedule) {
    if (!confirm(`Delete schedule "${s.name}"?`)) return;
    try {
      await deleteReportSchedule(s.id);
      await refresh();
    } catch (e) {
      error = String(e);
    }
  }

  onMount(refresh);
</script>

<section class="mt-8">
  <h2 class="mb-3 text-lg font-semibold">Scheduled reports</h2>

  {#if error}
    <p class="mb-3 rounded-lg bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200">
      {error}
    </p>
  {/if}

  <!-- existing schedules -->
  {#if schedules.length > 0}
    <div class="mb-4 overflow-x-auto rounded-lg border border-slate-200 dark:border-slate-800">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-left dark:bg-slate-900">
          <tr>
            <th class="px-3 py-2 font-medium">Name</th>
            <th class="px-3 py-2 font-medium">Report</th>
            <th class="px-3 py-2 font-medium">Format</th>
            <th class="px-3 py-2 font-medium">Cron (UTC)</th>
            <th class="px-3 py-2 font-medium">Channel</th>
            <th class="px-3 py-2 font-medium">Enabled</th>
            <th class="px-3 py-2 font-medium"></th>
          </tr>
        </thead>
        <tbody>
          {#each schedules as s (s.id)}
            <tr class="border-t border-slate-100 dark:border-slate-800">
              <td class="px-3 py-1.5">{s.name}</td>
              <td class="px-3 py-1.5">{sourceLabel(s)}</td>
              <td class="px-3 py-1.5 uppercase">{s.format}</td>
              <td class="px-3 py-1.5 font-mono text-xs">{s.cron}</td>
              <td class="px-3 py-1.5">{channelLabel(s.channel_id)}</td>
              <td class="px-3 py-1.5">
                <button
                  class="rounded border border-slate-300 px-2 py-0.5 text-xs dark:border-slate-700"
                  onclick={() => toggle(s)}>
                  {s.enabled ? "On" : "Off"}
                </button>
              </td>
              <td class="px-3 py-1 text-right">
                <button
                  class="rounded border border-slate-300 px-2 py-0.5 text-xs text-red-600 dark:border-slate-700"
                  onclick={() => remove(s)}>Delete</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <p class="mb-4 text-sm text-slate-500">No scheduled reports yet.</p>
  {/if}

  <!-- create form -->
  <div class="space-y-3 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
    <h3 class="text-sm font-semibold">New schedule</h3>
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <label class="block text-sm">
        <span class="text-slate-500">Name</span>
        <input
          class="mt-1 w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700"
          bind:value={name}
          placeholder="Weekly largest files" />
      </label>
      <label class="block text-sm">
        <span class="text-slate-500">Report</span>
        <select
          class="mt-1 w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700"
          bind:value={source}>
          <option value="">Select a report…</option>
          <optgroup label="Canned">
            {#each canned as r (r.id)}
              <option value={"canned:" + r.id}>{r.title}</option>
            {/each}
          </optgroup>
          {#if custom.length > 0}
            <optgroup label="Custom">
              {#each custom as c (c.id)}
                <option value={"custom:" + c.id}>{c.name}</option>
              {/each}
            </optgroup>
          {/if}
        </select>
      </label>
      <label class="block text-sm">
        <span class="text-slate-500">Format</span>
        <select
          class="mt-1 w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700"
          bind:value={format}>
          {#each EXPORT_FORMATS as f (f)}
            <option value={f}>{f.toUpperCase()}</option>
          {/each}
        </select>
      </label>
      <label class="block text-sm">
        <span class="text-slate-500">Deliver to channel</span>
        <select
          class="mt-1 w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700"
          bind:value={channelId}>
          <option value="">No channel (generate only)</option>
          {#each channels as c (c.id)}
            <option value={c.id}>{c.name} ({c.type})</option>
          {/each}
        </select>
      </label>
    </div>
    <div class="text-sm">
      <span class="text-slate-500">Schedule</span>
      <ScheduleField value={cron} onChange={(c) => (cron = c)} />
    </div>
    <button
      class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm font-medium text-white disabled:opacity-50"
      onclick={create}
      disabled={creating}>
      {creating ? "Creating…" : "Create schedule"}
    </button>
  </div>
</section>
