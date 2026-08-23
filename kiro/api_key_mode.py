# -*- coding: utf-8 -*-

# Kiro Gateway
# https://github.com/jwadow/kiro-gateway
# Copyright (C) 2025 Jwadow
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""
API Key Mode support for Kiro Gateway.

When API_KEY_MODE=true, clients supply their own Kiro API key per-request
via the Authorization: Bearer <kiro_api_key> header.
Server-side credentials (REFRESH_TOKEN, KIRO_CREDS_FILE, KIRO_CLI_DB_FILE)
are not required in this mode.

The client's Bearer token is forwarded directly to the Kiro API.
"""

import asyncio
import json
import time
import uuid
from typing import Any, List, Optional

import httpx
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from kiro.config import (
    BASE_RETRY_DELAY,
    FALLBACK_MODELS,
    FIRST_TOKEN_MAX_RETRIES,
    MAX_RETRIES,
    MODEL_CACHE_TTL,
    REGION,
    STREAMING_READ_TIMEOUT,
    get_kiro_api_host,
    get_kiro_q_host,
)
from kiro.model_override import apply_model_override
from kiro.utils import get_machine_fingerprint  # noqa: F401 - may be used by external code
from kiro.usage.scheduler import is_db_configured


# Module-level cache for /v1/models in API_KEY_MODE: api_key -> (models_list, fetched_at)
_model_cache: dict = {}  # api_key -> (models_list, fetched_at)
_model_cache_lock = asyncio.Lock()


async def get_models_cached(api_key: str, app_state) -> List[dict]:
    """
    Lazily fetch available models from Kiro using the caller's API key.

    Caches results per api_key for MODEL_CACHE_TTL seconds. Falls back to
    FALLBACK_MODELS on any error.

    Args:
        api_key: Kiro API key supplied by the client.
        app_state: FastAPI app.state (unused, kept for interface symmetry).

    Returns:
        List of model dicts as returned by Kiro's ListAvailableModels endpoint.
    """
    global _model_cache

    now = time.time()
    cached = _model_cache.get(api_key)
    if cached is not None and now - cached[1] < MODEL_CACHE_TTL:
        return cached[0]

    async with _model_cache_lock:
        cached = _model_cache.get(api_key)
        if cached is not None and now - cached[1] < MODEL_CACHE_TTL:
            return cached[0]

        q_host = get_kiro_q_host(REGION)
        url = f"{q_host}/ListAvailableModels"
        headers = build_api_key_headers(api_key)
        params = {"origin": "AI_EDITOR"}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.get(url, headers=headers, params=params)
            if response.status_code == 200:
                models = response.json().get("models", [])
                _model_cache[api_key] = (models, now)
                return models
            raise Exception(f"HTTP {response.status_code}")
        except Exception as e:
            logger.warning(f"API_KEY_MODE: model fetch failed ({e}), using fallback")

    return FALLBACK_MODELS


async def get_usage_limits(api_key: str, resource_type: str = "AGENTIC_REQUEST") -> dict:
    """
    Fetch usage limits from Kiro API using the caller's API key.

    Args:
        api_key: Kiro API key supplied by the client.
        resource_type: Resource type to query (default: AGENTIC_REQUEST).

    Returns:
        Raw JSON response from Kiro's getUsageLimits endpoint.

    Raises:
        HTTPException: On auth failure or unexpected errors.
    """
    q_host = get_kiro_q_host(REGION)
    url = f"{q_host}/getUsageLimits"
    headers = build_api_key_headers(api_key, stream=False)
    params = {"origin": "AI_EDITOR", "resourceType": resource_type}

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=headers, params=params)

        if response.status_code == 200:
            return response.json()

        if response.status_code == 403:
            raise HTTPException(
                status_code=401,
                detail="Invalid Kiro API key (received 403 from Kiro API)",
            )

        raise HTTPException(
            status_code=response.status_code,
            detail=f"Kiro API returned {response.status_code}: {response.text[:500]}",
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"API_KEY_MODE: getUsageLimits failed: {e}")
        raise HTTPException(status_code=502, detail=f"Failed to fetch usage limits: {e}")


def extract_bearer_token(auth_header: Optional[str]) -> Optional[str]:
    """
    Extract the token value from an Authorization: Bearer <token> header.

    Args:
        auth_header: Raw Authorization header value, or None.

    Returns:
        The token string, or None if the header is absent or not Bearer format.
    """
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:]
    return None


def get_api_key_from_request(request: Request) -> str:
    """
    Extract the Kiro API key from the incoming request's Authorization header.

    In API_KEY_MODE the client's Bearer token IS the Kiro API key and is
    forwarded directly to the Kiro API without any server-side refresh.

    Args:
        request: FastAPI Request object.

    Returns:
        The Kiro API key string.

    Raises:
        HTTPException: 401 if the header is missing or not in Bearer format.
    """
    token = extract_bearer_token(request.headers.get("Authorization"))
    if not token:
        raise HTTPException(
            status_code=401,
            detail="API_KEY_MODE is enabled: supply your Kiro API key as 'Authorization: Bearer <key>'"
        )
    return token


def get_token_fingerprint(token: str) -> str:
    """
    Generates a unique fingerprint from a Kiro API token.

    In API Key Mode, each user has their own token, so we derive
    a unique fingerprint per token. This makes each user look like
    a separate Kiro IDE installation to AWS, which is more natural
    than all users sharing the same server-level fingerprint.

    Args:
        token: Kiro API key supplied by the client.

    Returns:
        SHA256 hex digest of the token (64 chars).
    """
    from kiro.db.repositories import hash_api_key
    return hash_api_key(token)


def build_api_key_headers(
    token: str,
    stream: bool = False,
    attempt: int = 1,
    max_attempts: Optional[int] = None,
    invocation_id: Optional[str] = None,
) -> dict:
    """
    Build Kiro API request headers using a caller-supplied token.

    Produces the same header set as kiro.utils.get_kiro_headers() but
    accepts the token directly instead of obtaining it from an auth manager.
    Uses a per-token fingerprint so each API key looks like a separate
    Kiro IDE installation.

    Args:
        token: Kiro access token supplied by the client.
        stream: If True, use streaming endpoint headers (generateAssistantResponse).
        attempt: Current attempt number (1-based). Used in amz-sdk-request header.
        max_attempts: Max attempts for this invocation. Defaults to 3 (stream) or 1 (non-stream).
        invocation_id: Stable UUID for amz-sdk-invocation-id. Generated fresh if not provided.

    Returns:
        Dictionary of HTTP headers ready for use with httpx.
    """
    from kiro.config import (
        KIRO_IDE_VERSION, KIRO_SDK_VERSION, KIRO_OS_STRING,
        KIRO_NODEJS_VERSION, KIRO_API_MODULE, KIRO_API_MODULE_VERSION,
        KIRO_M_FLAGS,
        KIRO_STREAMING_SDK_VERSION, KIRO_STREAMING_API_MODULE,
        KIRO_STREAMING_API_MODULE_VERSION, KIRO_STREAMING_M_FLAGS,
    )
    fingerprint = get_token_fingerprint(token)

    if stream:
        sdk_ver = KIRO_STREAMING_SDK_VERSION
        api_mod = KIRO_STREAMING_API_MODULE
        api_mod_ver = KIRO_STREAMING_API_MODULE_VERSION
        m_flags = KIRO_STREAMING_M_FLAGS
        default_max = 3
    else:
        sdk_ver = KIRO_SDK_VERSION
        api_mod = KIRO_API_MODULE
        api_mod_ver = KIRO_API_MODULE_VERSION
        m_flags = KIRO_M_FLAGS
        default_max = 1

    effective_max = max_attempts if max_attempts is not None else default_max
    effective_invocation_id = invocation_id if invocation_id is not None else str(uuid.uuid4())

    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": (
            f"aws-sdk-js/{sdk_ver} ua/2.1 os/{KIRO_OS_STRING} "
            f"lang/js md/nodejs#{KIRO_NODEJS_VERSION} "
            f"api/{api_mod}#{api_mod_ver} "
            f"m/{m_flags} KiroIDE-{KIRO_IDE_VERSION}-{fingerprint}"
        ),
        "x-amz-user-agent": f"aws-sdk-js/{sdk_ver} KiroIDE-{KIRO_IDE_VERSION}-{fingerprint}",
        "x-amzn-codewhisperer-optout": "true",
        "x-amzn-kiro-agent-mode": "vibe",
        "amz-sdk-invocation-id": effective_invocation_id,
        "amz-sdk-request": f"attempt={attempt}; max={effective_max}",
        "Connection": "close",
        "tokentype": "API_KEY",
    }




async def _resolve_gateway_key(token: str) -> tuple[str, int, int] | None:
    """If token is a gateway key (iziaigw_ prefix), resolve it to a Kiro key.

    Prefers the user's own Kiro keys first; falls back to the shared system
    key pool only when the user's keys are exhausted or unavailable.

    Uses sticky binding via StickyKeyBinder to preserve upstream prompt cache.

    Returns (kiro_raw_key, kiro_key_id, gateway_key_id) or None if not a gateway key.
    """
    if not token.startswith("iziaigw_"):
        return None
    if not is_db_configured():
        return None
    try:
        from kiro.db.repositories import get_gateway_key_by_hash, hash_api_key
        from kiro.usage.usage_cache import usage_cache, sticky_binder

        key_hash = hash_api_key(token)
        from kiro.db.engine import async_session_factory

        user_key_ids: list[int] = []
        async with async_session_factory() as session:
            gk = await get_gateway_key_by_hash(session, key_hash)
            if gk is None or not gk.is_active:
                return None
            if gk.user_id is not None:
                from kiro.db.models import ApiKey as ApiKeyModel
                from sqlalchemy import select as sa_select
                result = await session.execute(
                    sa_select(ApiKeyModel.id).where(
                        ApiKeyModel.user_id == gk.user_id,
                        ApiKeyModel.is_active == True,
                    )
                )
                user_key_ids = list(result.scalars().all())

        gk_id = gk.id

        # Prefer user's own Kiro keys — only require active + not temp-exhausted,
        # not quota ratio, since the key may not have synced limits yet.
        if user_key_ids:
            user_available = [kid for kid in user_key_ids if usage_cache.is_key_usable(kid)]
            if user_available:
                best_key_id, raw_kiro_key = await sticky_binder.pick_and_decrypt(gk_id, user_available)
                logger.info(f"Gateway key {gk_id} resolved to user's own key {best_key_id}")
                return raw_kiro_key, best_key_id, gk_id

        # Fall back to shared system key pool
        system_available = usage_cache.get_available_keys(system_only=True)
        if system_available:
            best_key_id, raw_kiro_key = await sticky_binder.pick_and_decrypt(gk_id, system_available)
            logger.info(f"Gateway key {gk_id} resolved to system key {best_key_id} (user keys exhausted or absent)")
            return raw_kiro_key, best_key_id, gk_id

        # Last resort: any available user key in the pool
        all_available = usage_cache.get_available_keys()
        if not all_available:
            logger.info("Gateway key used but no Kiro keys available in pool")
            return None

        best_key_id, raw_kiro_key = await sticky_binder.pick_and_decrypt(gk_id, all_available)
        logger.info(f"Gateway key {gk_id} resolved to user key {best_key_id} (no system keys, using pool fallback)")
        return raw_kiro_key, best_key_id, gk_id
    except Exception as e:
        logger.debug(f"Gateway key resolution failed: {e}")
        return None


async def _track_gateway_key_usage(gateway_key_id: int, input_tokens: int = 0, output_tokens: int = 0, model: str = "unknown", key_id: int | None = None) -> None:
    if not is_db_configured():
        return
    try:
        from kiro.db.engine import async_session_factory
        from kiro.db.repositories import get_canonical_usage_key_id, increment_gateway_key_usage
        from kiro.usage.daily_buffer import gateway_key_daily_buffer
        import time
        month = time.strftime("%Y-%m")
        async with async_session_factory() as session:
            canonical_key_id = key_id
            if key_id is not None:
                canonical_key_id = await get_canonical_usage_key_id(session, key_id)
            await increment_gateway_key_usage(session, gateway_key_id, month, 1, key_id=canonical_key_id)
        today = time.strftime("%Y-%m-%d")
        gateway_key_daily_buffer.record(gateway_key_id, today, input_tokens, output_tokens, model=model, key_id=canonical_key_id)
    except Exception as e:
        logger.debug(f"Gateway key usage tracking failed: {e}")


async def _resolve_gateway_key_id_only(token: str) -> int | None:
    """Look up just the gateway_key_id for an iziaigw_ token, without resolving a Kiro key.

    Used by the 503 fallback path (pool empty) so 9router usage can still be
    attributed to the gateway key. Returns None when not a gateway key, DB is
    unconfigured, the key is unknown/inactive, or on any error.
    """
    if not token.startswith("iziaigw_") or not is_db_configured():
        return None
    try:
        from kiro.db.engine import async_session_factory
        from kiro.db.repositories import get_gateway_key_by_hash, hash_api_key
        key_hash = hash_api_key(token)
        async with async_session_factory() as session:
            gk = await get_gateway_key_by_hash(session, key_hash)
        if gk is None or not gk.is_active:
            return None
        return gk.id
    except Exception as e:
        logger.debug(f"Gateway key id resolution failed: {e}")
        return None


def _make_nine_router_usage_cb(gateway_key_id: int | None):
    """
    Build an on_usage callback for the 9router proxy fallback.

    When a request falls back to 9router the Kiro key pool served nothing, so we
    do NOT touch per-Kiro-key usage. We only record gateway-key usage (request
    count + daily tokens) so the gateway-key dashboard still reflects the traffic.
    Returns None when there is nothing to track (no gateway key / no DB).
    """
    if gateway_key_id is None or not is_db_configured():
        return None

    async def _cb(input_tokens: int, output_tokens: int, model: str) -> None:
        # key_id=None: usage is attributed to the gateway key only, not a Kiro key.
        await _track_gateway_key_usage(
            gateway_key_id, input_tokens, output_tokens, model=model, key_id=None
        )

    return _cb


async def _resolve_key_id(token: str) -> int | None:
    """Look up an existing key_id for token. Does NOT create new keys."""
    if not is_db_configured():
        return None
    from kiro.db.engine import async_session_factory
    from kiro.db.repositories import get_api_key_by_hash, hash_api_key
    from kiro.usage.token_cache import token_cache
    try:
        key_h = hash_api_key(token)
        cached = await token_cache.get(key_h)
        if cached is not None:
            return cached

        async with async_session_factory() as session:
            existing = await get_api_key_by_hash(session, key_h)
        if existing is not None:
            try:
                await token_cache.set(key_h, existing.id)
            except Exception:
                pass
            return existing.id
        return None
    except Exception as e:
        logger.debug(f"Failed to look up key: {e}")
        return None


async def _persist_new_key(token: str) -> None:
    """Persist a new key to DB after a successful Kiro request, then sync usage."""
    if not is_db_configured():
        return
    from kiro.db.engine import async_session_factory
    from kiro.db.repositories import get_or_create_api_key, hash_api_key
    from kiro.usage.token_cache import token_cache
    try:
        async with async_session_factory() as session:
            api_key, is_new = await get_or_create_api_key(session, token)
        try:
            await token_cache.set(hash_api_key(token), api_key.id)
        except Exception:
            pass
        if is_new:
            logger.info(f"New API key (id={api_key.id}) persisted after successful Kiro request")
            asyncio.ensure_future(_sync_new_key(api_key.id, token))
    except Exception as e:
        logger.debug(f"Failed to persist new key: {e}")


async def _sync_new_key(key_id: int, raw_token: str) -> None:
    """Immediately fetch usage limits and kiro_user_id for a newly persisted key."""
    try:
        data = await get_usage_limits(raw_token, resource_type="CREDIT")

        breakdown_list = data.get("usageBreakdownList", [])
        if not breakdown_list:
            return

        credit_entry = None
        for entry in breakdown_list:
            if entry.get("resourceType") == "CREDIT":
                credit_entry = entry
                break
        if credit_entry is None:
            credit_entry = breakdown_list[0]

        usage_limit = int(credit_entry.get("usageLimit", 0))
        current_usage_precise = credit_entry.get("currentUsageWithPrecision")
        current_usage = int(current_usage_precise) if current_usage_precise is not None else int(credit_entry.get("currentUsage", 0))

        from datetime import datetime, timezone
        month = datetime.now(timezone.utc).strftime("%Y-%m")

        from kiro.db.engine import async_session_factory
        from kiro.db.repositories import upsert_kiro_user_mappings, upsert_usage_limits, update_api_key
        from kiro.usage.usage_cache import usage_cache

        async with async_session_factory() as session:
            await upsert_usage_limits(session, key_id, month, usage_limit, current_usage)

            user_info = data.get("userInfo", {})
            kiro_user_id = user_info.get("userId")
            if kiro_user_id:
                await upsert_kiro_user_mappings(session, [{"kiro_user_id": kiro_user_id}])
                await update_api_key(session, key_id, kiro_user_id=kiro_user_id)
                logger.info(f"Mapped key {key_id} to kiro_user_id={kiro_user_id}")

        raw_reset = credit_entry.get("nextDateReset") or data.get("nextDateReset")
        next_reset_at: float | None = float(raw_reset) if raw_reset is not None else None
        await usage_cache.refresh_limits({key_id: (usage_limit, current_usage, next_reset_at)})
        logger.info(f"Synced new key {key_id}: usage={current_usage}/{usage_limit} next_reset_at={next_reset_at}")

    except Exception as e:
        from fastapi import HTTPException as FastAPIHTTPException
        if isinstance(e, FastAPIHTTPException) and e.status_code == 401:
            asyncio.ensure_future(_deactivate_key(key_id))
        else:
            logger.warning(f"Failed to sync new key {key_id}: {e}")


async def _deactivate_key(key_id: int) -> None:
    """Mark a key as inactive in DB and cache after receiving 403 from Kiro."""
    try:
        from kiro.db.engine import async_session_factory
        from kiro.db.repositories import update_api_key
        from kiro.usage.usage_cache import usage_cache
        async with async_session_factory() as session:
            await update_api_key(session, key_id, is_active=False)
        usage_cache.set_key_active(key_id, False)
        logger.warning(f"Deactivated key {key_id} due to 403 from Kiro API")
    except Exception as e:
        logger.error(f"Failed to deactivate key {key_id}: {e}")


async def _try_fallback_pre_check(token: str, key_id: int | None) -> tuple[str, int | None] | None:
    if key_id is None or not is_db_configured():
        return None
    try:
        from kiro.usage.usage_cache import usage_cache
        entry = usage_cache.get(key_id)
        if entry and entry.is_system:
            return None  # system keys are managed by gateway resolution, not fallback
        from kiro.usage.fallback import fallback_router
        result = await fallback_router.pre_check(key_id)
        if result:
            new_key_id, new_raw_key = result
            return new_raw_key, new_key_id
    except Exception as e:
        logger.debug(f"Fallback pre-check failed: {e}")
    return None


async def _track_usage_background(key_id: int | None, input_tokens: int = 0, output_tokens: int = 0, model: str = "unknown") -> None:
    if key_id is None or not is_db_configured():
        return
    try:
        from kiro.usage.tracker import track_usage
        from kiro.usage.sync_worker import record_activity
        tracked_key_id = await track_usage(key_id, input_tokens, output_tokens, model=model)
        if tracked_key_id is not None:
            record_activity(tracked_key_id)
    except Exception as e:
        logger.debug(f"Usage tracking failed: {e}")


def _extract_response_model(chunk: str) -> str | None:
    """Extract model from message_start or first SSE chunk."""
    try:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                parsed = json.loads(line[6:])
                msg = parsed.get("message")
                if isinstance(msg, dict):
                    return msg.get("model")
                if "model" in parsed:
                    return parsed.get("model")
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def _accumulate_tokens_from_chunk(chunk: str, token_counts: dict) -> None:
    """Parse usage fields from SSE chunk and accumulate into token_counts dict."""
    try:
        for line in chunk.splitlines():
            if line.startswith("data: ") and line != "data: [DONE]":
                try:
                    parsed = json.loads(line[6:])
                    usage = parsed.get("usage")
                    if isinstance(usage, dict):
                        it = usage.get("input_tokens") or usage.get("prompt_tokens")
                        ot = usage.get("output_tokens") or usage.get("completion_tokens")
                        if it is not None and int(it) > 0:
                            token_counts["input"] = int(it)
                        if ot is not None and int(ot) > 0:
                            token_counts["output"] = int(ot)
                    msg = parsed.get("message")
                    if isinstance(msg, dict):
                        msg_usage = msg.get("usage")
                        if isinstance(msg_usage, dict):
                            it = msg_usage.get("input_tokens")
                            if it is not None and int(it) > 0:
                                token_counts["input"] = int(it)
                except (json.JSONDecodeError, ValueError, TypeError):
                    pass
    except Exception:
        pass


from dataclasses import dataclass

@dataclass
class _ResolvedToken:
    token: str
    key_id: int | None
    original_key_id: int | None
    gateway_key_id: int | None


async def _resolve_token_and_keys(token: str, error_detail_for_gw_fail: Any) -> _ResolvedToken:
    """Resolve gateway key, fallback, and key_id. Shared by both chat handlers."""
    gateway_key_id: int | None = None

    gw_result = await _resolve_gateway_key(token)
    if gw_result is not None:
        token, key_id, gateway_key_id = gw_result[0], gw_result[1], gw_result[2]
        original_key_id = key_id
    elif token.startswith("iziaigw_"):
        raise HTTPException(status_code=503, detail=error_detail_for_gw_fail)
    else:
        key_id = await _resolve_key_id(token)
        original_key_id = key_id

    fallback_result = await _try_fallback_pre_check(token, key_id)
    if fallback_result:
        token, key_id = fallback_result[0], fallback_result[1]

    return _ResolvedToken(token=token, key_id=key_id, original_key_id=original_key_id, gateway_key_id=gateway_key_id)


async def _read_error_response(response, http_client: "ApiKeyModeClient") -> str:
    """Read and enhance error text from a non-200 Kiro API response."""
    try:
        error_content = await response.aread()
    except Exception:
        error_content = b"Unknown error"
    await _release_response(response)
    await http_client.close()
    error_text = error_content.decode("utf-8", errors="replace")
    try:
        error_json = json.loads(error_text)
        from kiro.kiro_errors import enhance_kiro_error
        error_info = enhance_kiro_error(error_json)
        error_text = error_info.user_message
    except (json.JSONDecodeError, KeyError):
        pass
    return error_text


async def _release_response(response: "httpx.Response") -> None:
    """Close a response, returning its connection to the pool.

    Required whenever we abandon a streaming response without fully reading
    its body — e.g. before retrying or falling back. Without this, the
    connection is never returned to the shared pool and accumulates as
    CLOSE_WAIT sockets until the pool is exhausted (PoolTimeout).
    """
    try:
        await response.aclose()
    except Exception:
        pass


async def _track_all_usage(
    key_id: int | None, original_key_id: int | None, gateway_key_id: int | None,
    input_tokens: int = 0, output_tokens: int = 0, model: str = "unknown"
) -> None:
    await _track_usage_background(key_id, input_tokens, output_tokens, model=model)
    await _track_fallback_usage_background(original_key_id, key_id, input_tokens, output_tokens, model=model)
    if gateway_key_id is not None:
        await _track_gateway_key_usage(gateway_key_id, input_tokens, output_tokens, model=model, key_id=key_id)


async def _track_fallback_usage_background(
    original_key_id: int | None, actual_key_id: int | None,
    input_tokens: int = 0, output_tokens: int = 0, model: str = "unknown"
) -> None:
    if original_key_id is None or actual_key_id is None or not is_db_configured():
        return
    if original_key_id == actual_key_id:
        return
    try:
        from kiro.db.engine import async_session_factory
        from kiro.db.repositories import increment_fallback_usage
        month = time.strftime("%Y-%m")
        async with async_session_factory() as session:
            await increment_fallback_usage(session, original_key_id, actual_key_id, month, input_tokens, output_tokens, model=model)
    except Exception as e:
        logger.debug(f"Fallback usage tracking failed: {e}")


class ApiKeyModeClient:
    """
    Lightweight HTTP client for API_KEY_MODE.

    Sends requests to the Kiro API using a caller-supplied token.
    Handles 429 / 5xx retries with exponential back-off, but does NOT
    attempt token refresh on 403 (the caller owns the token).

    Supports both per-request clients and a shared application-level client.

    Args:
        token: Kiro API key supplied by the client.
        shared_client: Optional shared httpx.AsyncClient for connection pooling.
                       When provided it is used as-is and never closed by this class.
    """

    def __init__(self, token: str, shared_client: Optional[httpx.AsyncClient] = None, key_id: Optional[int] = None):
        self.token = token
        self.key_id = key_id
        self._shared_client = shared_client
        self._owns_client = shared_client is None
        self.client: Optional[httpx.AsyncClient] = shared_client

    async def _try_switch_key_on_429(self) -> bool:
        """Try to switch to a different key after a 429. Returns True if switched."""
        if self.key_id is None or not is_db_configured():
            return False
        try:
            from kiro.usage.usage_cache import usage_cache, sticky_binder
            entry = usage_cache.get(self.key_id)
            if entry is None:
                return False

            if entry.is_system:
                # Mark this system key exhausted and pick another from pool
                usage_cache.mark_quota_exhausted(self.key_id)
                sticky_binder.invalidate(self.key_id)
                available = usage_cache.get_available_keys(exclude_key_id=self.key_id, system_only=True)
                if not available:
                    return False
                new_key_id, new_raw_key = await sticky_binder.pick_and_decrypt(self.key_id, available)
                logger.info(f"ApiKeyModeClient: 429 on system key {self.key_id}, switched to {new_key_id}")
                self.token = new_raw_key
                self.key_id = new_key_id
                return True
            else:
                # User key: use fallback router
                from kiro.usage.fallback import fallback_router
                result = await fallback_router.post_check(self.key_id)
                if result is None:
                    return False
                new_key_id, new_raw_key = result
                logger.info(f"ApiKeyModeClient: 429 on user key {self.key_id}, switched to {new_key_id}")
                self.token = new_raw_key
                self.key_id = new_key_id
                return True
        except Exception as e:
            logger.debug(f"ApiKeyModeClient: key switch on 429 failed: {e}")
            return False

    async def _get_client(self, stream: bool = False) -> httpx.AsyncClient:
        if self._shared_client is not None:
            return self._shared_client
        
        # Check if we should use proxy for this key
        trust_env = True
        if self.key_id is not None:
            from kiro.usage.usage_cache import usage_cache
            entry = usage_cache.get(self.key_id)
            if entry and not entry.use_proxy:
                trust_env = False

        if self.client is None or self.client.is_closed:
            if stream:
                timeout = httpx.Timeout(
                    connect=30.0,
                    read=STREAMING_READ_TIMEOUT,
                    write=30.0,
                    pool=30.0,
                )
            else:
                timeout = httpx.Timeout(timeout=300.0)
            self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, trust_env=trust_env)
        return self.client

    async def close(self) -> None:
        """Close the client if this instance owns it."""
        if not self._owns_client:
            return
        if self.client and not self.client.is_closed:
            try:
                await self.client.aclose()
            except Exception as e:
                logger.warning(f"Error closing ApiKeyModeClient: {e}")

    async def request_with_retry(
        self,
        method: str,
        url: str,
        json_data: dict,
        stream: bool = False,
    ) -> httpx.Response:
        """
        Execute an HTTP request with retry logic.

        Retries on 429 and 5xx with exponential back-off.
        Returns 401 immediately on 403 (invalid user-supplied token).

        Args:
            method: HTTP method (e.g. "POST").
            url: Target URL.
            json_data: Request body as a dict (serialised to JSON).
            stream: Whether to use streaming mode.

        Returns:
            httpx.Response on success.

        Raises:
            HTTPException: On unrecoverable errors or after all retries exhausted.
        """
        max_retries = FIRST_TOKEN_MAX_RETRIES if stream else MAX_RETRIES
        client = await self._get_client(stream=stream)
        invocation_id = str(uuid.uuid4())

        for attempt in range(max_retries):
            try:
                headers = build_api_key_headers(self.token, stream=stream, attempt=attempt + 1, max_attempts=max_retries, invocation_id=invocation_id)

                if stream:
                    req = client.build_request(method, url, json=json_data, headers=headers)
                    logger.debug("ApiKeyModeClient: sending streaming request to Kiro API")
                    response = await client.send(req, stream=True)
                else:
                    logger.debug("ApiKeyModeClient: sending request to Kiro API")
                    response = await client.request(method, url, json=json_data, headers=headers)

                if response.status_code == 200:
                    return response

                if response.status_code == 403:
                    # User-supplied token is invalid — do not retry
                    await _release_response(response)
                    logger.warning("ApiKeyModeClient: received 403, user-supplied Kiro API key is invalid")
                    if self.key_id is not None and is_db_configured():
                        # Update cache immediately so subsequent requests don't reuse this key
                        try:
                            from kiro.usage.usage_cache import usage_cache
                            usage_cache.set_key_active(self.key_id, False)
                        except Exception:
                            pass
                        asyncio.ensure_future(_deactivate_key(self.key_id))
                    raise HTTPException(
                        status_code=401,
                        detail="Invalid Kiro API key (received 403 from Kiro API)"
                    )

                if response.status_code == 402:
                    logger.warning(f"ApiKeyModeClient: 402 monthly limit on key {self.key_id}, attempting key switch (attempt {attempt + 1}/{max_retries})")
                    if self.key_id is not None and is_db_configured():
                        try:
                            from kiro.usage.usage_cache import usage_cache
                            usage_cache.mark_quota_exhausted(self.key_id)
                        except Exception:
                            pass
                    switched = await self._try_switch_key_on_429()
                    if not switched:
                        return response  # no fallback available, propagate 402 (body still needed by caller)
                    await _release_response(response)
                    continue  # retry immediately with new key

                if response.status_code == 429:
                    await _release_response(response)
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"ApiKeyModeClient: 429 rate-limit, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                    await self._try_switch_key_on_429()
                    await asyncio.sleep(delay)
                    continue

                if 500 <= response.status_code < 600:
                    await _release_response(response)
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"ApiKeyModeClient: {response.status_code} server error, retrying in {delay}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(delay)
                    continue

                # Other status codes — return as-is for the caller to handle
                return response

            except HTTPException:
                raise  # propagate our own 401 immediately

            except httpx.TimeoutException as e:
                if attempt < max_retries - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"ApiKeyModeClient: timeout, retrying in {delay}s (attempt {attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"ApiKeyModeClient: timeout after {max_retries} attempts")
                    raise HTTPException(status_code=504, detail="Kiro API request timed out")

            except httpx.RequestError as e:
                if attempt < max_retries - 1:
                    delay = BASE_RETRY_DELAY * (2 ** attempt)
                    logger.warning(f"ApiKeyModeClient: connection error, retrying in {delay}s (attempt {attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(delay)
                else:
                    logger.error(f"ApiKeyModeClient: connection error after {max_retries} attempts: {e}")
                    raise HTTPException(status_code=502, detail=f"Connection error: {e}")

        raise HTTPException(
            status_code=502,
            detail=f"Kiro API request failed after {max_retries} attempts"
        )

    async def __aenter__(self) -> "ApiKeyModeClient":
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self.close()


# ==================================================================================================
# ApiKeyAuthAdapter — duck-types KiroAuthManager for streaming functions
# ==================================================================================================

class ApiKeyAuthAdapter:
    """
    Minimal duck-type of KiroAuthManager for use in API_KEY_MODE.

    The streaming and MCP helper functions accept a KiroAuthManager but only
    use a small subset of its interface.  This adapter satisfies that interface
    using the caller-supplied token and the default region from config.

    Attributes:
        api_host: Kiro generateAssistantResponse host (q.amazonaws.com)
        q_host: Kiro Q (ListAvailableModels) host
        profile_arn: Always None in API_KEY_MODE
        fingerprint: Machine fingerprint for User-Agent
    """

    def __init__(self, token: str, region: str = REGION):
        self._token = token
        self._region = region
        self._fingerprint = get_token_fingerprint(token)

        # Resolve hosts from config helpers
        self._api_host = get_kiro_api_host(region)
        self._q_host = get_kiro_q_host(region)

        self._auth_type = "api_key"

    async def get_access_token(self) -> str:
        """Return the caller-supplied token directly (no refresh)."""
        return self._token

    async def force_refresh(self) -> str:
        """No-op in API_KEY_MODE — token is owned by the caller."""
        return self._token

    @property
    def api_host(self) -> str:
        return self._api_host

    @property
    def q_host(self) -> str:
        return self._q_host

    @property
    def profile_arn(self) -> None:
        return None

    @property
    def auth_type(self):
        return self._auth_type

    @property
    def fingerprint(self) -> str:
        return self._fingerprint

    @property
    def region(self) -> str:
        return self._region


async def _stream_and_track(
    stream_source: Any,
    model_sentinel: str,
    error_chunk_fn: Any,
    request_model: str,
    key_id: int | None,
    original_key_id: int | None,
    gateway_key_id: int | None,
    http_client: "ApiKeyModeClient",
):
    """Iterate stream_source, accumulate tokens, fire usage tracking on close."""
    response_model: str | None = None
    token_counts = {"input": 0, "output": 0}
    try:
        async for chunk in stream_source:
            yield chunk
            if response_model is None and model_sentinel in chunk:
                response_model = _extract_response_model(chunk)
            if '"usage"' in chunk:
                _accumulate_tokens_from_chunk(chunk, token_counts)
    except GeneratorExit:
        pass
    except Exception as e:
        try:
            yield error_chunk_fn(e)
        except Exception:
            pass
        raise
    finally:
        billing_model = response_model or request_model
        try:
            await asyncio.shield(_track_all_usage(
                key_id, original_key_id, gateway_key_id,
                input_tokens=token_counts["input"],
                output_tokens=token_counts["output"],
                model=billing_model,
            ))
        finally:
            await http_client.close()


# ==================================================================================================
# Chat handlers
# ==================================================================================================

async def handle_chat_openai(request: Request, request_data: Any) -> Any:
    # Note: truncation recovery and web_search auto-injection (from routes_openai.py)
    # are not applied in API_KEY_MODE. This is a known limitation.
    """
    Handle /v1/chat/completions in API_KEY_MODE.

    Extracts the Kiro API key from the Authorization header, builds the Kiro
    payload, and proxies the request using ApiKeyModeClient.

    Args:
        request: FastAPI Request (used to read the Authorization header and
                 access app.state.http_client / app.state.model_cache).
        request_data: Validated ChatCompletionRequest Pydantic model.

    Returns:
        StreamingResponse (stream=True) or JSONResponse (stream=False).
    """
    from kiro.converters_openai import build_kiro_payload
    from kiro.streaming_openai import (
        stream_with_first_token_retry,
        collect_stream_response,
    )
    from kiro.utils import generate_conversation_id
    from kiro.cache import ModelInfoCache

    token = get_api_key_from_request(request)
    _is_gateway_key = token.startswith("iziaigw_")
    try:
        resolved = await _resolve_token_and_keys(
            token,
            error_detail_for_gw_fail={"error": {"message": "Gateway key is invalid, inactive, or no API keys are available in the pool. Please try again later.", "type": "service_unavailable", "code": 503}},
        )
    except HTTPException as e:
        if e.status_code == 503 and _is_gateway_key:
            from kiro.nine_router_client import forward_to_nine_router, is_nine_router_enabled
            if is_nine_router_enabled():
                logger.info("[API_KEY_MODE/OpenAI] Gateway key pool empty, falling back to 9router")
                gw_id = await _resolve_gateway_key_id_only(token)
                return await forward_to_nine_router(
                    request, await request.body(),
                    on_usage=_make_nine_router_usage_cb(gw_id),
                )
        raise
    token, key_id, original_key_id, gateway_key_id = resolved.token, resolved.key_id, resolved.original_key_id, resolved.gateway_key_id

    auth_adapter = ApiKeyAuthAdapter(token)
    model_cache: ModelInfoCache = request.app.state.model_cache

    conversation_id = generate_conversation_id()

    await apply_model_override(request_data)

    try:
        kiro_payload = build_kiro_payload(request_data, conversation_id, "")
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    url = f"{auth_adapter.api_host}/generateAssistantResponse"
    logger.debug(f"[API_KEY_MODE] Kiro API URL: {url}")

    if request_data.stream:
        http_client = ApiKeyModeClient(token, shared_client=None, key_id=key_id)
    else:
        http_client = ApiKeyModeClient(token, shared_client=request.app.state.http_client, key_id=key_id)

    try:
        response = await http_client.request_with_retry("POST", url, kiro_payload, stream=True)

        if response.status_code != 200:
            if response.status_code == 402:
                from kiro.nine_router_client import forward_to_nine_router, is_nine_router_enabled
                if is_nine_router_enabled():
                    await _release_response(response)
                    await http_client.close()
                    logger.warning("[API_KEY_MODE/OpenAI] All keys quota-exhausted, falling back to 9router")
                    return await forward_to_nine_router(
                        request, await request.body(),
                        on_usage=_make_nine_router_usage_cb(gateway_key_id),
                    )
            error_text = await _read_error_response(response, http_client)
            return JSONResponse(
                status_code=response.status_code,
                content={"error": {"message": error_text, "type": "kiro_api_error", "code": response.status_code}},
            )

        if key_id is None and gateway_key_id is None:
            asyncio.ensure_future(_persist_new_key(token))

        messages_for_tokenizer = [msg.model_dump() for msg in request_data.messages]
        tools_for_tokenizer = [t.model_dump() for t in request_data.tools] if request_data.tools else None

        if request_data.stream:
            async def make_retry_request():
                return await http_client.request_with_retry("POST", url, kiro_payload, stream=True)

            source = stream_with_first_token_retry(
                make_request=make_retry_request,
                client=http_client.client,
                model=request_data.model,
                model_cache=model_cache,
                auth_manager=auth_adapter,
                initial_response=response,
                request_messages=messages_for_tokenizer,
                request_tools=tools_for_tokenizer,
            )
            return StreamingResponse(
                _stream_and_track(
                    source, '"model"', lambda e: "data: [DONE]\n\n",
                    request_data.model, key_id, original_key_id, gateway_key_id, http_client,
                ),
                media_type="text/event-stream",
            )

        else:
            openai_response = await collect_stream_response(
                http_client.client,
                response,
                request_data.model,
                model_cache,
                auth_adapter,
                request_messages=messages_for_tokenizer,
                request_tools=tools_for_tokenizer,
            )
            input_tokens = 0
            output_tokens = 0
            resp_model = request_data.model
            if isinstance(openai_response, dict):
                usage = openai_response.get("usage", {})
                input_tokens = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
                output_tokens = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
                resp_model = openai_response.get("model") or request_data.model
            await _track_all_usage(key_id, original_key_id, gateway_key_id, input_tokens=input_tokens, output_tokens=output_tokens, model=resp_model)
            await http_client.close()
            return JSONResponse(content=openai_response)

    except HTTPException:
        await http_client.close()
        raise
    except Exception as e:
        await http_client.close()
        logger.error(f"[API_KEY_MODE] Internal error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")


async def handle_chat_anthropic(request: Request, request_data: Any, anthropic_version: Optional[str] = None) -> Any:
    # Note: truncation recovery and web_search auto-injection (from routes_anthropic.py)
    # are not applied in API_KEY_MODE. This is a known limitation.
    """
    Handle /v1/messages in API_KEY_MODE.

    Extracts the Kiro API key from the Authorization or x-api-key header,
    builds the Kiro payload, and proxies the request using ApiKeyModeClient.

    Args:
        request: FastAPI Request.
        request_data: Validated AnthropicMessagesRequest Pydantic model.
        anthropic_version: Value of the anthropic-version header (optional).

    Returns:
        StreamingResponse (stream=True) or JSONResponse (stream=False).
    """
    from kiro.converters_anthropic import anthropic_to_kiro
    from kiro.streaming_anthropic import (
        stream_with_first_token_retry_anthropic,
        collect_anthropic_response,
    )
    from kiro.utils import generate_conversation_id
    from kiro.cache import ModelInfoCache

    # Support both Authorization: Bearer and x-api-key headers
    raw_token = (
        extract_bearer_token(request.headers.get("Authorization"))
        or request.headers.get("x-api-key")
    )
    if not raw_token:
        raise HTTPException(
            status_code=401,
            detail={
                "type": "error",
                "error": {
                    "type": "authentication_error",
                    "message": "API_KEY_MODE is enabled: supply your Kiro API key via Authorization: Bearer or x-api-key",
                },
            },
        )

    _is_gateway_key = raw_token.startswith("iziaigw_")
    try:
        resolved = await _resolve_token_and_keys(
            raw_token,
            error_detail_for_gw_fail={
                "type": "error",
                "error": {
                    "type": "service_unavailable",
                    "message": "Gateway key is invalid, inactive, or no API keys are available in the pool. Please try again later.",
                },
            },
        )
    except HTTPException as e:
        if e.status_code == 503 and _is_gateway_key:
            from kiro.nine_router_client import forward_to_nine_router, is_nine_router_enabled
            if is_nine_router_enabled():
                logger.info("[API_KEY_MODE/Anthropic] Gateway key pool empty, falling back to 9router")
                gw_id = await _resolve_gateway_key_id_only(raw_token)
                return await forward_to_nine_router(
                    request, await request.body(),
                    on_usage=_make_nine_router_usage_cb(gw_id),
                )
        raise
    raw_token, key_id, original_key_id, gateway_key_id = resolved.token, resolved.key_id, resolved.original_key_id, resolved.gateway_key_id

    auth_adapter = ApiKeyAuthAdapter(raw_token)
    model_cache: ModelInfoCache = request.app.state.model_cache

    conversation_id = generate_conversation_id()

    await apply_model_override(request_data)

    try:
        kiro_payload = anthropic_to_kiro(request_data, conversation_id, "")
    except ValueError as e:
        logger.error(f"[API_KEY_MODE] Conversion error: {e}")
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request_error", "message": str(e)}},
        )

    url = f"{auth_adapter.api_host}/generateAssistantResponse"
    logger.debug(f"[API_KEY_MODE] Kiro API URL: {url}")

    if request_data.stream:
        http_client = ApiKeyModeClient(raw_token, shared_client=None, key_id=key_id)
    else:
        http_client = ApiKeyModeClient(raw_token, shared_client=request.app.state.http_client, key_id=key_id)

    try:
        response = await http_client.request_with_retry("POST", url, kiro_payload, stream=True)

        if response.status_code != 200:
            if response.status_code == 402:
                from kiro.nine_router_client import forward_to_nine_router, is_nine_router_enabled
                if is_nine_router_enabled():
                    await _release_response(response)
                    await http_client.close()
                    logger.warning("[API_KEY_MODE/Anthropic] All keys quota-exhausted, falling back to 9router")
                    return await forward_to_nine_router(
                        request, await request.body(),
                        on_usage=_make_nine_router_usage_cb(gateway_key_id),
                    )
            error_text = await _read_error_response(response, http_client)
            return JSONResponse(
                status_code=response.status_code,
                content={"type": "error", "error": {"type": "api_error", "message": error_text}},
            )

        if key_id is None and gateway_key_id is None:
            asyncio.ensure_future(_persist_new_key(raw_token))

        messages_for_tokenizer = [msg.model_dump() for msg in request_data.messages]
        tools_for_tokenizer = [t.model_dump() for t in request_data.tools] if request_data.tools else None
        if isinstance(request_data.system, list):
            system_for_tokenizer = [b.model_dump() if hasattr(b, "model_dump") else b for b in request_data.system]
        else:
            system_for_tokenizer = request_data.system

        if request_data.stream:
            async def make_retry_request():
                return await http_client.request_with_retry("POST", url, kiro_payload, stream=True)

            source = stream_with_first_token_retry_anthropic(
                make_request=make_retry_request,
                model=request_data.model,
                model_cache=model_cache,
                auth_manager=auth_adapter,
                initial_response=response,
                request_messages=messages_for_tokenizer,
                request_tools=tools_for_tokenizer,
                request_system=system_for_tokenizer,
            )

            def _anthropic_error_chunk(e: Exception) -> str:
                return f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'api_error', 'message': str(e)}})}\n\n"

            return StreamingResponse(
                _stream_and_track(
                    source, '"message_start"', _anthropic_error_chunk,
                    request_data.model, key_id, original_key_id, gateway_key_id, http_client,
                ),
                media_type="text/event-stream",
            )

        else:
            anthropic_response = await collect_anthropic_response(
                response,
                request_data.model,
                model_cache,
                auth_adapter,
                request_messages=messages_for_tokenizer,
                request_tools=tools_for_tokenizer,
                request_system=system_for_tokenizer,
            )
            input_tokens = 0
            output_tokens = 0
            resp_model = request_data.model
            if isinstance(anthropic_response, dict):
                usage = anthropic_response.get("usage", {})
                input_tokens = usage.get("input_tokens", 0)
                output_tokens = usage.get("output_tokens", 0)
                resp_model = anthropic_response.get("model") or request_data.model
            await _track_all_usage(key_id, original_key_id, gateway_key_id, input_tokens=input_tokens, output_tokens=output_tokens, model=resp_model)
            await http_client.close()
            return JSONResponse(content=anthropic_response)

    except HTTPException:
        await http_client.close()
        raise
    except Exception as e:
        await http_client.close()
        logger.error(f"[API_KEY_MODE] Internal error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal Server Error: {str(e)}")
