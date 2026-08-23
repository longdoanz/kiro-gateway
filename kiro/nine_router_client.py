# -*- coding: utf-8 -*-
"""
9Router fallback client.

Forwards OpenAI-compatible requests to a 9router instance when all Kiro
accounts are exhausted (402 quota / all accounts unavailable).

Usage:
    from kiro.nine_router_client import forward_to_nine_router, is_nine_router_enabled

    if is_nine_router_enabled():
        return await forward_to_nine_router(request, body_bytes)
"""

import asyncio
import json
from typing import AsyncIterator, Awaitable, Callable, Optional

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from loguru import logger

from kiro.config import ENABLE_NINE_ROUTER_DIRECT, NINE_ROUTER_API_KEY, NINE_ROUTER_URL

# ---------------------------------------------------------------------------
# 9router model override — own config (toggle, rules, default), cached in-process
# ---------------------------------------------------------------------------
_nine_router_override_cache: tuple[bool, list[dict], str] | None = None  # (enabled, rules, default_model)


def invalidate_nine_router_override_cache() -> None:
    global _nine_router_override_cache
    _nine_router_override_cache = None


async def _get_nine_router_override() -> tuple[bool, list[dict], str]:
    """Return (enabled, rules, default_model) from DB config (cached)."""
    global _nine_router_override_cache
    if _nine_router_override_cache is not None:
        return _nine_router_override_cache
    try:
        from kiro.db.engine import async_session_factory
        from kiro.db.repositories import get_config
        async with async_session_factory() as session:
            enabled_raw = await get_config(session, "enable_nine_router_model_override") or "false"
            rules_raw = await get_config(session, "nine_router_model_override_rules") or "[]"
            default_raw = await get_config(session, "nine_router_model_override_default") or "auto"
        import json as _json
        rules = _json.loads(rules_raw) if isinstance(rules_raw, str) else rules_raw
        rules = rules if isinstance(rules, list) else []
        result = (enabled_raw.lower() == "true", rules, default_raw)
    except Exception:
        result = (False, [], "auto")
    _nine_router_override_cache = result
    return result


def _rewrite_model_in_body(body: bytes, override_model: str) -> bytes:
    """Replace the 'model' field in a JSON request body. Returns original body on any error."""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "model" in parsed:
            parsed["model"] = override_model
            return json.dumps(parsed, separators=(",", ":")).encode()
    except Exception:
        pass
    return body


# ---------------------------------------------------------------------------
# 9router direct mode — global toggle routing every request straight to 9router
# ---------------------------------------------------------------------------
_nine_router_direct_cache: bool | None = None


def invalidate_nine_router_direct_cache() -> None:
    global _nine_router_direct_cache
    _nine_router_direct_cache = None


async def is_nine_router_direct_enabled() -> bool:
    """Return True when direct-to-9router mode is enabled (DB key, env fallback)."""
    global _nine_router_direct_cache
    if _nine_router_direct_cache is not None:
        return _nine_router_direct_cache
    result = False
    try:
        from kiro.db.engine import async_session_factory
        from kiro.db.repositories import get_config
        async with async_session_factory() as session:
            enabled_raw = await get_config(session, "enable_nine_router_direct") or ""
        result = enabled_raw.lower() == "true"
    except Exception:
        # DB unavailable — fall back to the environment flag.
        result = ENABLE_NINE_ROUTER_DIRECT
    _nine_router_direct_cache = result
    return result

# Headers that must not be forwarded upstream
_HOP_BY_HOP = frozenset({
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "upgrade",
    "authorization",  # replaced with 9router's own key
    # httpx auto-decompresses response bodies; forwarding content-encoding
    # would make the client decode an already-decoded body.
    "content-encoding",
})


def is_nine_router_enabled() -> bool:
    """Return True when NINE_ROUTER_URL is configured."""
    return bool(NINE_ROUTER_URL)


# Callback signature: (input_tokens, output_tokens, model) -> awaitable
OnUsage = Callable[[int, int, str], Awaitable[None]]


def _accumulate_usage_from_chunk(chunk: str, token_counts: dict, model_box: list) -> None:
    """
    Parse usage/model fields from an SSE chunk (or whole JSON body) and update
    token_counts / model_box in place.

    Mirrors api_key_mode._accumulate_tokens_from_chunk: tolerant of partial /
    malformed JSON, handles both OpenAI (prompt_tokens/completion_tokens) and
    Anthropic (input_tokens/output_tokens, nested under "message").
    """
    import json

    def _scan(parsed: dict) -> None:
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
            if model_box[0] is None and msg.get("model"):
                model_box[0] = msg.get("model")
            msg_usage = msg.get("usage")
            if isinstance(msg_usage, dict):
                it = msg_usage.get("input_tokens")
                ot = msg_usage.get("output_tokens")
                if it is not None and int(it) > 0:
                    token_counts["input"] = int(it)
                if ot is not None and int(ot) > 0:
                    token_counts["output"] = int(ot)
        if model_box[0] is None and parsed.get("model"):
            model_box[0] = parsed.get("model")

    try:
        # SSE: one or more "data: {...}" lines. Non-streaming: a single JSON body.
        if "data: " in chunk:
            for line in chunk.splitlines():
                line = line.strip()
                if line.startswith("data: ") and line != "data: [DONE]":
                    try:
                        _scan(json.loads(line[6:]))
                    except (json.JSONDecodeError, ValueError, TypeError):
                        pass
        else:
            try:
                _scan(json.loads(chunk))
            except (json.JSONDecodeError, ValueError, TypeError):
                pass
    except Exception:
        pass


def _build_headers(original_request: Request) -> dict[str, str]:
    """Build forwarding headers, replacing auth with 9router API key."""
    headers = {
        k: v
        for k, v in original_request.headers.items()
        if k.lower() not in _HOP_BY_HOP
    }
    if NINE_ROUTER_API_KEY:
        headers["Authorization"] = f"Bearer {NINE_ROUTER_API_KEY}"
    # Avoid compressed upstream bodies — httpx decompresses them and the
    # content-encoding header is stripped from the proxied response.
    headers["accept-encoding"] = "identity"
    return headers


async def forward_to_nine_router(
    original_request: Request,
    body: bytes,
    path: Optional[str] = None,
    on_usage: Optional[OnUsage] = None,
) -> StreamingResponse | JSONResponse:
    """
    Forward an OpenAI-compatible request to 9router and stream the response back.

    Args:
        original_request: The incoming FastAPI request (for headers and method).
        body: Raw request body bytes.
        path: Override path (default: same as original request path).
        on_usage: Optional async callback invoked once after the response is fully
                  consumed, with (input_tokens, output_tokens, model).  This is
                  *best-effort* — the proxy layer does not delay the client; if the
                  stream is exhausted or an error occurs the callback is still fired
                  with whatever token counts were accumulated (possibly zeroes).

    Returns:
        StreamingResponse for streaming requests, JSONResponse for non-streaming.
    """
    if not NINE_ROUTER_URL:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": "All Kiro accounts exhausted and 9router fallback is not configured.", "type": "service_unavailable"}},
        )

    target_path = path or original_request.url.path
    target_url = f"{NINE_ROUTER_URL.rstrip('/')}{target_path}"
    if original_request.url.query:
        target_url += f"?{original_request.url.query}"

    # Apply 9router's own model override (independent of Global Model Enforcement).
    # Returns an ordered list of candidate models — a multi-level override yields
    # several targets tried in order, with the default (when configured) as the
    # final level. `candidates` is a list of Optional[str]: None means "send the
    # original body untouched" (no model key to rewrite).
    enabled, rules, default_model = await _get_nine_router_override()
    original_model: Optional[str] = None
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict):
            original_model = parsed.get("model")
    except Exception:
        pass

    if original_model and enabled:
        from kiro.model_override import OverrideConfig, resolve_models
        # 9router treats "auto" as a real model name, so a configured default
        # (even "auto") must be enforced via has_default.
        config = OverrideConfig(enabled=True, rules=rules, default_model=default_model, has_default=True)
        candidates: list[Optional[str]] = resolve_models(original_model, config)
        if candidates != [original_model]:
            logger.info(f"9router model override: {original_model!r} → {candidates}")
    else:
        candidates = [original_model]

    headers = _build_headers(original_request)
    logger.info(f"9router fallback: forwarding {original_request.method} {target_path}")

    # Reuse the application-wide pooled client (connection pooling + keep-alive)
    # so a burst of fallbacks does not open one TCP connection — and one file
    # descriptor — per request. Only fall back to a private client when the
    # shared one is unavailable (e.g. called outside the app lifespan / tests),
    # in which case we own it and must close it.
    shared_client = getattr(getattr(original_request, "app", None), "state", None)
    shared_client = getattr(shared_client, "http_client", None)
    if shared_client is not None:
        client = shared_client
        owns_client = False
    else:
        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0))
        owns_client = True

    async def _maybe_close_client() -> None:
        # Never close the shared pooled client — only a private one we created.
        if owns_client:
            await client.aclose()

    # Try each candidate in order; on a pre-stream failure advance to the next.
    # Once the upstream accepts a candidate (2xx), stream it back and stop —
    # partial bytes must not be retried.
    last_error: JSONResponse | None = None
    for idx, candidate in enumerate(candidates):
        request_body = _rewrite_model_in_body(body, candidate) if candidate is not None else body
        try:
            response = await client.send(
                client.build_request(
                    method=original_request.method,
                    url=target_url,
                    headers=headers,
                    content=request_body,
                ),
                stream=True,
            )
        except httpx.ConnectError as exc:
            logger.error(f"9router fallback: connection failed to {NINE_ROUTER_URL}: {exc}")
            last_error = JSONResponse(
                status_code=503,
                content={"error": {"message": f"9router fallback unavailable: {exc}", "type": "service_unavailable"}},
            )
        except httpx.TimeoutException as exc:
            logger.error(f"9router fallback: timeout: {exc}")
            last_error = JSONResponse(
                status_code=504,
                content={"error": {"message": "9router fallback timed out.", "type": "timeout"}},
            )
        except Exception as exc:
            logger.error(f"9router fallback: unexpected error: {exc}")
            last_error = JSONResponse(
                status_code=502,
                content={"error": {"message": f"9router fallback error: {exc}", "type": "bad_gateway"}},
            )

        if last_error is not None:
            has_more = idx < len(candidates) - 1
            if has_more:
                logger.info(f"9router override failover: {candidate!r} failed, trying next candidate")
                last_error = None  # reset for the next iteration
                continue
            await _maybe_close_client()
            return last_error

        upstream_headers = {
            k: v for k, v in response.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }

        if response.status_code != 200:
            error_body = await response.aread()
            await response.aclose()
            logger.warning(f"9router fallback returned {response.status_code}: {error_body[:200]}")
            if idx < len(candidates) - 1:
                logger.info(f"9router override failover: {candidate!r} failed ({response.status_code}), trying next candidate")
                continue
            await _maybe_close_client()
            return JSONResponse(
                status_code=response.status_code,
                content={"error": {"message": error_body.decode("utf-8", errors="replace"), "type": "nine_router_error"}},
            )

        async def _stream_and_close() -> AsyncIterator[bytes]:
            token_counts = {"input": 0, "output": 0}
            model_box: list = [None]
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
                    if on_usage is not None and chunk:
                        # Cheap byte-level gate before the UTF-8 decode on the hot path.
                        # OpenAI repeats "model" in every SSE delta, so once we have the
                        # model we only look for "usage" (final chunk) to avoid decoding
                        # every chunk.
                        want_model = model_box[0] is None
                        has_usage = b'"usage"' in chunk
                        if has_usage or (want_model and b'"model"' in chunk):
                            text = chunk.decode("utf-8", errors="ignore")
                            if text:
                                _accumulate_usage_from_chunk(text, token_counts, model_box)
            finally:
                # aclose() returns the connection to the shared pool; the client itself
                # is only closed when we privately own it.
                await response.aclose()
                await _maybe_close_client()
                if on_usage is not None:
                    try:
                        await asyncio.shield(
                            on_usage(token_counts["input"], token_counts["output"], model_box[0] or "unknown")
                        )
                    except Exception as exc:  # never let tracking break the stream
                        logger.debug(f"9router usage callback failed: {exc}")

        return StreamingResponse(
            _stream_and_close(),
            status_code=response.status_code,
            headers=upstream_headers,
            media_type=response.headers.get("content-type", "text/event-stream"),
        )

    # All candidates exhausted (unreachable in practice — the loop returns within
    # each branch) — safety net returning the last observed error.
    await _maybe_close_client()
    return last_error or JSONResponse(
        status_code=502,
        content={"error": {"message": "9router fallback: all candidates failed.", "type": "bad_gateway"}},
    )
