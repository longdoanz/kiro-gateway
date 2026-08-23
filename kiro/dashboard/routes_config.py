import json

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from kiro.dashboard.deps import require_admin
from kiro.dashboard.schemas import SystemConfigResponse, SystemConfigUpdate
from kiro.db.engine import get_session
from kiro.db.models import User
from kiro.db.repositories import get_all_config, set_config

router = APIRouter(prefix="/config", tags=["config"])

CONFIG_DEFAULTS = {
    "enable_model_override": "false",
    "model_override_rules": "[]",
    "model_override_default": "auto",
    "enable_usage_sharing": "false",
    "enable_nine_router_model_override": "false",
    "nine_router_model_override_rules": "[]",
    "nine_router_model_override_default": "auto",
    "enable_nine_router_direct": "false",
}

_OVERRIDE_KEYS = {"enable_model_override", "model_override_rules", "model_override_default"}
_NINE_ROUTER_OVERRIDE_KEYS = {"enable_nine_router_model_override", "nine_router_model_override_rules", "nine_router_model_override_default"}
_NINE_ROUTER_DIRECT_KEYS = {"enable_nine_router_direct"}


def _to_response(raw: dict[str, str]) -> SystemConfigResponse:
    merged = {**CONFIG_DEFAULTS, **raw}
    from kiro.model_override import _parse_rules
    rules_raw = _parse_rules(merged.get("model_override_rules", "[]"))
    nr_rules_raw = _parse_rules(merged.get("nine_router_model_override_rules", "[]"))
    return SystemConfigResponse(
        enable_model_override=merged["enable_model_override"].lower() == "true",
        model_override_rules=rules_raw,
        model_override_default=merged.get("model_override_default", "auto"),
        enable_usage_sharing=merged["enable_usage_sharing"].lower() == "true",
        enable_nine_router_model_override=merged["enable_nine_router_model_override"].lower() == "true",
        nine_router_model_override_rules=nr_rules_raw,
        nine_router_model_override_default=merged.get("nine_router_model_override_default", "auto"),
        enable_nine_router_direct=merged["enable_nine_router_direct"].lower() == "true",
    )


@router.get("", response_model=SystemConfigResponse, response_model_by_alias=True)
async def get_config_route(admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    raw = await get_all_config(session)
    return _to_response(raw)


@router.put("", response_model=SystemConfigResponse, response_model_by_alias=True)
async def update_config_route(body: SystemConfigUpdate, admin: User = Depends(require_admin), session: AsyncSession = Depends(get_session)):
    updates = body.model_dump(exclude_unset=True, by_alias=True)
    for key, value in updates.items():
        if isinstance(value, bool):
            str_value = str(value).lower()
        elif isinstance(value, list):
            str_value = json.dumps(value)  # already dicts with "from" key via by_alias=True
        else:
            str_value = str(value)
        await set_config(session, key, str_value)
    raw = await get_all_config(session)
    if "enable_usage_sharing" in updates:
        from kiro.usage.fallback import fallback_router
        fallback_router.update_sharing_config(str(updates["enable_usage_sharing"]).lower() == "true")
    if _OVERRIDE_KEYS & set(updates.keys()):
        from kiro.model_override import invalidate_cache
        invalidate_cache()
    if _NINE_ROUTER_OVERRIDE_KEYS & set(updates.keys()):
        from kiro.nine_router_client import invalidate_nine_router_override_cache
        invalidate_nine_router_override_cache()
    if _NINE_ROUTER_DIRECT_KEYS & set(updates.keys()):
        from kiro.nine_router_client import invalidate_nine_router_direct_cache
        invalidate_nine_router_direct_cache()
    return _to_response(raw)
