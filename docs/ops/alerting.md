# Alerting & notification channels

Filearr watches your libraries (file created/modified/deleted/moved) and its own
health (scan failures, extract-error spikes, low disk space, failed report
deliveries) and dispatches notifications through **channels**. A channel is a
destination — a webhook, an SMTP mailbox, or an Apprise URL — configured under
**Admin → Alerts → Channels** (admin scope). Rules decide *what* fires; channels
decide *where* it goes.

## Webhook payload formats (FIX-16)

A webhook channel POSTs a JSON body to your URL. Because different endpoints
require different body shapes, each webhook channel has a **payload format**:

| Format    | Body shape                                                    | HMAC signature |
| --------- | ------------------------------------------------------------ | -------------- |
| `generic` | Filearr's native compact JSON (`rule`, `event_type`, `paths`, …) | **yes** — `X-Filearr-Signature` |
| `discord` | `{ "content": …, "embeds": [ { title, description, fields, color, timestamp } ] }` | no |
| `slack`   | `{ "text": …, "blocks": [ … ] }`                             | no             |

* **`generic`** is the default and is unchanged from earlier releases: the body
  is Filearr's own JSON and every request carries an `X-Filearr-Signature`
  (`t=<ts>,sha256=<hex>`) so your receiver can verify authenticity + freshness.
  This is the back-compat default for any channel created before this feature.
* **`discord`** and **`slack`** reshape the body so those services accept it. A
  Discord webhook otherwise rejects Filearr's generic body with
  `400 {"code": 50006, "message": "Cannot send an empty message"}` because it
  requires `content` or `embeds`. These formats **do not** send the HMAC header —
  Discord/Slack don't verify it — but every other protection is identical (see
  below).

The format is **auto-detected from the URL** when you create a channel and stays
editable in the form:

* `https://discord.com/api/webhooks/…` or `…discordapp.com/api/webhooks/…` → `discord`
* `https://hooks.slack.com/…` → `slack`
* anything else → `generic`

Truncation and escaping are handled for you: Discord embed limits (content 2000,
description 4096, field value 1024, ≤25 fields) and Slack's section limit are
enforced with ellipsis truncation, and untrusted filenames are escaped so they
cannot inject Discord markdown or Slack `<url|text>` links.

Scheduled-report deliveries use the same channel format — a Discord/Slack channel
gets the report name as the title plus the row count and download link; a generic
channel gets the JSON summary as before.

### Discord setup example

1. In Discord: **Server Settings → Integrations → Webhooks → New Webhook**, pick a
   channel, and **Copy Webhook URL** (looks like
   `https://discord.com/api/webhooks/123456789/abcdef…`).
2. In Filearr: **Admin → Alerts → Channels → + New channel**, type `webhook`,
   paste the URL. The **Payload format** select auto-switches to `discord`.
3. Leave the HMAC secret blank (Discord ignores it). Save.
4. Click **Test** on the channel row — a test embed should appear in your Discord
   channel within a second. If it does not, the test result shows the reason
   (e.g. a 401 for a revoked webhook).
5. Attach the channel to the alert rules (or the built-in system rules) you want
   delivered there.

### Slack setup example

1. Create an **Incoming Webhook** in your Slack workspace and copy its
   `https://hooks.slack.com/services/…` URL.
2. New webhook channel in Filearr, paste the URL → format auto-detects as `slack`.
3. Save and **Test**.

## Security posture (unchanged across all formats)

The webhook driver's protections apply identically to `generic`, `discord`, and
`slack`:

* **SSRF default-deny** — the target is resolved and every A/AAAA record vetted;
  a name host is pinned to its validated IP for the actual socket (DNS-rebinding
  defense). `FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS` is the only widening.
* **Redirects are never followed** (a 3xx to a private IP is an SSRF bypass).
* **Bounded I/O** — per-request timeout (`FILEARR_ALERT_WEBHOOK_TIMEOUT_S`) and a
  response-size cap.

Only the HMAC signature differs: it is added for `generic` and omitted for
`discord`/`slack`.

## Distributed-agent health alerts (P8-T11)

When the agent fleet is enabled (`FILEARR_AGENTS_ENABLED=true`), a 5-minutely
monitor (`filearr.tasks.agentmon`, the low-disk `diskmon` sibling) evaluates two
built-in **system** alert rules. Both are **seeded disabled** — they exist so an
operator can attach a channel and turn them on, exactly like *System: low disk
space*.

| Rule                                | Event key                    | Fires when                                                                                                  | Default threshold |
| ----------------------------------- | ---------------------------- | ---------------------------------------------------------------------------------------------------------- | ----------------- |
| **System: agent offline**           | `agent_offline`              | a cert-bound, non-revoked agent's `last_seen_at` is older than the threshold                                | **48h** (`FILEARR_AGENT_OFFLINE_ALERT_SECONDS=172800`) |
| **System: agent replication stalled** | `agent_replication_stalled` | the agent **is alive** (seen within the offline window) but its newest replication watermark is older than the threshold | **6h** (`FILEARR_AGENT_REPLICATION_STALL_ALERT_SECONDS=21600`) |

Why the offline default is so generous: **offline is a normal agent state**. A
laptop agent that sleeps every night, or a desktop powered off over a long
weekend, must never page anyone — so the offline signal is deliberately soft
(48h). The **replication-stall** rule is the sharper one: an agent that is
*online but silent* (its outbox has stopped draining, or a sync is wedged) is a
real fault, so it trips at 6h. The watermark is the newest of the agent's
replication-ledger `applied_at` and its `last_reconcile_at`.

Guards baked in:

* **Pending (cert-unbound) and revoked agents never alert** — only agents that
  completed enrollment and are not denylisted are evaluated.
* **A fresh enrollee never stalls** — an agent with zero ledger rows *and* no
  reconcile has never replicated, so it cannot have *stalled*.
* **No double-alert** — an offline agent does not also fire the stall rule.
* Both recovery-clear (a `recovered` event) when the agent reappears / replication
  resumes, and both dedup to at most one firing + one recovery per agent per hour.
* The whole monitor is a **no-op** when `FILEARR_AGENTS_ENABLED` is false or the
  agent tables are absent.

### Post-deploy operator step (required to receive these alerts)

Seeding creates the rules **disabled with no channel**, so nothing dispatches
until you:

1. **Admin → Alerts → Rules** — open *System: agent offline* and/or *System: agent
   replication stalled*.
2. Attach a notification **channel** (the same webhook / email / Apprise channels
   used everywhere else).
3. **Enable** the rule. Optionally tune the two thresholds via the environment
   variables above for an always-on server fleet (tighter) vs. a laptop fleet
   (keep them generous).
