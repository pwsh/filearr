# TLS & the agent mTLS plane

Filearr's Caddy sidecar (service `caddy`) terminates HTTPS in front of the app.
There are two modes, selected by `FILEARR_TLS_MODE` (the Proxmox deploy prompts
for it; `FILEARR_CADDYFILE` in the CT `.env` picks the matching Caddyfile):

| mode | issuer | for | Caddyfile |
|---|---|---|---|
| `internal` (default) | Caddy's self-signed **internal CA** | LAN / homelab, no public DNS | `docker/caddy/Caddyfile.internal` |
| `acme-dns` | **Let's Encrypt wildcard** `*.<domain>` via Cloudflare DNS-01 | public TLS, terminated in the CT (no external nginx), **plus the agent mTLS plane** | `docker/caddy/Caddyfile.acme` |

The custom Caddy image (`docker/caddy/Dockerfile`) is an xcaddy build of caddy
**v2.11.4** with two plugins (re-verify both + the caddy CVE list on any bump):

- `github.com/caddy-dns/cloudflare` **v0.2.4** — the DNS-01 solver (no inbound
  `:80` / HTTP-01; issuance works behind NAT).
- `github.com/mholt/caddy-l4` **v0.1.2** — raw-TCP / SNI routing, used for the
  step-ca passthrough (below).

---

## `internal` mode (LAN / homelab)

Unchanged from the original OPS-T1 behaviour. Caddy mints a self-signed root on
the `caddy_data` volume and serves the UI/API on the published TLS port
(`WEB_TLS_PORT`, default 8443). Trust the root once to remove the browser
warning (also in `docs/ops/backup.md`):

```bash
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./filearr-root-ca.crt
# import filearr-root-ca.crt into the OS/browser trust store
```

No public DNS, ACME, or internet egress required.

---

## `acme-dns` mode (public wildcard + agent mTLS)

The CT terminates public TLS itself — **the external nginx proxy is out of the
path entirely.** One wildcard cert `*.<domain>` (Let's Encrypt, Cloudflare
DNS-01) backs three hostnames, all on port **443**:

| hostname | purpose | Caddy handling |
|---|---|---|
| `filearr.<domain>` | web UI / API | `reverse_proxy app:8000` |
| `agents.<domain>`  | agent plane (replication / reconcile / policy / commands) | **mTLS** `reverse_proxy app:8000` |
| `ca.<domain>`      | step-ca | **raw SNI/L4 passthrough** → `step-ca:9000` |

### L4 / HTTP coexistence on 443

step-ca's `/renew` authenticates with the agent's **client cert on the direct
TLS connection** — an L7 terminator silently breaks it (see agents.md §7.4). So
`ca.<domain>` must be passed through **raw**. caddy-l4's `layer4` **listener
wrapper** runs first on the HTTPS listener: it peeks the TLS ClientHello without
terminating and, when the SNI is `ca.<domain>`, raw-TCP-proxies the whole
connection to step-ca (which keeps its own TLS). Everything else falls through
to the `tls` wrapper, where the HTTP app terminates + host-routes normally. This
is the coexistence mechanism (HTTP app == default fallback); it is exercised by
`caddy validate` + the P5-T6 local E2E.

**This replaces the old external-proxy SNI requirement** (agents.md §7.4): the CA
passthrough now lives *inside* the CT, so no upstream nginx `stream {}` block is
needed.

### HTTP/3 is pinned OFF — and must stay off

Both Caddyfiles set `protocols h1 h2`. Two independent reasons:

1. **Every 443/8443 mapping we publish is TCP-only** (Docker's default without a
   `/udp` suffix). Caddy's default protocol set is `h1 h2 h3`, which advertises
   `Alt-Svc: h3=":443"` on a port where UDP was never forwarded. Browsers that
   honour the advertisement attempt QUIC and **stall on a site that is otherwise
   perfectly healthy** — the server looks down while `curl` against it returns
   `HTTP/2 200`. This bit a live deployment on 2026-07-19; the symptom vanished
   only when QUIC was disabled in the browser.
2. **QUIC would bypass the `ca.<domain>` passthrough.** The `layer4` listener
   wrapper above peeks the ClientHello on the **TCP** listener only. A QUIC client
   reaching `ca.<domain>` would skip the raw proxy entirely and hit the HTTP app,
   breaking agent CA renewal (agents.md §7.4).

So publishing UDP/443 is **not** a valid fix for (1) — it trades a stall for a
broken agent CA plane. If HTTP/3 is ever wanted, (2) must be solved first (e.g. a
QUIC-aware SNI split, or moving step-ca off the shared 443).

Diagnostic: a correct deployment returns **no** `alt-svc` header.

```bash
curl -sI --resolve filearr.<domain>:443:127.0.0.1 \
  https://filearr.<domain>/api/v1/health | grep -i alt-svc   # expect no output
```

Note that browsers cache `Alt-Svc` (ours advertised `ma=2592000`, 30 days), so
after fixing this you must clear browsing data or use a fresh profile — a stale
cache keeps retrying QUIC and masks the fix.

### Agent-plane mTLS

The `agents.<domain>` site enforces `client_auth { mode require_and_verify;
trust_pool file /step-root/certs/root_ca.crt }` — the step-ca root, mounted
read-only into caddy from the shared `stepca_data` volume. Caddy verifies the
client cert, then forwards the **already-verified** identity to the backend as
trusted headers, guarded by a shared secret:

```
X-Filearr-Agent-San   {http.request.tls.client.san.dns_names.0}   # == str(agent_id)
X-Filearr-Agent-Fp    {http.request.tls.client.fingerprint}        # secondary check
X-Filearr-Proxy-Auth  {$FILEARR_PROXY_SHARED_SECRET}               # trust gate
```

The backend trusts these headers only when `X-Filearr-Proxy-Auth` matches
`FILEARR_PROXY_SHARED_SECRET` (constant-time) — i.e. the request demonstrably
transited *this* proxy. See `FILEARR_AGENT_AUTH_MODE` below and agents.md §6.

### What the operator must do (acme-dns)

1. **DNS** — create `A`/`AAAA` records pointing `filearr.<domain>`,
   `agents.<domain>`, `ca.<domain>` at the CT (or your port-forward). All three
   share 443 via SNI.
2. **Cloudflare token** — scope **`Zone:DNS:Edit` on the `<domain>` zone**. The
   deploy stores it in the CT `.env` (`CLOUDFLARE_API_TOKEN`) only; never echoed.
3. **step-ca** — acme-dns mode brings up the `agents` compose profile so the CA
   root exists for the mTLS `trust_pool`. Configure its provisioner claims + JWK
   per agents.md §7 before enrolling agents.
4. **Shared secret** — `FILEARR_PROXY_SHARED_SECRET` is auto-generated once by
   the deploy (like `FILEARR_SECRET_KEY`); never rotated automatically.

The Proxmox deploy prompts for mode/domain/email/token and prints this runbook.

---

## Troubleshooting (live-verified 2026-07-17)

- **"timed out waiting for record to fully propagate … last error: <nil>"**
  with NO Cloudflare API errors above it = the TXT record published fine but
  Caddy's propagation SELF-CHECK can't see it — almost always **split-horizon
  DNS**: a LAN resolver (OPNsense/Unbound host-override, Pi-hole local zone)
  that answers authoritatively for your domain hides the public
  `_acme-challenge` record from the CT. The shipped Caddyfile pins the check
  to public resolvers (`resolvers 1.1.1.1:53 8.8.8.8:53` in each `tls` block)
  for exactly this reason — do not remove it on a homelab network.
- **LAN DNS records:** with split-horizon overrides, EVERY hostname the CT
  serves needs its own override → CT IP: `filearr.<domain>`,
  `agents.<domain>`, `ca.<domain>` (or a wildcard override). A missing `ca.`
  override breaks agent cert renewal from inside the LAN.
- **Cloudflare token scope:** a scoped token needs BOTH `Zone → Zone → Read`
  and `Zone → DNS → Edit` on the zone (DNS:Edit alone cannot enumerate the
  zone). The token lives ONLY in the CT `.env` (never `deploy.conf`, never
  echoed); leave the deploy prompt blank on redeploys to keep the current one.
- **Deploying uncommitted work:** the deploy pushes the source tree verbatim.
  `push_source` now refuses/confirms on a dirty git tree
  (`FILEARR_DEPLOY_ALLOW_DIRTY=1` to override deliberately).

## `FILEARR_AGENT_AUTH_MODE` — the agent-plane auth switch

Every agent-plane endpoint routes through `_authenticate_agent`
(`backend/filearr/api/agent_commands.py`). Its behaviour is set by
`FILEARR_AGENT_AUTH_MODE`:

| mode | how an agent authenticates | bearer? |
|---|---|---|
| `fingerprint` (default) | interim P5-T1 scheme — the agent's bound `cert_fingerprint` as a bearer token | required |
| `mtls-header` | the Caddy-forwarded, already-verified mTLS identity: `X-Filearr-Proxy-Auth` matches the shared secret **and** `X-Filearr-Agent-San == str(agent_id)`. Identity is the **SAN** — renewal-proof (survives cert rotation). Optional `X-Filearr-Agent-Fp` secondary check when the agent row has a bound fingerprint. | **refused** |
| `both` | a request carrying `X-Filearr-Proxy-Auth` is validated via the mtls-header path (hard-fails on a bad secret/SAN — no silent downgrade); a request without it falls back to bearer. Migration window. | fallback only |

Semantics: `401` missing/wrong credential, `403` SAN≠agent_id / fingerprint
contradiction / revoked agent, `404` unknown agent. `mtls-header`/`both` require
`FILEARR_PROXY_SHARED_SECRET`; when it is unset those modes **fail closed**.

### Mode-flip runbook (interim bearer → mTLS, zero downtime)

The interim fingerprint bearer has a known drift caveat (agents.md §6): the
fingerprint rotates on cert renewal, so central can 401 a renewed agent. mTLS
**kills that caveat** because identity is the SAN, not the fingerprint.

1. Set `FILEARR_AGENT_AUTH_MODE=both`, redeploy. Existing agents keep working on
   the bearer; migrated agents are accepted via mTLS.
2. Point each agent at `https://agents.<domain>` (Go agent: `-central …` or
   `FILEARR_AGENT_CENTRAL_URL`; it presents its enrolled client cert
   automatically — see below). Optionally `FILEARR_AGENT_CA_BUNDLE` for a custom
   server root.
3. Once every agent is on mTLS, set `FILEARR_AGENT_AUTH_MODE=mtls-header`. The
   weaker bearer path is now shut off.

### Agent client (Go)

The shared agent HTTP client (`agent/cmd/filearr-agent/httpclient.go`, used by
replication, policy poll, and reconcile) presents the enrolled leaf as its client
certificate when the central URL is `https://` **and** a leaf+key exist on disk.
The cert is loaded per-handshake, so renewal is picked up live. Server
verification uses the system roots (public LE), optionally augmented by
`FILEARR_AGENT_CA_BUNDLE` (extra PEM roots). Plain-http central (dev/E2E) is
unchanged. The interim bearer is still sent (harmless; supports mode `both`).
