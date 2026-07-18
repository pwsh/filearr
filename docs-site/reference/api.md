# API

Filearr exposes a public REST API that supports **search and metadata updates**,
plus everything the web UI does. It is served under `/api/v1` and documented
interactively via OpenAPI.

## Interactive docs

Every running instance serves interactive API documentation:

- **Swagger UI:** `http://<host>:8484/api/docs`
- The OpenAPI schema underlies it, so client generators work out of the box.

This is the authoritative, always-current reference for request/response shapes —
this page is an orientation, not a substitute.

## Authentication

Two credential carriers are accepted on the same routes (see
[Security](../security.md#authentication-model)):

- **API key** — a Bearer token with `read` / `write` / `admin` scope:

    ```http
    Authorization: Bearer <api-key>
    ```

- **Session cookie** — issued by the login flow for the interactive UI; a
  principal's global role maps onto the same read/write/admin scope vocabulary.

With `FILEARR_AUTH_ENABLED=false`, routes are open (development only).

## Surface areas

| Area | Routes (under `/api/v1`) | What it does |
|---|---|---|
| Search | `search` | Typo-tolerant search, facets, filters. |
| Items | `items` (incl. `PATCH`, batch, `digests`) | Read items; edit `user_metadata`/tags; batch edits; on-demand digests. |
| Libraries | `libraries`, `scan-paths` | Define libraries, roots, schedules, presets. |
| Scans | `scans` (with SSE) | Trigger/stop scans; live progress via Server-Sent Events. |
| Query | `query/preview`, `query/keys` | Filter-builder DSL preview and value pickers. |
| Reports | `custom-reports`, `report-schedules`, `exports` | Saved reports, scheduled delivery, CSV/NDJSON/XLSX exports. |
| Saved searches | `saved-searches` | Persisted search definitions. |
| Metadata | `metadata-profiles`, `custom-fields` | Extraction profiles and user-defined fields. |
| Filesystem | `fs/browse` | Allow-listed server-side folder browser (for the library form). |
| System | `system` (health, disk, jobs, `rebuild-index`, share-map, version) | Ops endpoints. |
| Stats | `stats` | Catalog and operational statistics. |
| Auth & identity | `auth`, `oidc`, `rbac`, `audit` | Login/session, SSO, path grants, audit log. |
| Alerts | `alerts` | Channels, rules, events. |
| Agents *(when enabled)* | `agents`, `agent-commands`, `agent-policies`, `agent-releases`, `transfers`, `agent-staging`, `agent-thumbs`, `agent-share-maps` | The distributed-agent control and data planes. |

## A few common calls

```bash
# health
curl http://localhost:8484/api/v1/health

# create a library and scan it
curl -X POST http://localhost:8484/api/v1/libraries \
  -H 'Content-Type: application/json' \
  -d '{"name":"media","root_path":"/data/media"}'
curl -X POST http://localhost:8484/api/v1/libraries/<id>/scan

# edit an item's user metadata (write scope) — never touches extracted metadata
curl -X PATCH http://localhost:8484/api/v1/items/<id> \
  -H 'Authorization: Bearer <write-key>' -H 'Content-Type: application/json' \
  -d '{"user_metadata": {"note": "keep"}, "tags": ["favorite"]}'

# rebuild the search index (admin) — always safe; Meili is disposable
curl -X POST http://localhost:8484/api/v1/system/rebuild-index \
  -H 'Authorization: Bearer <admin-key>'

# the AGPL §13 source link + running version
curl http://localhost:8484/api/v1/version
```

!!! note "Edits go to `user_metadata` only"
    `PATCH /items/{id}` writes the **user** overlay; a rescan can never clobber
    it. Extracted metadata is read-only through the API. This is
    [architecture invariant 2](../data-collection.md#extracted-metadata-vs-user-edits-the-separation-contract).
