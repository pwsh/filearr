# Proxmox LXC

`proxmox/deploy-proxmox.sh` deploys the whole Filearr stack into a
**Docker-enabled LXC** on Proxmox VE, mounting your network storage **inside**
the container so the deployment does not depend on host-side mounts. It is a
wizard on first run and an idempotent redeploy on later runs.

Run it on the **Proxmox host shell** (as root), from inside the project folder:

```bash
bash proxmox/deploy-proxmox.sh                # first run -> wizard, then deploy
bash proxmox/deploy-proxmox.sh               # later runs -> redeploy with saved defaults
bash proxmox/deploy-proxmox.sh --reconfigure # re-run the wizard
bash proxmox/deploy-proxmox.sh --storages    # re-run only the storage definitions
bash proxmox/deploy-proxmox.sh --status      # CT + mounts + stack status
bash proxmox/deploy-proxmox.sh --destroy     # stop & delete the container
```

## What the wizard asks

The wizard saves your answers so redeploys never re-ask. It prompts once for:

- **Container basics** — starting VMID (first free `>=` your number is used),
  hostname, network bridge, DHCP or static IP, rootfs storage, disk size, CPU
  cores, memory, web UI port (default 8484), HTTPS port (default 8443).
- **Public base URL** — the absolute prefix for export/report download links
  (e.g. `https://filearr.example.com:8443`); blank means site-relative links.
- **TLS mode** — `internal` (self-signed LAN CA in the container; no public DNS)
  or `acme-dns` (Let's Encrypt **wildcard** `*.<domain>` via Cloudflare DNS-01;
  the container terminates public TLS itself and also fronts the agent mTLS plane
  and the step-ca SNI passthrough).
- **Distributed agents** — enable the step-ca certificate authority and the
  enrollment endpoints. Safe to enable now and enroll machines later.
- **Storage definitions** — one or more network shares (see below).

### The prompt-once model, and where secrets go

Answers persist to `~/.config/filearr/deploy.conf`; storage definitions
(including credentials) persist to `~/.config/filearr/storages.env` (mode 0600).
Both are re-applied on every redeploy, so **the container is fully disposable** —
destroy and rebuild it and your configuration returns.

!!! danger "Secrets never go in `deploy.conf`"
    `deploy.conf` holds only non-secret settings. The Cloudflare API token, the
    auto-generated proxy shared secret, the auto-generated `FILEARR_SECRET_KEY`,
    and the extracted CA provisioner JWK are **secrets** and live in the
    container's `.env` **only** — never in `deploy.conf`, never echoed to the
    terminal. On a redeploy, a blank token answer means "keep the container's
    existing one".

## Storage: rclone/NFS mounts inside the container

Each storage mounts **read-only** at `/data/media/<name>` inside the container,
and Docker Compose binds `/data/media` into the app and worker. Mounts are
installed as systemd units ordered `Before=docker.service`, so containers always
see them.

| Type | How it is mounted | Container privilege |
|---|---|---|
| SMB/CIFS, FTP, SFTP, WebDAV | rclone FUSE mount (userspace) | **Unprivileged** CT with the `fuse=1` feature |
| NFS | kernel mount | **Privileged** CT (the script switches automatically and warns) |
| local | host path bind-mounted via `pct` | the only type that touches the host |

!!! warning "NFS forces a privileged container"
    Kernel NFS mounts require a **privileged** LXC. If you define an NFS storage,
    the script switches the container to privileged and warns you. SMB/FTP/SFTP/
    WebDAV all work in an unprivileged container via rclone FUSE — prefer those
    where you can. FUSE mounts also need `fuse=1` on the container (the script
    sets it).

For SMB, credentials are collected once per host and reused across every share on
that host. AD domain goes in the separate domain field; use a **bare** username
(no domain prefix).

### The credential-free share map

Because the deploy alone knows the real share URL behind each mount, it writes a
**credential-free** map to `/config/share-map.json` in the container
(regenerated on every deploy). Filearr reads it read-only and auto-populates each
library's user-facing **share location** from the mount that covers its root — so
the "open in Explorer / Finder" hint survives remounts and redeploys with no hand
maintenance. A manual share location always wins. A missing or malformed file
simply disables the feature; the app never fails to start.

## TLS and reverse-proxy topology

The container runs a Caddy TLS front. Two modes:

- **`internal`** — Caddy mints a self-signed LAN CA and serves HTTPS on your
  chosen port (default 8443). No DNS, no ACME, no egress needed. Trust the root
  CA on your clients once to remove the browser warning
  (see [Operations → TLS](../operations.md#tls-and-acme-issuance-failures)).
- **`acme-dns`** — a Let's Encrypt **wildcard** `*.<domain>` via Cloudflare
  DNS-01. The container terminates public TLS itself (no external nginx), and its
  Caddy also carries a layer-4 listener that raw-TCP-proxies `ca.<domain>`
  straight to step-ca (SNI passthrough — the CA must **never** be L7-terminated,
  or agent cert renewal silently breaks).

An **acme-dns example pattern** (all three hostnames share port 443 via SNI):

```text
filearr.example.com   A/AAAA -> <container-ip>   # web UI / API
agents.example.com    A/AAAA -> <container-ip>   # agent mTLS plane
ca.example.com        A/AAAA -> <container-ip>   # step-ca SNI passthrough
```

The Cloudflare API token needs **both** `Zone:Read` and `DNS:Edit` on the zone.
DNS-01 needs no inbound port 80, so issuance works behind NAT. On a split-horizon
LAN (a local resolver answering for your domain), add an override for each
hostname to the container IP — a missing `ca.` override breaks agent renewal from
inside the LAN.

## Optional: iGPU passthrough for video thumbnails

iGPU passthrough is deliberately **not** wired automatically (it is host-specific
and the thumbnail pipeline degrades cleanly to software). To enable QSV video
poster frames after the first deploy, add the DRI cgroup allow and `/dev/dri`
mount entry to the container config and reboot it — the worker compose service
already carries the device mapping, added conditionally only when `/dev/dri`
exists. See the comments in `deploy-proxmox.sh`.

## Redeploy behavior

A redeploy is safe and self-quiescing. It:

1. Gracefully **stops running scans** (progress kept, no tombstoning) and
   remembers their libraries.
2. Updates the container OS packages and Docker engine.
3. Re-applies the storage mounts and regenerates the share map.
4. Pushes the current source (with a dirty-tree guard — it refuses to silently
   deploy uncommitted work) and does a clean extract, preserving `.env`,
   `config/`, and the compose override.
5. Builds and starts the stack, runs the idempotent DB bootstrap, and **verifies
   the running image was built from the source just pushed** (a build-stamp
   check) plus a functional smoke test.
6. **Re-triggers** the scans it stopped in step 1.

See [Upgrades & migrations](upgrades.md) for what happens to the schema on
redeploy.
