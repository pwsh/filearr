"""Application settings, loaded from environment / .env (pydantic-settings)."""

from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FILEARR_", env_file=".env", extra="ignore")

    app_name: str = "filearr"
    environment: str = "production"
    log_level: str = "INFO"
    config_dir: str = "/config"  # thumbnails, caches (single-volume convention)
    # OPS-T7: deploy-time network-share mount map. proxmox/deploy-proxmox.sh
    # (setup_storages) writes this file inside the CT (bind-mounted to /config)
    # so a library's user-facing ``share_prefix`` auto-populates from the
    # rclone/NFS mounts the deploy configured, and stays correct across
    # remounts/redeploys with no hand-maintenance. Missing file = feature off
    # (empty map, logged once). Never carries credentials.
    share_map_path: str = "/config/share-map.json"

    # Database (source of truth + Procrastinate job queue)
    database_url: str = "postgresql+psycopg://filearr:filearr@postgres:5432/filearr"
    procrastinate_dsn: str = "postgresql://filearr:filearr@postgres:5432/filearr"

    # Meilisearch (rebuildable projection — never the source of truth)
    meili_url: str = "http://meilisearch:7700"
    meili_master_key: str = "change-me"
    meili_index: str = "items"
    # searchCutoffMs circuit-breaker (P9-T2): upper bound (ms) on a single
    # search's wall-clock time so a crafted/pathological query degrades to a
    # best-effort partial result instead of hanging a worker. Applied by
    # ensure_index(); mirrors meili_ops.DEFAULT_SEARCH_CUTOFF_MS (kept in
    # lockstep by test_search_cutoff_setting_mirrors_meili_ops_default).
    meili_search_cutoff_ms: int = 1500

    # P6-T3 RBAC search scoping: the maximum compiled Meilisearch scope-filter
    # expression length (chars) before ``tenant_tokens`` REFUSES compilation
    # (R2 — refuse, never coarsen; the admin must consolidate grants). Measured
    # note: the shipped enforcement is the SERVER-SIDE PROXY (the API injects the
    # filter into the Meili query body), so there is NO JWT/HTTP-header ceiling to
    # respect — this bound guards Meili-side parse/eval cost and pathological grant
    # sets. Conservative default; override via FILEARR_MEILI_SCOPE_FILTER_CEILING.
    meili_scope_filter_ceiling: int = 4096

    auth_enabled: bool = True

    # AGPL-3.0 §13: the running instance must offer users its Corresponding
    # Source. Exposed by GET /api/v1/version as ``source_url`` and rendered as the
    # footer "Source" link, so an operator running a FORK can point users at THEIR
    # modified source without rebuilding the frontend (the Vite build-time
    # __SOURCE_URL__ is only the fallback default). Override via FILEARR_SOURCE_URL.
    source_url: str = "https://github.com/filearr/filearr"

    # --- Phase 6 identity/auth/RBAC (P6-T1) ----------------------------------
    # Interactive session cookie name + lifecycle. Postgres-backed sessions
    # (research §1.3), NOT stateless JWT, so revocation is O(1) (delete the row).
    # Grafana's defaults: 30d absolute cap / 7d inactivity / 10min token
    # rotation. All FILEARR_SESSION_*-tunable. The cookie is HttpOnly +
    # SameSite=Strict always, and Secure whenever the request arrived over
    # https (honouring X-Forwarded-Proto from the Caddy TLS front — set uvicorn
    # --proxy-headers so request scheme is trustworthy behind the proxy).
    session_cookie_name: str = "filearr_session"
    session_ttl_hours: int = 720          # 30d absolute lifetime (hard cap)
    session_inactivity_hours: int = 168   # 7d idle window (sliding, per request)
    session_rotation_minutes: int = 10    # opaque-token rotation cadence
    # P6-T5 (OIDC SSO): the session-cookie SameSite policy. **Changed from the
    # P6-T1 default of ``strict`` to ``lax`` as a DELIBERATE P6-T5 ruling.** An
    # OIDC callback is a top-level *cross-site* navigation (the browser arrives
    # from the IdP origin); under ``SameSite=Strict`` the freshly-minted session
    # cookie is withheld on the callback's 302→``/`` redirect chain, so the user
    # lands logged-out and must reload. ``lax`` sends the cookie on top-level GET
    # navigations (the SSO return) while STILL withholding it on cross-site POST/
    # PATCH/DELETE and sub-resource requests — i.e. CSRF protection for every
    # state-changing endpoint is preserved (all mutations are non-GET JSON APIs;
    # the Bearer path is unaffected). ``lax`` is the standard session-cookie
    # choice for exactly this reason. Kept configurable for operators who run
    # local-only (no SSO) and prefer ``strict``.
    session_cookie_samesite: str = "lax"  # 'lax' (default, SSO-safe) | 'strict' | 'none'

    # --- Phase 6 P6-T8: brute-force rate limiting + lockout --------------------
    # Postgres-backed fixed-window counter + lock (NO Redis, NO in-memory slowapi
    # — the state must survive a restart AND be shared across workers). TWO
    # INDEPENDENT buckets are tracked per credential-check: the submitted USERNAME
    # string (catches a distributed brute force — many source IPs, one target
    # account — that no single per-IP counter would ever see) AND the source IP.
    # Either bucket crossing the threshold locks that bucket. A lock is checked
    # (429 + Retry-After) BEFORE the slow argon2 verify runs, so a locked account
    # costs ~one indexed SELECT. Authelia-pattern defaults: 3 failed attempts /
    # 2-min window / 5-min lock. Set FILEARR_AUTH_RATELIMIT_ENABLED=false to
    # disable entirely (the limiter becomes a byte-for-byte no-op).
    auth_ratelimit_enabled: bool = True
    auth_ratelimit_max_attempts: int = 3       # failures within the window → lock
    auth_ratelimit_window_seconds: int = 120   # 2-min find window
    auth_ratelimit_lock_seconds: int = 300     # 5-min lockout
    # When TRUE, the client IP is read from the LEFTMOST X-Forwarded-For entry
    # (the Caddy TLS sidecar sets it). Leave FALSE unless a trusted proxy is in
    # front — otherwise a client could spoof the header to evade the per-IP
    # bucket (the per-USERNAME bucket is unspoofable regardless).
    auth_ratelimit_trust_forwarded_for: bool = False

    # --- Phase 6 P6-T9: security audit log ------------------------------------
    # Opt-in READ/search auditing. Login/logout/lifecycle/grant events are ALWAYS
    # recorded; read-level audit (a 'search' event per query) is high-volume and
    # low-value outside multi-tenant SaaS, so it is OFF by default (brief §4).
    audit_reads: bool = False
    # Retention for the security_events table. Noisy ``login_failure`` rows are
    # purged after a shorter window; every other (higher-value) event is kept
    # longer. NOTE: distinct from ``audit_retention_days`` above (that governs the
    # ItemVersion metadata-change audit; a different table + trust model).
    security_audit_retention_days: int = 365          # non-failure events
    security_audit_failure_retention_days: int = 90   # login_failure events

    # --- Phase 6 P6-T5: OIDC / OpenID Connect SSO (Authlib RP) ---------------
    # Env-only provider config (no admin UI for providers — the issuer/secret are
    # server-config, not user data). SSO is a PURE ADDITION: with
    # ``oidc_enabled=false`` (default) nothing here engages and the login page is
    # byte-for-byte the P6-T1 local form. Enabling requires an issuer + client
    # credentials; the discovery document (``{issuer}/.well-known/openid-
    # configuration``) is fetched lazily and cached. Authlib pin (see
    # pyproject.toml) re-verified live at implementation (R5): the newest advisory
    # (GHSA-w8p2-r796-3vmq, 2026-06-08) is patched only in 1.6.10/1.7.1, so the
    # floor moved UP from the brief's >=1.6.9 to **>=1.7.1**.
    oidc_enabled: bool = False
    oidc_issuer: str | None = None            # e.g. https://auth.example.com/
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_scopes: str = "openid profile email"  # space-separated; must include openid
    # Optional explicit redirect URI (must match the IdP client registration). When
    # unset it is derived from the request base + /api/v1/auth/oidc/callback
    # (honouring X-Forwarded-Proto/Host behind the TLS front). Set this when the
    # public URL cannot be inferred from the request (unusual proxy setups).
    oidc_redirect_uri: str | None = None
    # Role mapping (evaluated at EVERY login so an IdP-side role change applies on
    # next login). ``oidc_role_claim`` names the ID-token claim carrying the user's
    # role/group values (str or list); ``oidc_role_map`` maps claim-value→Filearr
    # global role as "adminsGroup:admin,editors:user" (highest-privilege match
    # wins). ``oidc_default_role`` is applied when no mapping matches; set it EMPTY
    # to REFUSE unmapped users (fail-closed — no silent viewer access).
    oidc_role_claim: str | None = None
    oidc_role_map: str = ""                     # "claimval:role,claimval:role"
    oidc_default_role: str = "viewer"           # "" => refuse unmapped (fail-closed)
    # JIT provisioning: create a local ``users`` row (password_hash NULL — SSO-only)
    # on first login. Username from ``oidc_username_claim`` (falls back to the email
    # local-part, then sub) with a numeric collision suffix. Disable to require the
    # account to pre-exist (linked by issuer+subject).
    oidc_auto_provision: bool = True
    oidc_username_claim: str = "preferred_username"
    # Existing-user linking by verified email is an ACCOUNT-TAKEOVER surface (an IdP
    # that lets a user set any email could seize a local account), so it is OFF by
    # default and only ever links on an EXACT, IdP-asserted ``email_verified=true``
    # match. Leave false unless the IdP's email is trustworthy.
    oidc_link_by_email: bool = False
    # Optional IdP group sync: the ID-token claim (e.g. "groups") whose values are
    # matched BY NAME to EXISTING ``principal_groups`` (never auto-created). Groups
    # with source='oidc' are fully IdP-managed (added AND removed each login); a
    # source='local' group name-match is add-only (an admin's manual membership is
    # never clobbered). Unset => no group sync.
    oidc_group_claim: str | None = None
    # Server-side single-use login-state (CSRF ``state`` + ``nonce`` + PKCE
    # verifier) TTL. A callback presenting a state older than this (or already
    # consumed) is rejected.
    oidc_login_state_ttl_minutes: int = 10
    # SSRF/DoS hygiene for the discovery/JWKS/token back-channel fetches (issuer is
    # operator-trusted, but bound the blast radius regardless): per-request timeout,
    # a hard response-size cap, and a discovery/JWKS cache TTL.
    oidc_http_timeout_s: float = 10.0
    oidc_discovery_max_bytes: int = 1_048_576   # 1 MiB cap on discovery + JWKS
    oidc_metadata_cache_seconds: int = 3600     # discovery/JWKS cache TTL

    # --- Phase 6 P6-T6: LDAP / Active Directory bind auth (ldap3) -------------
    # Env-only provider config (like OIDC). With ``ldap_enabled=false`` (default)
    # nothing here engages and ``/auth/login`` is byte-for-byte the local form.
    # Library: **ldap3** (pure-Python, offline MOCK_SYNC test harness, no known
    # CVEs as of 2026-07-13) — a deliberate override of the research doc's
    # python-ldap preference, see docs/ops/auth.md § "LDAP library choice".
    #
    # Transport is TLS-first: an ``ldaps://`` server uses implicit TLS; an
    # ``ldap://`` server to a NON-loopback host is upgraded via StartTLS
    # (``ldap_start_tls=true``, the default) and REFUSED outright if StartTLS is
    # off unless the operator sets ``ldap_allow_plaintext=true`` (logged loudly).
    # Plaintext is never silent.
    ldap_enabled: bool = False
    ldap_server: str | None = None            # ldaps://dc.example.com or ldap://...
    ldap_start_tls: bool = True               # upgrade ldap:// via StartTLS
    ldap_allow_plaintext: bool = False        # escape hatch: ldap:// w/o TLS (warns)
    ldap_tls_verify: bool = True              # verify the server cert (default on)
    ldap_tls_ca_cert_file: str | None = None  # optional CA bundle for the server cert
    ldap_timeout: int = 10                    # connect + receive timeout (seconds)
    # Service account used to search for the user DN and read group membership.
    # Leave both empty for an anonymous search bind (only if the server allows it).
    ldap_bind_dn: str | None = None
    ldap_bind_password: str | None = None
    # Direct-bind template — set this to bind the USER's credentials straight to a
    # derived DN (no search), e.g. "uid={username},ou=people,dc=example,dc=com" or
    # AD "{username}@corp.example.com". {username} is DN-escaped. When set, the
    # user_base/user_filter search is skipped for authentication (a service bind
    # may still be used to read attributes/groups).
    ldap_user_dn_template: str | None = None
    # Search-then-bind: locate the user under ``user_base`` matching ``user_filter``
    # ({username} is LDAP-filter-escaped — injection-safe), then bind as the found
    # DN with the presented password.
    ldap_user_base: str | None = None
    ldap_user_filter: str = "(uid={username})"          # AD: (sAMAccountName={username})
    ldap_attr_username: str = "uid"                      # AD: sAMAccountName
    ldap_attr_email: str = "mail"
    # Stable external subject attribute. PREFER an immutable operational attribute
    # (OpenLDAP ``entryUUID`` / AD ``objectGUID``) over the DN, which changes when
    # an entry is renamed/moved. Falls back to the DN (with a warning) when the
    # attribute is absent.
    ldap_attr_uid: str = "entryUUID"
    # Group membership resolution. Two modes:
    #   * memberOf (``ldap_use_memberof=true``): read the user entry's ``memberOf``
    #     values (AD default; needs the overlay on OpenLDAP).
    #   * group search (default): search ``group_base`` for ``group_filter`` with
    #     {user_dn} substituted (filter-escaped).
    ldap_use_memberof: bool = False
    ldap_attr_memberof: str = "memberOf"
    ldap_group_base: str | None = None
    ldap_group_filter: str = "(member={user_dn})"       # AD: (member={user_dn})
    # Role map: group-DN => Filearr global role. Group DNs contain commas, so pairs
    # are ';'-separated and the DN/role delimiter is '=>':
    #   "cn=admins,ou=groups,dc=ex,dc=com=>admin;cn=staff,...=>user"
    # DNs are matched case-insensitively. Highest-privilege match wins.
    ldap_role_map: str = ""
    ldap_default_role: str = ""               # "" => REFUSE unmapped (fail-closed)
    # Auto-provision a local users row (password_hash NULL — SSO-only) on first
    # successful bind. Disable to require the account to pre-exist.
    ldap_auto_provision: bool = True
    # Sync LDAP groups → EXISTING principal_groups matched BY NAME (the group's CN),
    # source='ldap' (added AND removed each login; a source='local' name-match is
    # add-only). Unset => no group sync.
    ldap_group_sync: bool = False

    # UI-T4: server-side folder browser allowlist. GET /api/v1/fs/browse may only
    # list directories at or under one of these roots; any request that normalizes
    # or symlink-resolves OUTSIDE every root is rejected (422). Default is the
    # single-volume media convention. Override via FILEARR_BROWSE_ROOTS (JSON list,
    # e.g. '["/data","/mnt/media"]').
    browse_roots: list[str] = ["/data"]

    # Scanning
    # --- P9-T7: Postgres<->Meili reconciliation sweep -----------------------
    # Bounded worst-case index staleness (safety net behind the no-retry Meili
    # webhooks). ``reconcile_max_fixes`` caps mutations per hourly sweep so a
    # large divergence is repaired incrementally instead of in one huge batch;
    # the overflow is carried to the next sweep. ``reconcile_tolerance`` is the
    # count delta below which the cheap compare treats the projection as in-sync
    # (0 = exact; the sweep already skips while the index queue is draining, so a
    # nonzero tolerance is rarely needed).
    reconcile_max_fixes: int = 10_000
    reconcile_tolerance: int = 0
    reconcile_pg_chunk: int = 5_000  # server-side yield_per for id streaming
    reconcile_meili_page: int = 1_000  # /documents pagination batch

    # --- P9-T5: shadow-index swap rebuild ----------------------------------
    # A full rebuild builds a fresh shadow index, backfills it from Postgres,
    # then atomically swaps it in — concurrent searches never see a half-built
    # index. ``meili_rebuild_wait_s`` is the TOTAL wall-clock budget for waiting
    # on the shadow's Meili tasks (settings + every document batch) to finish
    # before the swap; on timeout the job fails cleanly leaving the live index
    # untouched and the partial shadow deleted. ``meili_shadow_max_age_hours``
    # is the age past which an orphaned shadow (from a crashed/retried rebuild)
    # is reaped by the maintenance sweep. Disk headroom note: a rebuild holds
    # BOTH the live and shadow copies on disk simultaneously (~2x the index
    # size) until the post-swap delete — same LMDB constraint as compaction.
    meili_rebuild_wait_s: float = 900.0
    meili_shadow_max_age_hours: int = 6
    meili_rebuild_batch: int = 1_000  # Postgres->shadow backfill page size

    scan_hash_full_max_bytes: int = 1_073_741_824
    scan_batch_size: int = 500
    recycle_retention_days: int = 30

    # --- FIX-8: procrastinate job-history retention ------------------------
    # Terminal procrastinate rows (succeeded / failed / cancelled / aborted)
    # older than this window are hard-deleted by the daily ``purge_job_history``
    # maintenance task so the failed-jobs list (Admin + Jobs pages) and the
    # succeeded-job backlog can never grow unbounded. todo/doing jobs are NEVER
    # touched regardless of age. Override via FILEARR_JOB_HISTORY_RETENTION_DAYS.
    # Note: because succeeded rows also power the Jobs page queue-card "done"
    # counters + the extract-rate ETA, those become WINDOWED (last N days) once a
    # purge has run — healthier, not a regression. Default 14d.
    job_history_retention_days: int = 14

    # --- P4-T9: ItemVersion audit retention -------------------------------
    # Attributed extractor-sourced audit rows (source='scan'/'extract:<type>')
    # are purged past this window by the daily ``purge_item_versions`` task so
    # per-rescan audit growth stays bounded. source='user' (API/UI edit) rows are
    # ALWAYS exempt — a manual edit's history is never auto-purged regardless of
    # age. Override via FILEARR_AUDIT_RETENTION_DAYS. Default 90d.
    audit_retention_days: int = 90

    # --- Extraction throughput controls (T8) ---------------------------------
    # Jobs are split across dedicated Procrastinate queues so a large scan's
    # extraction backlog cannot starve scan-control or maintenance work:
    #   scan        walk/diff/tombstone (one long job per library)
    #   extract     per-file metadata + hashing (the high-volume queue)
    #   index       Meili projection sync
    #   maintenance periodic purge/reconcile/schedule ticks
    # Queue names are settings so an operator can pin a second worker to a
    # single queue (see docker-compose.yml scale-out notes) without code edits.
    queue_scan: str = "scan"
    queue_extract: str = "extract"
    queue_index: str = "index"
    queue_maintenance: str = "maintenance"

    # Default per-worker concurrency and the queue set a worker serves. These
    # back the FILEARR_WORKER_* env the compose worker command reads; they are
    # surfaced here so the values are documented in one place and testable.
    worker_concurrency: int = 4
    worker_queues: str = ""  # empty = all queues; else comma-separated list

    # --- UI-T14: per-task-class default job priorities ----------------------
    # Procrastinate orders the ``todo`` queue by ``priority DESC, id ASC`` -- a
    # HIGHER integer runs SOONER (VERIFIED against procrastinate 3.9's
    # ``procrastinate_jobs_priority_idx`` and the fetch-job ORDER BY; default 0).
    # These defaults are applied at EVERY defer site so an operator can retune
    # queue precedence without code edits (and, at runtime, bump the pending jobs
    # of a queue via POST /api/v1/system/jobs/priority). A job's priority is fixed
    # at defer time: only ``todo`` jobs are reordered -- a job already ``doing`` is
    # never preempted. Bounds are -100..100 (the runtime endpoint clamps to this).
    #   scan (0)         the pipeline's front stage -- the default lane.
    #   extract (-10)    below scan so a freshly-triggered scan/cancel is never
    #                    queued behind a 5k-file extract backlog on a shared worker.
    #   index (0)        Meili projection sync -- tiny I/O, default lane.
    #   embed (-20)      lowest -- slow local ONNX inference must never starve
    #                    per-file extraction (see the semantic block below).
    #   maintenance (0)  periodic purge/reconcile/schedule ticks -- default lane.
    #   alerts (5)       ABOVE the default lane for user-facing timeliness so a
    #                    dispatch pump is not stuck behind a big scan/extract run.
    scan_priority: int = 0
    extract_priority: int = -10
    index_priority: int = 0
    maintenance_priority: int = 0
    alerts_priority: int = 5

    # --- UI-T14: staged scan -> extract pipeline ----------------------------
    # When TRUE (default -- the user asked for this), a scan does NOT trickle
    # per-batch extract jobs out mid-walk; it accumulates the new/changed item ids
    # and defers ALL extraction in one chunked pass at scan END (completed OR
    # gracefully stopped). This keeps the scan's directory walk and the extract
    # workers' hashing/parsing from hitting the same disk/network at once -- the
    # scan finishes fast, then post-processing runs. TRADEOFF: extracted metadata
    # appears only AFTER the scan completes, rather than trickling in during the
    # walk. Additionally, while staged mode is on, an ``extract_item`` whose
    # library is still being walked reschedules itself (see
    # ``extract_reschedule_seconds``) so a leftover old-queue extract never fights
    # a fresh scan's I/O. Set FALSE to restore the trickle-in-during-scan
    # behaviour. Global this round (no per-library override column yet -- future).
    staged_pipeline: bool = True
    # Delay (seconds) an ``extract_item`` waits before re-checking the staged gate
    # when a scan is walking its library. Rescheduling is attempt-agnostic (it
    # never burns the job's real failure-retry budget -- see extract.py).
    extract_reschedule_seconds: int = 120

    # --- FIX-6: stalled-job reaper thresholds --------------------------------
    # A worker restart/crash can leave jobs stranded in ``doing`` with no live
    # worker (procrastinate SETs job.worker_id NULL when the worker row is
    # pruned). The maintenance reaper (worker.reap_stalled_jobs) requeues or
    # fails such orphans. Two independent nets, matching procrastinate's own
    # heartbeat semantics plus an absolute-age backstop:
    #   * heartbeat net (ALL doing jobs): a job whose worker_id is NULL or whose
    #     worker has not heartbeat within ``job_stall_heartbeat_seconds`` is
    #     stalled. Mirrors JobManager.get_stalled_jobs' default (30 s) and should
    #     track procrastinate's update_heartbeat_interval / stalled_worker_timeout.
    #   * age net (NON-scan doing jobs only): a per-file/index/alert job doing for
    #     longer than ``job_stall_seconds`` is stalled regardless of heartbeat (an
    #     extract should never take an hour). scan_library is EXEMPT from the age
    #     net — a legitimate full-library walk can run long; it is reaped only via
    #     the heartbeat net when its worker truly dies.
    job_stall_heartbeat_seconds: int = 30
    job_stall_seconds: int = 3600

    # --- FIX-15 (ScanRuns stuck in 'stopping'/'running') --------------------
    # The graceful-stop transition (``stopping`` -> ``stopped``) only happens
    # inside a LIVE scan worker's between-batch check, and the reaper only
    # transitions a running/stopping ScanRun when it detects a *stalled ``doing``
    # scan job* that same tick. A ``stopping`` (or ``running``) ScanRun whose job
    # is GONE -- succeeded, failed, cancelled/aborted, or purged from job history
    # -- therefore has no stalled job to reap and never converges to a terminal
    # state, blocking the scheduler's running-row guard for that library forever.
    # The maintenance reconciler (worker.reconcile_orphaned_scan_runs_now) closes
    # this: a non-terminal ScanRun with NO scan_library job in todo/doing/aborting
    # for its (library, scope), older than this grace period, is finalized
    # (``stopping`` -> ``stopped`` honoring the operator's stop intent;
    # ``running`` -> ``failed`` per invariant 7). The grace window protects a
    # scan whose job row is momentarily not yet visible right after enqueue.
    scan_run_reconcile_grace_seconds: int = 600

    # --- FIX-8 (scan-scheduling storm) --------------------------------------
    # Furthest-back cron occurrence the minute scheduler will catch up to after
    # the scheduler/worker was down. Only the single LATEST missed occurrence
    # inside this window fires (never a per-slot backfill); occurrences older
    # than this are skipped and wait for the next one. Also bounds cronsim
    # iteration for a very frequent expression after a long outage. 48h covers
    # hourly/daily comfortably. See schedule.due_occurrence.
    scan_schedule_max_catchup_minutes: int = 2880
    # Reaper requeue budget for a stalled NON-scan doing job (FIX-8): the reaper
    # requeues an orphaned job (worker died) back to todo, but a job whose worker
    # keeps dying (e.g. an OOM loop) would otherwise be requeued unboundedly --
    # the live box saw attempts=50/51. Once a job has been attempted this many
    # times it is FAILED instead of requeued (a genuinely stuck job surfaces on
    # the failed-jobs list rather than looping forever). scan_library orphans are
    # always FAILED regardless (never requeued), so this cap governs the
    # extract/index/alert/maintenance requeue path only.
    reap_max_attempts: int = 10

    # Video extraction (ffprobe). ffprobe_path may be a bare name resolved on
    # PATH or an absolute path. Runtime is bounded and output size capped so a
    # hostile or oversized file cannot stall or OOM an extract worker.
    ffprobe_path: str = "ffprobe"
    ffprobe_timeout_s: float = 30.0
    ffprobe_max_output_bytes: int = 8_388_608  # 8 MiB cap on ffprobe JSON

    # 3D model extraction (trimesh). trimesh loads the whole mesh into RAM, so a
    # size ceiling caps memory before a hostile/huge asset can OOM a worker.
    # STEP/FBX/BLEND are not parsed for geometry here (no safe pure loader) — they
    # only get a lightweight file-fact record.
    model3d_max_bytes: int = 268_435_456  # 256 MiB ceiling on files handed to trimesh

    # --- P3-T1: on-demand cryptographic digests (MD5/SHA-256) ---------------
    # POST /api/v1/items/{id}/digests streams the file ONCE and caches the hex
    # digests in metadata_ (extracted fact, invariant 2). digest_max_bytes is a
    # hard ceiling: a file larger than this is rejected (413) rather than tying up
    # a worker streaming tens of GB over SMB -- the whole point of the on-demand
    # (not scan-time) design. digest_timeout_s bounds the client wait on the
    # threadpool computation (the underlying read is not force-killed, but the
    # request never hangs indefinitely). Both overridable via FILEARR_DIGEST_*.
    digest_max_bytes: int = 50 * 1024**3  # 50 GiB
    digest_timeout_s: float = 900.0

    # Document / spreadsheet extraction. Zip-based formats (docx/xlsx/3mf) are a
    # zip-bomb vector, so archives are size-capped and pypdf is told never to
    # touch the network. Metadata only (no text/cell extraction — that is v2).
    document_max_bytes: int = 268_435_456  # 256 MiB ceiling on doc/spreadsheet files

    # --- P3-T6: OCR pipeline (Tesseract, per-library opt-in, hash-gated cache) ---
    # Global default OFF (R4): OCR only runs for a library whose ``ocr_enabled``
    # column is true, so the default install pays ZERO OCR cost. ``ocr_min_text_chars``
    # is the native-text threshold below which a page/image is considered "no usable
    # text layer" and worth OCRing (Paperless=50, Docspell=500; we pick 100 per the
    # brief). ``ocr_max_pages``/``ocr_max_pixels``/``ocr_timeout_s`` are the recurring
    # page-count / pixel / wall-clock ceilings (ocrmypdf/Paperless/Docspell). The
    # binaries (tesseract, pdftoppm) ship in the Docker runtime stage; paths are
    # settings so an operator can override / point at an absolute path. ``ocr_max_chars``
    # caps the OCR text STORED in metadata_ (index-bloat + row-size control), reusing
    # the body-text-index cap on the projection side.
    ocr_enabled: bool = False                 # global default OFF (R4)
    ocr_min_text_chars: int = 100
    ocr_max_pages: int = 10                    # scanned-PDF page ceiling (rasterise+OCR)
    ocr_max_pixels: int = 40_000_000          # per-image pixel ceiling (~40 MP)
    ocr_timeout_s: float = 120.0              # per-subprocess wall clock
    ocr_max_chars: int = 100_000              # cap on OCR text stored in metadata_
    ocr_dpi: int = 200                        # pdftoppm rasterisation DPI
    ocr_lang: str = "eng"
    ocr_tesseract_path: str = "tesseract"
    ocr_pdftoppm_path: str = "pdftoppm"

    # --- P3-T11: EXIF deep extraction + GPS default-hidden gate (R5, CWE-1230) ---
    # exiftool (Perl binary, shipped in the Docker runtime stage) is invoked per
    # file as an external subprocess (never exiv2 in-process linking — GPL linking
    # ambiguity). Curated camera/lens/exposure/dimension/GPS fields are written into
    # metadata_ under the ``exif.`` namespace (P4 R5). GPS keys are stored RAW but
    # stripped from the projection + API unless the library's ``expose_gps`` is true.
    # ``gps_expose_default`` is ONLY the per-library toggle's default and stays false
    # (R5 — there is deliberately NO global default-true path).
    gps_expose_default: bool = False
    exiftool_path: str = "exiftool"
    exif_timeout_s: float = 30.0
    exif_max_output_bytes: int = 8_388_608    # 8 MiB cap on exiftool JSON

    # --- P3-T5: document body-text extraction (search snippets/highlighting) --
    # Two distinct caps (both documented so the difference is explicit):
    #   body_text_max_chars  — the ceiling on the text STORED in metadata_.body_text
    #     (Postgres). Snippets don't need whole novels; 100k chars is plenty of
    #     matchable context while keeping a row bounded. body_text_truncated flags
    #     when this (or a page/read ceiling) clipped the content.
    #   body_text_index_chars — the (smaller) ceiling on what build_doc PROJECTS
    #     into Meili's searchable body_text attribute. Index-bloat control: the
    #     first ~20k chars carry the overwhelming majority of useful matches, so we
    #     index a prefix rather than the full stored body. Old docs indexed before
    #     P3-T5 lack body_text until a rebuild-index re-projects them (invariant 1).
    body_text_max_chars: int = 100_000
    body_text_index_chars: int = 20_000
    # Decompression-bomb guard for zip-based office files (docx/xlsx), checked
    # against the zip central directory BEFORE any parser opens the archive: reject
    # when declared uncompressed total exceeds doc_decompressed_max, OR the overall
    # ratio exceeds doc_decompression_ratio once the payload already exceeds
    # doc_decompression_ratio_min_bytes (so an ordinary tiny, highly-compressible
    # office file is never falsely rejected — only a genuine ratio bomb is).
    doc_decompressed_max: int = 209_715_200  # 200 MiB total uncompressed
    doc_decompression_ratio: float = 100.0  # uncompressed:compressed
    doc_decompression_ratio_min_bytes: int = 10_485_760  # 10 MiB

    # --- P3-T13: archive member listing (guarded, index-only) ----------------
    # Surface an archive's contents (zip/cbz/jar + tar/tgz/tar.gz/tar.bz2/tar.xz)
    # as searchable member names WITHOUT unpacking. zip-family reuses the SAME
    # doc_decompression_* central-directory bomb guard above (checked BEFORE any
    # member is enumerated). tar-family has no central directory, so headers are
    # streamed under two bounds: a member COUNT cap and a compressed-stream BYTE
    # ceiling past which listing stops cleanly (truncated) -- a decompression-bomb
    # tar can never force unbounded work.
    #   archive_max_members          -- enumeration cap (count of listed members).
    #   archive_members_stored       -- {name,size} entries persisted in metadata_.
    #   archive_members_index_chars  -- flat searchable archive_members string cap
    #     (applied at STORE time in Postgres AND re-applied by build_doc when it
    #     projects the Meili searchable attribute -- index-bloat control).
    #   archive_scan_max_bytes       -- compressed-stream ceiling for tar listing.
    archive_max_members: int = 10_000
    archive_members_stored: int = 1_000
    archive_members_index_chars: int = 20_000
    archive_scan_max_bytes: int = 67_108_864  # 64 MiB compressed stream
    # --- P3-T8: local semantic-search embedder (bge-small-en-v1.5, ONNX) -----
    # Semantic/hybrid search is GLOBALLY OFF by default so a default install pays
    # ZERO cost: with ``semantic_enabled=false`` no model is ever loaded, no
    # vectors are computed, and the Meili ``userProvided`` embedder settings are
    # never applied. When enabled, a successful extract defers a LOWEST-priority
    # ``embed_item`` job that computes a dense vector with a LOCAL ONNX model
    # (fastembed, Apache-2.0 -- never a cloud API; private files never leave the
    # box) and stores it in ``metadata_._embedding`` (+ ``_embedding_fp`` drift
    # tag). The live-LXC benchmark (2026-07-12) pinned BAAI/bge-small-en-v1.5
    # (dim 384; ~40 texts/s; query 39 ms; ~490 MB RSS) -- ARCHITECT RULING. The
    # ~130 MB ONNX model downloads once into ``embed_model_cache`` (persistent
    # /config volume). ``embedder_concurrency`` stays 1 (ONE memory-capped, lowest
    # priority worker); ``embed_batch`` is the model inference batch (32).
    semantic_enabled: bool = False
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_dim: int = 384
    embed_version: str = "1"  # fingerprint disambiguator for two builds of one id
    embed_batch: int = 32
    embedder_concurrency: int = 1
    embed_model_cache: str = "/config/models"
    # FIX-7: the display model name (embed_model) stays "BAAI/bge-small-en-v1.5"
    # but the ACTUAL ONNX artifact is a separate HF repo/file (fastembed served
    # the same pair). ``embed_model_repo`` is what huggingface_hub downloads;
    # ``embed_model_file`` is the .onnx inside it. Both feed the drift
    # fingerprint so a change of served artifact forces a re-embed.
    embed_model_repo: str = "Qdrant/bge-small-en-v1.5-onnx-Q"
    embed_model_file: str = "model_optimized.onnx"
    # onnxruntime intra-op thread cap for the ONE embed worker. Conservative
    # default (1) per the live-Proxmox benchmark ruling; raise to trade the
    # box's spare cores for throughput once extraction is not competing.
    embed_threads: int = 1
    # Per-run cap on the ``embed_missing`` backfill: at most this many ``embed_item``
    # jobs are deferred per invocation (the remainder is picked up next run) so a
    # 750k-item first-enable never enqueues an unbounded burst.
    embed_backfill_batch: int = 5_000
    # The embed stage runs on its OWN queue at a priority BELOW extract (-10) so
    # slow local inference never starves per-file metadata extraction on a shared
    # worker (ONE embed worker, lowest priority -- R3/ruling).
    queue_embed: str = "embed"
    embed_priority: int = -20
    # --- Phase 8 alerting (P8-T2/T4) -----------------------------------------
    # FILEARR_SECRET_KEY is the envelope key for channel-secret encryption
    # (AES-GCM; the 32-byte content key is sha256(secret_key)). It is held
    # OUTSIDE Postgres so a stolen DB dump exposes no channel credentials. It is
    # REQUIRED to store/read channel secrets: when unset, the alert-channels API
    # returns 503 (never a plaintext fallback). Generate one with, e.g.,
    #   python -c "import secrets; print(secrets.token_urlsafe(48))"
    secret_key: str | None = None
    # R5: single boolean widening the webhook SSRF guard to permit the PRIVATE
    # class ONLY (RFC1918 / IPv6 ULA) for trusted-LAN webhook targets. Loopback,
    # link-local (cloud metadata 169.254.169.254), reserved and unspecified stay
    # denied regardless. Admin/server-config level, NOT a per-rule toggle.
    webhook_allow_private_cidrs: bool = False
    # Outbound webhook dispatch hygiene (brief §7.1 pt 4): bound the wall-clock of
    # one POST and cap the response body a hostile endpoint can make us read.
    alert_webhook_timeout_s: float = 10.0
    alert_webhook_max_response_bytes: int = 65536
    # Freshness window (seconds) baked into the signed X-Filearr-Signature so a
    # receiver can reject replays; also the tolerance our own test-fire uses.
    alert_signature_max_age_s: int = 300
    # R4/P8-T15: global per-rule hourly dispatch ceiling (storm safety net). The
    # config lands now; the dispatch-path enforcement is P8-T15.
    alert_rule_max_per_hour: int = 100
    # P8-T7/T8 dispatch pump tunables (state-derived windowing over alert_events).
    # group_interval: minimum gap before re-notifying an already-notified group
    # that has gathered NEW matches (Alertmanager semantics). max_delivery_attempts:
    # a group's dispatch is retried on TRANSIENT failures up to this ceiling, then
    # goes terminal (delivery_attempts == ceiling, still undelivered, last_error set).
    # digest_max_events: cap on events enumerated in a grouped/digest body before an
    # "and N more" tail (keeps a pathological glob from producing a megabyte message).
    alert_group_interval_s: int = 300
    alert_max_delivery_attempts: int = 5
    alert_digest_max_events: int = 50
    # P8-T10: extract-error-spike system rule threshold (errors ADDED per library
    # within the rolling window). Seeds the is_system rule's threshold_count; the
    # pump snapshots the GIN-indexed extract_error_counts_by_library() aggregate
    # every tick and fires when a library's increase over the window exceeds it.
    alert_error_spike_threshold: int = 50
    alert_error_spike_window_s: int = 3600
    # P8-T14: retention (days) for TERMINAL alert_events (delivered OR
    # retries-exhausted). Pending/held rows are NEVER purged regardless of age.
    alert_events_retention_days: int = 30

    # --- Phase 12 thumbnails (S12/P12 slice 1) --------------------------------
    # Content-addressed WebP thumbnail cache under ``{config_dir}/thumbnails``.
    # DISPOSABLE projection (invariant 1 discipline applied to a derived store):
    # every byte is rebuildable from the still-live source via ``thumb_item``.
    # Default ON -- image + audio-cover generation is cheap (in-process Pillow,
    # no subprocess) and the search/browse grid always wants a thumb. Set
    # FILEARR_THUMBS_ENABLED=false to pay zero thumbnail cost.
    thumbs_enabled: bool = True
    # Two tiers, one decode, two resizes (research ladder). ``grid`` is
    # pregenerated at extract ride-along; ``preview`` is lazy (generated on the
    # first serve-path miss). Longest-edge px, WebP quality start, and a HARD
    # per-file byte cap (over cap even at the quality floor => store nothing +
    # record an error; never let the derivative store exceed the source --
    # the Nextcloud failure mode the research calls out).
    thumbnail_grid_px: int = 320
    thumbnail_preview_px: int = 800
    thumbnail_grid_quality: int = 70
    thumbnail_preview_quality: int = 78
    thumbnail_grid_max_bytes: int = 20_000
    thumbnail_preview_max_bytes: int = 60_000
    # WebP quality is stepped DOWN this ladder until the encoded bytes fit the
    # tier cap; below the floor the thumbnail is abandoned (no oversized store).
    thumbnail_quality_floor: int = 40
    thumbnail_quality_step: int = 10
    # Source-decode guard (untrusted, possibly hostile images): a source whose
    # decoded pixel count exceeds this ceiling is rejected BEFORE resize so a
    # decompression bomb cannot OOM a worker (mirrors ocr_max_pixels / T6's
    # "check size before the parser" discipline; also Pillow's own
    # MAX_IMAGE_PIXELS bomb guard stays armed).
    thumbnail_max_pixels: int = 50_000_000  # ~50 MP
    # PDF first-page render pixel budget (P12-T5). pypdfium2 renders page 1 at a
    # scale that targets the tier's longest edge, so the output is inherently
    # bounded by ``tier.max_edge`` (<=800px => <1 MP); this ceiling is a
    # belt-and-braces cap on the RENDER buffer so a page declaring absurd
    # dimensions can never balloon the intermediate bitmap. ~16 MP.
    thumbnail_pdf_max_pixels: int = 16_000_000
    # Baked into the content-addressed cache key: bumping it makes every existing
    # thumbnail simply unaddressed by new requests (lazy regeneration, no mass
    # invalidation storm); the GC sweep reclaims the old-version files.
    thumbnail_generator_version: int = 1
    # Dedicated low-priority queue so a post-version-bump regeneration backlog
    # never delays a concurrently running scan/extract. Priority is BELOW extract
    # (-10): a search row renders a placeholder while its thumb is queued, but the
    # row is not searchable until extraction runs.
    queue_thumbnail: str = "thumbs"
    thumbs_priority: int = -15
    # Serve-path inline generation-on-miss concurrency ceiling (semaphore): bounds
    # how many hostile/large images can be decoded in request handlers at once.
    thumbnail_inline_concurrency: int = 4
    # Daily orphan GC (mandatory per the Nextcloud postmortem): reclaim thumbnail
    # files whose manifest row is gone and manifest rows whose backing file/item
    # is gone. ``thumbnail_gc_max_files`` bounds the fanout walk per run so a
    # multi-million-file cache is swept incrementally.
    thumbnail_gc_max_files: int = 100_000
    # P12-T13 agent-plane thumbnail upload: the HARD size cap on an agent-generated
    # thumbnail blob accepted by ``POST /agents/{id}/thumbs`` (256 KiB). A thumbnail
    # over this is refused (413) — a derivative can never approach the source size
    # (the same posture as the per-tier byte caps above). Agents encode JPEG (no
    # pure-Go lossy WebP under CGO_ENABLED=0); the blob is stored under central's
    # ``<key>.webp`` name and served by sniffed content-type.
    thumbnail_agent_max_bytes: int = 256 * 1024

    # --- Phase 12 slice 2: video poster-frames (ffmpeg, OPS-T7 QSV-ready) ------
    # Video thumbnails grab a single decoded frame via ffmpeg (same subprocess
    # posture as ffprobe.py: argv list, hard timeout, output cap, `--` before the
    # untrusted path). ``ffmpeg_path`` may be a bare name resolved on PATH or an
    # absolute path (mirrors ``ffprobe_path``).
    ffmpeg_path: str = "ffmpeg"
    # Hard wall-clock cap on a single frame-grab (decode + scale + encode). A
    # hostile/damaged container that makes ffmpeg spin is killed here.
    thumb_ffmpeg_timeout_s: float = 60.0
    # Seek target = max(this, 10% of duration), clamped below duration-0.5s for
    # short clips. The floor skips black-frame / logo intros (research §gen).
    thumbnail_video_min_seek_s: float = 1.0
    # Cap on the raw PNG frame ffmpeg writes to stdout before it reaches Pillow.
    # The frame is scaled to the tier edge INSIDE ffmpeg, so this is a generous
    # backstop (a tier-edge PNG is small); an over-cap frame is discarded.
    thumbnail_video_max_frame_bytes: int = 33_554_432  # 32 MiB
    # Hardware acceleration policy for the frame decode (OPS-T7): ``auto`` probes
    # for ``/dev/dri/renderD128`` ONCE and, when present, tries ``-hwaccel qsv``
    # first, falling back to software transparently (and logging ONCE per boot) on
    # any nonzero exit; ``off`` forces software. VAAPI is the same swappable
    # policy slot if a deployment prefers it. No device access ever depends on
    # request input -- the accel choice is an operator setting only.
    thumb_accel: str = "auto"  # auto | off
    # HDR poster tonemap: when the source is HDR (ffprobe metadata_.hdr /
    # smpte2084 / arib-std-b67 transfer) attempt a zscale->tonemap->zscale chain
    # so the poster is not washed-out grey. The chain is FRAGILE on mistagged
    # sources, so a tonemap failure transparently retries the plain scale (a
    # washed-out poster beats no poster). Disable to always take the plain path.
    thumb_hdr_tonemap: bool = True
    # P12-T12 storage-budget soft alarm: /stats logs a WARNING when the total
    # thumbnail_manifest byte sum exceeds this. Advisory only -- never blocks
    # generation (the per-file byte caps are the hard guard).
    thumbnail_total_budget_bytes: int = 5_368_709_120  # 5 GiB

    # --- FIX-11: filesystem-full guardrails + low-space alerting -------------
    # A LIVE INCIDENT (thumbnail generation filled /config and crashed Postgres)
    # motivates these floors. Producers writing to /config or tmp fail-closed at
    # the CRITICAL floor (filearr.diskguard.guard_write) and a 5-minutely monitor
    # (filearr.tasks.diskmon) fires an OPS ALERT on warn/critical transitions.
    # TWO floors are evaluated per path and the MORE CONSERVATIVE wins: an
    # absolute-GB floor and a percent-of-total floor. Neither alone is safe across
    # deploy sizes (5 GB is trivial on a 40 GB LXC rootfs but ample on a 4 TB NAS;
    # 2% is huge on a 4 TB volume but nothing on a 40 GB one).
    disk_min_free_gb: float = 5.0      # CRITICAL when free < this (absolute floor)
    disk_warn_free_gb: float = 20.0    # WARN when free < this (absolute floor)
    disk_crit_pct_free: float = 2.0    # CRITICAL when free% < this (percent floor)
    disk_warn_pct_free: float = 10.0   # WARN when free% < this (percent floor)
    # Explicit override of the monitored paths (JSON list). Empty => derived
    # defaults: {config_dir}/thumbnails, the tmp dir, and disk_pg_path if set.
    disk_watch_paths: list[str] = []
    # Postgres data dir, monitored ONLY when visible to THIS process. In the
    # compose stack Postgres runs in its own container/volume (invisible here) so
    # this stays unset; in the single-volume LXC deploy /config already shares the
    # PG filesystem, so watching the thumbnail dir covers Postgres too. When set
    # AND critical, the staged pipeline PAUSES deferring new extract/thumb work.
    disk_pg_path: str | None = None
    # Producer statvfs cache TTL: a per-file producer loop pays one syscall every
    # this-many seconds, not one per file.
    disk_guard_cache_s: float = 5.0
    # Periodic disk monitor master switch (the 5-minutely diskmon task).
    disk_monitor_enabled: bool = True
    # Emergency-GC LRU target: at critical the monitor runs the thumbnail orphan
    # GC AGGRESSIVELY; when this is > 0 it ALSO LRU-evicts valid (oldest-first,
    # by generated_at) thumbnails until the cache filesystem has this many GB
    # free. 0 (default) => orphan-GC only (no valid-thumb eviction). Thumbnails
    # are a DISPOSABLE projection (invariant 1) so eviction only costs regen.
    disk_gc_target_free_gb: float = 0.0

    # --- P11-T5/T9/T11: background report exports + scheduled delivery -------
    # Async report/export jobs stream to a diskguarded staging file under
    # ``{config_dir}/exports`` (never a web-served static root); a periodic sweep
    # purges expired artifacts (row retained, ``purged_at`` set) and flips a
    # crashed ``running`` export to ``failed`` (invariant-7 reconcile). The sync
    # run endpoints stay for interactive use (bounded by ``MAX_LIMIT``).
    export_dir: str | None = None          # None => {config_dir}/exports
    # Cheap advisory ceiling for the sync-vs-async decision the UI/clients make;
    # the sync endpoints themselves still hard-cap at reports.MAX_LIMIT.
    export_sync_max_rows: int = 10_000
    # Absolute row ceiling for a background export (bounds a runaway job).
    export_max_rows: int = 1_000_000
    # P11-T11 per-principal concurrency cap: max in-flight (queued/running)
    # background exports a single scoped principal may hold. A manual enqueue
    # beyond this returns 429 (schedule-triggered runs are NOT capped). An
    # unrestricted actor (admin / API key / auth-off) is exempt.
    export_max_active: int = 3
    # Artifact time-to-live (hours): after this the file is purged and the row's
    # ``purged_at`` is stamped (audit trail retained). Default 7 days.
    export_ttl_hours: int = 168
    # Transient-delivery / job retry budget for a scheduled export delivery.
    export_max_delivery_attempts: int = 5
    # Dedicated LOW-priority Procrastinate queue so a large export never starves
    # scan/extract capacity (research §7). Priority below extract (-10).
    exports_priority: int = -20
    # P11-T9 scheduled delivery: an email channel ATTACHES the artifact when its
    # size is <= this ceiling, else falls back to a link-style summary message
    # (research OQ2). Webhook delivery NEVER embeds the file — always a JSON
    # summary + a download URL.
    report_email_max_bytes: int = 10_485_760  # 10 MiB
    # Absolute base URL used to build the download link handed to a webhook /
    # link-fallback email (e.g. "https://filearr.example.com"). Unset => a
    # site-relative "/api/v1/exports/<id>/download" path is sent instead.
    public_base_url: str | None = None

    # --- Phase 5 distributed agents (P5-T1) ---------------------------------
    # Master switch for the distributed-agent fleet surface. OFF by default:
    # agents are a v3 OPT-IN and a single-node deploy must be entirely
    # unaffected. When false the /agents API returns 404 and the Admin → Agents
    # panel stays hidden; the tables still exist (empty) so enabling later needs
    # no migration. The step-ca CA runs as an OPTIONAL compose profile
    # (`--profile agents`), never in the default stack (Apache-2.0, but agents
    # are opt-in) — see docs/ops/agents.md.
    agents_enabled: bool = False
    # Enrollment-token TTL (minutes). Minutes-to-hours, NOT days (research §7.1):
    # the token is the single human-copy-paste weak link, so its blast-radius
    # window is kept short. Default 1h.
    enrollment_token_ttl_minutes: int = 60
    # step-ca bootstrap info handed back to a registering agent so it can pin the
    # CA root and drive the CSR/cert flow (P5-T2). Empty until an operator stands
    # up the CA (docs/ops/agents.md). NEVER a secret — the root fingerprint is
    # public pinning material, not a credential.
    ca_url: str = ""                       # e.g. https://ca.filearr.lan:9000
    ca_fingerprint: str = ""               # step-ca root fingerprint (pin)
    ca_provisioner: str = "filearr-agents" # JWK/ACME provisioner name for agents
    # Advisory agent client-cert lifetime (hours), surfaced in bootstrap info.
    # 24-72h range (research §1.3): short blast-radius for a stolen cert, long
    # enough that a brief central outage does not strand agents. Default 48h.
    agent_cert_ttl_hours: int = 48
    # --- P5-T2 (central half): step-ca JWK one-time token (OTT) minting -------
    # The provisioner's DECRYPTED private JWK (a JSON string; EC P-256/ES256
    # expected). This is a SECRET on par with FILEARR_SECRET_KEY -- env-held for
    # the single-operator model (document rotation), treated like a credential
    # and NEVER logged. When UNSET (or malformed), registration STILL succeeds
    # but the register response's ``ca_ott`` is null: agents enroll but cannot
    # obtain a client cert until it is set (fail-safe -- a bad key never takes
    # registration down; agentsync.load_provisioner_jwk logs the failure MODE,
    # never the key). Extract it from step-ca's provisioner config and decrypt
    # the encryptedKey -- see docs/ops/agents.md.
    ca_provisioner_jwk: str | None = None
    # OTT lifetime (seconds). SHORT -- it is a single-use bearer the agent
    # exchanges with step-ca immediately after register. Default 5 minutes.
    ca_ott_ttl_seconds: int = 300
    # P10-T1 agent_commands primitive tunables (all FILEARR_AGENT_COMMAND_*).
    # Default TTL for a newly enqueued command (seconds): a command an agent
    # never picks up flips to ``expired`` after this. "Hours, not minutes"
    # (research §5); a per-kind default (stat_check short, stage_upload longer)
    # is P10-T7 -- for now one default, overridable per-enqueue up to the ceiling.
    agent_command_ttl_seconds: int = 3600
    agent_command_ttl_max_seconds: int = 86400  # clamp on an enqueue TTL override
    # A delivered-but-unacked command is presumed dropped after the lease; the
    # sweep then re-queues it (at-least-once) up to ``max_attempts`` deliveries.
    agent_command_lease_seconds: int = 300
    agent_command_max_attempts: int = 5
    agent_command_poll_max: int = 50  # ceiling on how many a single poll drains
    # Size caps (bytes of compact JSON): a hostile/buggy caller cannot bloat a
    # row. Enqueue payload is small (a path + flags); a result may carry hashes.
    agent_command_payload_max_bytes: int = 16384
    agent_command_result_max_bytes: int = 65536
    # P5-T4 replication apply path: hard cap on entries in one replication batch
    # POSTed to /agents/{id}/replication-batch. A batch above this is rejected
    # (413) so a hostile/buggy agent cannot force an unbounded single-transaction
    # apply. The agent's outbox drains in bounded slices anyway (§4.2).
    agent_replication_max_entries: int = 1000
    # P5-T5 full-manifest reconciliation sweep (§4.4). The agent pages its whole
    # manifest to /agents/{id}/reconcile/{session}/rows; a page above this many
    # rows is rejected (413) so one POST cannot force an unbounded staging insert
    # (the sweep pages in bounded slices anyway). A reconcile session whose
    # ``started_at`` is older than the TTL is treated as expired (404 on
    # rows/finish; swept opportunistically at the next start) so an abandoned
    # sweep frees its staging without a dedicated periodic task.
    agent_reconcile_page_max: int = 5000
    agent_reconcile_session_ttl_seconds: int = 3600
    # P5-T6 config/policy push: hard cap (bytes of compact JSON) on ONE policy
    # payload PUT to /agent-policies/{scope}. A body above this is rejected (413)
    # so a hostile/buggy operator cannot bloat a policy_versions row. The policy
    # is small (preset names + a handful of glob lists + scalar tunables); 64 KiB
    # is generous headroom for the forward-compat unknown-key passthrough.
    agent_policy_max_bytes: int = 65536
    # --- W6-D3 extensible inventory framework --------------------------------
    # Hard cap (bytes of compact JSON) on the additive ``capabilities`` object an
    # agent attaches to a command poll (inventory collector vocabulary + version).
    # Small by nature; a body above this is refused (413) so a hostile/buggy agent
    # cannot bloat the row it is stored on.
    agent_capabilities_max_bytes: int = 16384
    # Hard cap (bytes) on a gzip-NDJSON inventory RESULT blob POSTed to
    # /agents/{id}/inventory-results. A small result inlines in the command
    # completion (agent_command_result_max_bytes); a larger one uploads here. 8 MiB
    # mirrors the thumbnail/small-blob posture (NOT the multi-GB staging plane).
    agent_inventory_result_max_bytes: int = 8 * 1024 * 1024
    # Directory the inventory-results receiver writes ``<command_id>.ndjson.gz``
    # under. None => ``{config_dir}/inventory`` (writable central disk, not a media
    # mount — invariant 6).
    inventory_dir: str | None = None
    # --- P5-T6 agent-plane mTLS enforcement ----------------------------------
    # How the agent plane (replication / reconcile / policy / commands) proves an
    # agent's identity. Three modes (all preserve the 401/403/404 semantics):
    #
    #   "fingerprint" (default) — the INTERIM pre-mTLS scheme: the agent presents
    #     its bound ``cert_fingerprint`` as a bearer token. Unchanged behaviour;
    #     a single-node/LAN deploy that never fronts agents behind the mTLS proxy
    #     stays on this. NOTE the renewal-drift caveat (docs/ops/agents.md §6):
    #     the fingerprint rotates on cert renewal, so a long-lived fleet must pin
    #     FILEARR_AGENT_AUTH_FINGERPRINT or migrate to mtls-header.
    #
    #   "mtls-header" — the agent connects over mTLS to the Caddy ``agents.<domain>``
    #     site, which verifies the client cert against the step-ca root and forwards
    #     the VERIFIED identity as trusted headers, guarded by a shared secret:
    #       X-Filearr-Proxy-Auth  the shared secret (compare_digest vs the setting)
    #       X-Filearr-Agent-San   the client cert's first DNS SAN == str(agent_id)
    #       X-Filearr-Agent-Fp    the client cert fingerprint (secondary check)
    #     Identity is the SAN (== agent_id) — renewal-PROOF, so the fingerprint-drift
    #     caveat dies. The bearer token is REFUSED in this mode (the weaker path is
    #     shut off once flipped). Requires FILEARR_PROXY_SHARED_SECRET.
    #
    #   "both" — transition: a request carrying the proxy-auth header is validated
    #     via the mtls-header path (hard-fails on a bad secret/SAN — no silent
    #     downgrade); a request WITHOUT it falls back to the bearer path. Lets an
    #     operator flip to "both", migrate agents to https://agents.<domain>, then
    #     flip to "mtls-header" with zero downtime.
    agent_auth_mode: str = "fingerprint"
    # Shared secret the Caddy mTLS proxy stamps on X-Filearr-Proxy-Auth so the
    # backend can trust the forwarded (already-verified) client identity. A SECRET
    # on par with FILEARR_SECRET_KEY — auto-generated once by the deploy script
    # (openssl rand), env-held, never logged. REQUIRED for mtls-header/both; when
    # unset those modes refuse every agent-plane request (fail closed).
    proxy_shared_secret: str | None = None
    # --- P5-T7 signed update manifest + staged rollout -----------------------
    # Where uploaded agent-release artifact BINARIES live (manifests are in the
    # agent_releases table). None => {config_dir}/agent-releases. Each release's
    # files sit under <dir>/<version>/<filename>; the agent-authed download
    # endpoint serves ONLY filenames listed in that release's stored manifest
    # (no path traversal — the filename is looked up, never joined blindly).
    agent_releases_dir: str | None = None
    # The rollout_group whose agents receive stage='canary' releases before the
    # operator promotes them to 'general' (R5). Everyone sees 'general'; only this
    # group sees an un-promoted 'canary'. Rename per deployment if desired.
    agent_canary_group: str = "canary"
    # Hard ceiling (bytes) on a single uploaded release artifact — bounds a
    # hostile/buggy admin upload and the agent's own download read.
    agent_update_max_artifact_bytes: int = 536_870_912  # 512 MiB
    # --- P8-T11: agent-offline + replication-stall ops alerts -----------------
    # Both drive the two SEEDED (disabled-by-default) system alert rules evaluated
    # by the 5-minutely filearr.tasks.agentmon tick. No-ops when agents_enabled is
    # false or the agent tables are absent.
    #
    # Agent-offline threshold (seconds): a cert-bound, non-revoked agent whose
    # ``last_seen_at`` is older than this fires "System: agent offline". The
    # default is DELIBERATELY generous (48h) because OFFLINE IS A NORMAL AGENT
    # STATE (research §7.4): a laptop agent that sleeps every night, or a desktop
    # powered off over a long weekend, must NOT page anyone. Operators who run
    # always-on server agents can tighten this per-deployment.
    agent_offline_alert_seconds: int = 172_800  # 48h
    # Replication-stall threshold (seconds): the SHARPER signal — an agent that IS
    # alive (seen within the offline threshold) but whose replication has gone
    # quiet. Fires "System: agent replication stalled" when the newest of its
    # replication-ledger ``applied_at`` / ``last_reconcile_at`` watermark is older
    # than this. Tighter than offline (6h) because a live-but-silent agent is a
    # real fault (broken outbox drain, wedged sync) rather than an expected sleep.
    agent_replication_stall_alert_seconds: int = 21_600  # 6h
    # --- P10-T4 resumable agent->central staging data plane -------------------
    # Where staged agent-upload bodies land: writable central disk, NOT a media
    # mount (R5 -- invariant 6 untouched). None => {config_dir}/staging. On-disk
    # name is the transfer UUID (transfers.staging_path_for) so it is traversal-
    # proof by construction. Share this volume with the app's persistent storage;
    # size it for the sum of in-flight retrievals (bounded by the TTL sweep, P10-T8).
    staging_dir: str | None = None
    # TTL (seconds) for a staging_transfers row from creation. "Hours, not
    # minutes" (research §5): a staged file is trivially re-fetchable, so losing
    # one early is an inconvenience, not data loss. Default 24h. Actual reaping is
    # P10-T8; this only stamps ``expires_at`` at attach.
    staging_transfer_ttl_seconds: int = 86_400  # 24h
    # Hard ceiling (bytes) on a single upload PATCH chunk body -- bounds one
    # request's memory/disk write and mirrors the agent's own chunk size with
    # headroom. A chunk above this is 413. The agent streams 8 MiB chunks by
    # default; 64 MiB leaves room for a larger operator-tuned chunk.
    staging_max_chunk_bytes: int = 67_108_864  # 64 MiB
    # --- P10-T13 RBAC-gated transfer API --------------------------------------
    # TTL (seconds) for a retrieve initiated via POST /items/{id}/transfer: the
    # stage_upload command's TTL AND the staging_transfers row's expires_at. Pins
    # open question 3 ("hours, not minutes" — LONGER than stat_check's TTL,
    # ``agent_command_ttl_seconds`` 1h): an offline agent is the NORMAL case
    # (research §5), so a retrieve waits patiently and a staged file survives to be
    # (re-)downloaded within the window. Default 6h. Actual reaping is P10-T8.
    transfer_ttl_seconds: int = 21_600  # 6h
    # --- P10-T8 staging TTL cleanup sweep -------------------------------------
    # The maintenance sweep (worker.cleanup_staging_transfers) bounds central
    # staging disk: a staging_transfers row past its ``expires_at`` is moved to
    # ``expired`` and its staged file deleted, EXCEPT when a download is actively
    # draining it -- a slow client whose ``last_range_request_at`` is within this
    # grace window is left untouched so an in-flight download is never cut
    # mid-stream (the watermark api/transfers.download stamps on every request).
    # Default 1h -- generous headroom for a large file over a slow link.
    staging_download_grace_seconds: int = 3_600  # 1h
    # A PARTIAL upload (state pending/uploading) that has made NO progress for
    # this long -- measured from the row's ``updated_at`` (bumped on every PATCH
    # append) -- is an abandoned transfer: the agent went away mid-upload and is
    # not resuming. It is reclaimed on THIS shorter schedule rather than waiting
    # for the full 24h attach TTL, so a died-mid-upload staged prefix does not sit
    # on disk for a day. Default 6h (a stalled upload well past any real network
    # hiccup); still comfortably longer than a legitimate pause between chunks.
    staging_abandoned_upload_seconds: int = 21_600  # 6h
    # --- P10-T10 agent identity / online status (item-detail UI) ---------------
    # An agent is shown "online" in the item-detail agent-status panel when its
    # ``last_seen_at`` (refreshed on every command poll / replication batch) is
    # within this window. DELIBERATELY tighter than ``agent_offline_alert_seconds``
    # (48h): that generous value avoids paging for a sleeping laptop, but a live
    # "online now" badge must reflect actual recent contact, not a two-day-old
    # heartbeat. Default 300s (a few poll intervals of headroom); an always-on
    # server agent stays green, an offline one flips within minutes.
    agent_online_threshold_seconds: int = 300  # 5m

    @field_validator("agent_auth_mode")
    @classmethod
    def _valid_agent_auth_mode(cls, v: str) -> str:
        allowed = {"fingerprint", "mtls-header", "both"}
        val = (v or "").strip().lower()
        if val not in allowed:
            raise ValueError(
                f"FILEARR_AGENT_AUTH_MODE must be one of {sorted(allowed)}, got {v!r}"
            )
        return val

    @field_validator("database_url")
    @classmethod
    def _require_psycopg(cls, v: str) -> str:
        if v.startswith("postgresql://"):
            v = v.replace("postgresql://", "postgresql+psycopg://", 1)
        return v

    @property
    def oidc_scope_list(self) -> list[str]:
        """The requested OIDC scopes as a list (``openid`` is force-included)."""
        scopes = [x for x in self.oidc_scopes.replace(",", " ").split() if x]
        if "openid" not in scopes:
            scopes = ["openid", *scopes]
        return scopes

    @property
    def oidc_role_map_parsed(self) -> dict[str, str]:
        """Parse ``oidc_role_map`` ("val:role,val:role") → {claim_value: role}.
        Malformed pairs (no colon, unknown role) are dropped (fail-safe)."""
        out: dict[str, str] = {}
        valid = {"admin", "user", "viewer"}
        for pair in self.oidc_role_map.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            k, _, v = pair.partition(":")
            k, v = k.strip(), v.strip().lower()
            if k and v in valid:
                out[k] = v
        return out

    @property
    def oidc_is_configured(self) -> bool:
        """True when OIDC is enabled AND minimally configured (issuer + client id).
        Gates the ``/auth/status`` flag and the login/callback endpoints so a
        half-configured provider fails closed (endpoints 404) rather than 500."""
        return bool(
            self.auth_enabled
            and self.oidc_enabled
            and self.oidc_issuer
            and self.oidc_client_id
        )

    @property
    def ldap_role_map_parsed(self) -> dict[str, str]:
        """Parse ``ldap_role_map`` ("dn=>role;dn=>role") → {lowercased_dn: role}.

        Group DNs contain commas, so pairs are ';'-separated and the DN/role
        delimiter is '=>'. DNs are lower-cased for case-insensitive matching.
        Malformed pairs (no '=>', unknown role) are dropped (fail-safe)."""
        out: dict[str, str] = {}
        valid = {"admin", "user", "viewer"}
        for pair in self.ldap_role_map.split(";"):
            pair = pair.strip()
            if not pair or "=>" not in pair:
                continue
            dn, _, role = pair.rpartition("=>")
            dn, role = dn.strip().lower(), role.strip().lower()
            if dn and role in valid:
                out[dn] = role
        return out

    @property
    def ldap_is_configured(self) -> bool:
        """True when LDAP is enabled AND minimally configured (a server plus a way
        to locate the user: a DN template or a search base). Gates the login
        fall-through and the ``/auth/status`` flag so a half-configured provider
        fails closed (login simply never attempts LDAP) rather than erroring."""
        return bool(
            self.auth_enabled
            and self.ldap_enabled
            and self.ldap_server
            and (self.ldap_user_dn_template or self.ldap_user_base)
        )

    @property
    def worker_queue_list(self) -> list[str] | None:
        """Parse ``worker_queues`` into a list of queue names, or ``None`` for
        "all queues". Whitespace/empties are dropped so ``"extract, index"`` and
        ``"extract,index"`` are equivalent; an all-blank value means all queues."""
        names = [q.strip() for q in self.worker_queues.split(",") if q.strip()]
        return names or None

    @property
    def embedder_config(self):
        """The configured local embedder identity (the drift-fingerprint basis).

        Imported lazily so ``config`` never hard-depends on ``embed`` at import
        time (and the optional model libs stay out of import)."""
        from filearr.embed import EmbedderConfig

        return EmbedderConfig(
            model_id=self.embed_model,
            dim=self.embed_dim,
            version=self.embed_version,
            repo=self.embed_model_repo,
            model_file=self.embed_model_file,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
