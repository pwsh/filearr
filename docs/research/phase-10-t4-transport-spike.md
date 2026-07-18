# P10-T4 transport sizing spike (R6): tus vs hand-rolled offset-PATCH subset

Ruling R6 (docs/tasks/phase-10-agent-transfer-tasks.md) required a sizing spike —
mirroring P5-T2a / P5-T3a — before committing the agent→central staging upload's
wire transport. This note records the evaluation and the **exact wire protocol**
the implemented subset uses (central `backend/filearr/api/agent_staging.py`, agent
`agent/internal/commands/staging.go` + `staging_client.go`).

## Verdict: hand-rolled tus SUBSET (offset-PATCH). Full tus rejected.

The expectation (task doc: "the subset wins; prove it, don't assume it") holds.
Evaluation against the R6 criteria:

### Dependency cost (Python side) — decisive

- **Full tus** on FastAPI means one of:
  - **`tusd`** (the reference Go server) as a *separate process* — a second
    network daemon to deploy, monitor, TLS-terminate, and reverse-proxy, with its
    own storage backend config. It does not share our `_authenticate_agent` auth,
    our `FILEARR_AGENTS_ENABLED` gate, our RBAC, or our Postgres row lifecycle;
    we would bolt hooks onto it and reconcile two sources of truth. Rejected: it
    is more operational surface than the entire rest of the agent plane combined.
  - **A Python ASGI tus server library** — the maintained options
    (`tuspy` is a *client*; server-side `ASGI`/Starlette tus implementations)
    vary in maintenance and each pulls the FULL protocol surface (see below) as a
    dependency whose CVE stream we must then track. Our need is a ~150-line
    receiver.
- **Subset**: ZERO new Python dependencies. The receiver is stdlib `os`
  file I/O + the existing SQLAlchemy row + the frozen `transfers.py` state
  machine. The agent side adds exactly one small, vendored-quality module —
  `golang.org/x/time/rate` (the de-facto Go token bucket, already required by the
  research §2.4 rate-limit anyway, not by the transport choice).

### Exactly-our-needs fit — decisive

tus's value is *generic* resumable uploads from *untrusted browsers* that must
**create** an upload, attach **metadata**, optionally **concatenate** parallel
parts, negotiate **checksums**, and **defer** the length. We need none of that:

| tus feature | Our need | Verdict |
|---|---|---|
| Creation (`POST` w/ `Upload-Length`, `Upload-Metadata`) | The `staging_transfers` row is created out-of-band by the **command queue** (P10-T1) — the agent *attaches* to an existing, RBAC-authorised transfer. | Not needed |
| Concatenation extension | One agent, one file, one stream. | Not needed |
| Checksum extension | Integrity is a **central streaming hash** vs the catalog (P10-T5), stronger than a per-chunk checksum. | Not needed |
| Expiration extension | TTL is a Postgres column + Procrastinate sweep (P10-T8). | Not needed |
| Metadata (Base64 `Upload-Metadata`) | Item/agent identity come from the **command**, never agent-supplied headers. | Not needed (and safer) |
| **Core: `HEAD`→offset, `PATCH`@offset, 409-on-mismatch** | **Exactly the resume primitive we need.** | **Adopted** |

What remains after subtracting the above is precisely the tus *core*: a monotone
committed offset, `Upload-Offset` echoed on every request, and 409-on-wrong-offset.
We reproduce that discipline byte-for-byte so the semantics are familiar and
correct, without the extension surface.

### Auditability — decisive

A ~150-line receiver whose entire wire contract is documented here and whose only
trust inputs are (a) the authenticated agent identity and (b) a UUID-validated
transfer id is far easier to security-review than a third-party server whose full
extension matrix is reachable code. Project priority order is
security > integrity > reliability > speed > compatibility > scalability; the
subset wins the top three and loses nothing that matters below them.

### Reliability parity

The subset keeps tus's single reliability-critical property: **the committed
offset is always a durable prefix.** Central advances `bytes_transferred` ONLY
after the chunk write is `fsync`ed; a dropped connection loses only the un-acked
tail. A restarted agent holds NO local upload state — it re-attaches (idempotent
per `command_id`) and resumes from central's committed offset. This is identical
in behaviour to a tus resume, which is the whole point of R6.

## The wire protocol (normative)

All endpoints are behind `FILEARR_AGENTS_ENABLED` (404 when off) and use the
agent-plane auth of `api.agent_commands._authenticate_agent` (interim
cert-fingerprint bearer / mTLS-header per `FILEARR_AGENT_AUTH_MODE`). `agent_id`
in the path must match the authenticated agent; a transfer whose `agent_id` is not
the caller's is a 404 (never leak another agent's transfer).

### 1. Attach — `POST /api/v1/agents/{agent_id}/staging`

Request body (JSON): `{"command_id": "<uuid>", "total_bytes": <int >= 0>}`.

- Creates-or-returns the single `staging_transfers` row for that `stage_upload`
  command. **Idempotent per `command_id`** (`UNIQUE(command_id)`): a restarted
  agent, or an at-least-once command redelivery, re-attaches the SAME row.
- `item_id` / `agent_id` are read from the **command row**, never the body.
- The command must be `kind='stage_upload'` and `status='picked_up'` on first
  create (409 otherwise); re-attach returns the existing row regardless of the
  command's current status.
- A `total_bytes` that disagrees with an already-partially-uploaded row is a
  **409** (the source changed mid-resume — restart, never splice).
- Response: **201** on create / **200** on re-attach. Body:
  `{id, item_id, agent_id, command_id, state, bytes_transferred, total_bytes,
  verified}`. Headers: `Upload-Offset: <bytes_transferred>`,
  `Upload-Total: <total_bytes>`, `Upload-State: <state>`.
- `bytes_transferred` in this response IS the resume offset.

### 2. Offset query — `HEAD /api/v1/agents/{agent_id}/staging/{transfer_id}`

- The tus `HEAD` equivalent — the committed offset of record. No body.
- Headers: `Upload-Offset: <committed bytes>`, `Upload-Total`, `Upload-State`.
- (The agent normally resumes from the attach response's offset and does not need
  a separate HEAD; the endpoint exists for a pure re-query and is tested.)

### 3. Append — `PATCH /api/v1/agents/{agent_id}/staging/{transfer_id}`

- Header `Upload-Offset: <int>` — **required**. MUST equal the current committed
  `bytes_transferred`.
  - **Mismatch → 409**, body `{"reason":"offset_mismatch","offset":<committed>}`,
    header `Upload-Offset: <committed>`. The agent re-seeks to `<committed>` and
    retries — idempotent recovery under the at-least-once queue (tus discipline).
- `Content-Type: application/offset+octet-stream` (tus convention; not enforced).
  Request body = the raw bytes to append at the offset.
- Server behaviour: open the staged file, `ftruncate` to `offset` (discard any
  un-acked tail from a crashed prior PATCH), `lseek(offset)`, stream-write the
  body, `fsync`, then advance `bytes_transferred = offset + written` and commit —
  **in that order**, so the committed offset is always a durable prefix. The row
  is locked `FOR UPDATE` for the append so a racing PATCH serialises (loser 409s).
- Caps: a single chunk body above `FILEARR_STAGING_MAX_CHUNK_BYTES` → **413**; a
  chunk that would push `offset + written` past `total_bytes` → **409**.
- State (through `transfers.transfer_state_machine`): first byte
  `pending --start_upload--> uploading`; final byte
  `uploading --staged--> staged` with `verified=false` (integrity is P10-T5).
- Idempotent completion replay: a PATCH to an already-complete row
  (`bytes_transferred >= total_bytes`) is a **200** no-op returning the current
  status (a lost ack, agent retried).
- Response: **200**, body = the status object (as attach), header
  `Upload-Offset: <new committed>`.

### Empty file

`total_bytes == 0`: the agent sends a single zero-length PATCH at `offset=0`,
which drives `pending -> uploading -> staged` and commits a 0-byte staged file.

## Agent client (matches the spec)

`agent/internal/commands/staging.go`:
1. decode `{library_ref, rel_path}`;
2. **re-validate root membership + traversal BEFORE any read** (research §4) — an
   out-of-root path returns an error with NO stat / NO open / NO attach;
3. `stat` the file, `Attach` (get resume offset), `Open`, `Seek(offset)`;
4. stream `DefaultChunkBytes` (8 MiB) chunks via `PATCH`, applying a
   `golang.org/x/time/rate` token bucket sized from the policy cap read at upload
   start; on a 409 mismatch, re-seek to central's offset and continue;
5. `stageMu` guarantees 1 upload/agent (belt to the sequential poller's braces);
6. complete the command `ok=true` with `{transfer_id, total_bytes}` after the last
   chunk is acked, heartbeating the command lease throughout.

## Open question resolved (task-doc Q1: staging disk quota)

No total-staging-bytes ceiling is added in this cut. Per-transfer bounds already
exist (`total_bytes` cap on appends, per-chunk `FILEARR_STAGING_MAX_CHUNK_BYTES`),
and the 24h TTL + P10-T8 sweep bound accumulation. A global staging quota is a
small P10-T8 follow-up if a pathological many-simultaneous-retrievals case proves
real — logged there, not built now (integrity/reliability need is already met).
