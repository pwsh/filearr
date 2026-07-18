# filearr-agent — install, service, sidecar config, and logging (W6-D1)

This document covers installing the agent as a system service, the user-editable
sidecar config, log levels + rotation, and uninstall semantics.

## Quick start

```bash
# 1. (optional) drop a sidecar so you never re-type flags. See "Sidecar" below.
# 2. install as a service (requires admin/root; idempotent — re-run to upgrade):
sudo filearr-agent install
#    …or point at an explicit sidecar:
sudo filearr-agent install --config /etc/filearr-agent/filearr-agent.json

# manage it:
filearr-agent service status | start | stop | restart

# remove it (keeps data/logs/config by default):
sudo filearr-agent uninstall
sudo filearr-agent uninstall --purge      # also deletes data/logs/config
```

`install` copies the running binary into the OS install path, creates the data /
config / log directories, enrolls non-interactively **if** an enrollment token is
configured and the agent is not already enrolled, then registers and starts an
auto-start, restart-on-failure service. It requires elevation and fails early
with a clear message otherwise.

## Configuration precedence

For every setting the agent resolves, highest wins:

```
explicit CLI flag  >  FILEARR_AGENT_* env var  >  sidecar (filearr-agent.json)  >  built-in default
```

The sidecar is the single durable place to record settings that survive service
restarts without re-passing flags. A service install bakes `--data`, `--log-dir`,
and (when a sidecar was found) `--config` into the service's launch arguments.

## Sidecar config (`filearr-agent.json`)

Discovery order:

1. `--config <path>` flag (or `FILEARR_AGENT_CONFIG` env) — used verbatim; a load
   error is surfaced.
2. Beside the executable (`<dir-of-exe>/filearr-agent.json`).
3. OS config dir:
   - Windows `%ProgramData%\Filearr Agent\filearr-agent.json`
   - Linux `/etc/filearr-agent/filearr-agent.json`
   - macOS `/Library/Application Support/FilearrAgent/filearr-agent.json`

Schema (all keys optional; unknown keys are tolerated for forward-compat and
preserved on rewrite):

```json
{
  "central_url": "https://filearr.example.com",
  "enrollment_token": "fae_...",
  "agent_name": "",
  "config_group": "default",
  "data_dir": "",
  "log_level": "info",
  "log_dir": ""
}
```

`enrollment_token` is **one-shot**. After a successful enroll (via `enroll` or
`install`) the agent rewrites the sidecar with the token field emptied and a
`"enrollment_token_consumed_at": "<RFC3339>"` marker stamped; the spent token is
never written back. The rewrite is 0600 (owner-only) and preserves every other
key. On Windows the mode bits do not map to an ACL — the effective protection is
the parent directory's inherited ACL, so keep the sidecar under `%ProgramData%\
Filearr Agent\` (admin-writable) for a hardened deployment.

## Install layout per OS

| | Windows | Linux | macOS |
|---|---|---|---|
| Binary | `%ProgramFiles%\Filearr Agent\filearr-agent.exe` | `/usr/local/bin/filearr-agent` | `/usr/local/bin/filearr-agent` |
| Data (`data_dir` default) | `%ProgramData%\Filearr Agent\` | `/var/lib/filearr-agent` | `/Library/Application Support/FilearrAgent` |
| Config (sidecar) | `%ProgramData%\Filearr Agent\filearr-agent.json` | `/etc/filearr-agent/filearr-agent.json` | `/Library/Application Support/FilearrAgent/filearr-agent.json` |
| Logs | `%ProgramData%\Filearr Agent\logs` | `/var/log/filearr-agent` | `/Library/Logs/FilearrAgent` |

`--data` overrides the data dir (also `FILEARR_AGENT_DATA_DIR` or the sidecar
`data_dir`); with none of those set, a service install uses the system data dir
above rather than the per-user default that a bare `run`/`enroll` would pick.

## Service management

The service integration wraps the existing `run` daemon via
`github.com/kardianos/service` (v1.2.4) — it does not fork a second copy.
`filearr-agent run` always executes under kardianos, so one code path serves an
interactive terminal, a systemd/launchd unit, and the Windows SCM.

- **Auto-start + restart-on-failure** are configured per OS by kardianos:
  - **Linux (systemd):** `Restart=on-failure`. Note: kardianos v1.2.4 hardcodes
    `RestartSec=120` in its unit template (not configurable via an Option), so the
    restart backoff is 120 s, not 5 s.
  - **macOS (launchd):** `KeepAlive` + `RunAtLoad` — relaunched on any exit and at
    boot.
  - **Windows (SCM):** recovery action `restart` with a 5 s delay and a 10 s reset
    window.
- Lifecycle wrappers: `filearr-agent service status|start|stop|restart`.
- `install` is idempotent: a re-run stops + deregisters any existing service,
  re-copies the binary, and re-registers with the current configuration
  (in-place upgrade).

### Self-update under service management

The service's launch environment carries `FILEARR_AGENT_SERVICE=1`, and the
daemon also detects service management via `service.Interactive()`. When
service-managed, the self-updater takes a **clean-exit-for-restart** path: after
the A/B binary swap (or a rollback), it exits with a non-zero
`ServiceRestartExitCode` (20) instead of self-re-execing, and lets the service
manager relaunch the (new or restored) binary. The non-zero code is intentional —
it triggers systemd `Restart=on-failure` and the Windows recovery action, while
launchd `KeepAlive` relaunches on any exit. The boot-counter state written before
the swap drives the next boot's health-window / rollback check exactly as before.
Interactive `run` and the one-shot `update` command keep the historic
self-re-exec behavior (no service manager is present to own the restart).

## Log levels + file logging

`--log-level` (or `FILEARR_AGENT_LOG_LEVEL`, or sidecar `log_level`); default
`info`:

| name | shows |
|---|---|
| `error` | errors only |
| `warn` | warnings + errors |
| `info` | info + warn + error (default) |
| `verbose` | + operational seams (service lifecycle, sidecar resolution, install steps) |
| `debug` | everything |

`verbose` sits strictly between `info` and `debug`: at `info` verbose lines are
hidden; at `verbose` they show but debug stays hidden; at `debug` everything
shows.

`--log-dir` (or `FILEARR_AGENT_LOG_DIR`, or sidecar `log_dir`) enables a rotating
file at `<log_dir>/filearr-agent.log` — 10 MiB per file, 5 compressed backups —
and, when attached to a terminal, also echoes to stderr. With no log dir the
agent logs to stderr only (the historic default). A service install always sets a
log dir (the layout `logs` folder), so a serviced agent always has an on-disk
log.

## Uninstall semantics

- `filearr-agent uninstall` — stop + deregister the service and remove the
  installed binary. **Keeps** the data, config, and log directories and prints
  which ones were kept.
- `filearr-agent uninstall --purge` — additionally deletes the data, log, and
  config directories.

Both require elevation.
