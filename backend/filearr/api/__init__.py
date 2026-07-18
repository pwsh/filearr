from fastapi import APIRouter

from filearr.api import (
    agent_commands,
    agent_config_groups,
    agent_inventory,
    agent_policies,
    agent_share_maps,
    agent_staging,
    agent_thumbs,
    agent_updates,
    agents,
    alerts,
    audit,
    auth,
    custom_fields,
    custom_reports,
    digests,
    exports,
    fs,
    item_agent_status,
    items,
    libraries,
    metadata_profiles,
    oidc,
    presets,
    query,
    rbac,
    report_schedules,
    reports,
    saved_searches,
    scan_paths,
    scans,
    search,
    stats,
    system,
    transfers,
)

v1_router = APIRouter()
v1_router.include_router(system.router, tags=["system"])
v1_router.include_router(auth.router, tags=["auth"])
v1_router.include_router(audit.router, tags=["audit"])
v1_router.include_router(oidc.router, tags=["auth"])
v1_router.include_router(rbac.router, tags=["rbac"])
v1_router.include_router(fs.router, prefix="/fs", tags=["fs"])
v1_router.include_router(search.router, tags=["search"])
v1_router.include_router(
    saved_searches.router, prefix="/saved-searches", tags=["saved-searches"]
)
v1_router.include_router(items.router, prefix="/items", tags=["items"])
v1_router.include_router(digests.router, prefix="/items", tags=["items"])
v1_router.include_router(item_agent_status.router, prefix="/items", tags=["items"])
v1_router.include_router(
    custom_fields.router, prefix="/custom-fields", tags=["custom-fields"]
)
v1_router.include_router(libraries.router, prefix="/libraries", tags=["libraries"])
v1_router.include_router(presets.router, prefix="/presets", tags=["presets"])
v1_router.include_router(
    metadata_profiles.router, prefix="/metadata-profiles", tags=["metadata-profiles"]
)
v1_router.include_router(scan_paths.router, prefix="/libraries", tags=["scan-paths"])
v1_router.include_router(scans.router, prefix="/scans", tags=["scans"])
v1_router.include_router(stats.router, prefix="/stats", tags=["stats"])
v1_router.include_router(reports.router, prefix="/reports", tags=["reports"])
v1_router.include_router(
    custom_reports.router, prefix="/custom-reports", tags=["custom-reports"]
)
v1_router.include_router(exports.router, prefix="/exports", tags=["exports"])
v1_router.include_router(
    report_schedules.router, prefix="/report-schedules", tags=["report-schedules"]
)
v1_router.include_router(query.router, prefix="/query", tags=["query"])
v1_router.include_router(alerts.router, tags=["alerts"])
v1_router.include_router(transfers.router, tags=["transfers"])
v1_router.include_router(agents.router, tags=["agents"])
v1_router.include_router(agent_commands.router, tags=["agents"])
v1_router.include_router(agent_config_groups.router, tags=["agents"])
v1_router.include_router(agent_inventory.router, tags=["agents"])
v1_router.include_router(agent_policies.router, tags=["agents"])
v1_router.include_router(agent_share_maps.router, tags=["agents"])
v1_router.include_router(agent_staging.router, tags=["agents"])
v1_router.include_router(agent_thumbs.router, tags=["agents"])
v1_router.include_router(agent_updates.router, tags=["agents"])
