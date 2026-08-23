# -*- coding: utf-8 -*-
"""
Unit tests for kiro/nine_router_client.py.

Tests cover:
- is_nine_router_enabled()
- forward_to_nine_router() — fallback triggered, not triggered, streaming passthrough
- Error handling: connect error, timeout, non-200 upstream, disabled
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.datastructures import Headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_request(
    method: str = "POST",
    path: str = "/v1/chat/completions",
    query: str = "",
    headers: dict | None = None,
    shared_http_client=None,
) -> MagicMock:
    req = MagicMock(spec=Request)
    req.method = method
    req.url.path = path
    req.url.query = query
    req.headers = Headers(headers or {"content-type": "application/json"})
    req.body = AsyncMock(return_value=b'{"model":"gpt-4","messages":[]}')
    # Simulate app.state.http_client — None triggers private-client fallback
    req.app.state.http_client = shared_http_client
    return req


def _mock_stream_response(status_code: int = 200, chunks: list[bytes] | None = None, headers: dict | None = None):
    chunks = chunks or [b"data: chunk1\n\n", b"data: [DONE]\n\n"]
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {"content-type": "text/event-stream"}

    async def _aiter():
        for c in chunks:
            yield c

    resp.aiter_bytes = _aiter
    resp.aread = AsyncMock(return_value=b"error body")
    resp.aclose = AsyncMock()
    return resp


def _mock_client(response=None, send_side_effect=None):
    client = MagicMock()
    client.build_request = MagicMock(side_effect=lambda **kwargs: kwargs)
    client.send = AsyncMock(side_effect=send_side_effect) if send_side_effect else AsyncMock(return_value=response)
    client.aclose = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# is_nine_router_enabled
# ---------------------------------------------------------------------------

class TestIsNineRouterEnabled:
    def test_enabled_when_url_set(self):
        import kiro.nine_router_client as mod
        with patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"):
            assert mod.is_nine_router_enabled() is True

    def test_disabled_when_url_empty(self):
        import kiro.nine_router_client as mod
        with patch.object(mod, "NINE_ROUTER_URL", ""):
            assert mod.is_nine_router_enabled() is False


# ---------------------------------------------------------------------------
# forward_to_nine_router
# ---------------------------------------------------------------------------

class TestForwardToNineRouter:
    @pytest.mark.asyncio
    async def test_returns_503_when_not_configured(self):
        import kiro.nine_router_client as mod
        with patch.object(mod, "NINE_ROUTER_URL", ""):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, b"{}")
            assert isinstance(resp, JSONResponse)
            assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_streams_response_on_success(self):
        import kiro.nine_router_client as mod
        resp_mock = _mock_stream_response(200)
        client = _mock_client(response=resp_mock)

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, b"{}")
            assert isinstance(resp, StreamingResponse)
            assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_adds_api_key_header_when_configured(self):
        import kiro.nine_router_client as mod
        resp_mock = _mock_stream_response(200)
        client = _mock_client(response=resp_mock)

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch.object(mod, "NINE_ROUTER_API_KEY", "secret-key"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            await mod.forward_to_nine_router(req, b"{}")
            sent_headers = client.build_request.call_args.kwargs["headers"]
            assert sent_headers.get("Authorization") == "Bearer secret-key"

    @pytest.mark.asyncio
    async def test_strips_original_authorization_header(self):
        import kiro.nine_router_client as mod
        resp_mock = _mock_stream_response(200)
        client = _mock_client(response=resp_mock)

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch.object(mod, "NINE_ROUTER_API_KEY", ""),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request(headers={"authorization": "Bearer gateway-user-token"})
            await mod.forward_to_nine_router(req, b"{}")
            sent_headers = client.build_request.call_args.kwargs["headers"]
            assert "Authorization" not in sent_headers
            assert "authorization" not in sent_headers

    @pytest.mark.asyncio
    async def test_non_200_upstream_returns_json_error(self):
        import kiro.nine_router_client as mod
        resp_mock = _mock_stream_response(429)
        client = _mock_client(response=resp_mock)

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, b"{}")
            assert isinstance(resp, JSONResponse)
            assert resp.status_code == 429

    @pytest.mark.asyncio
    async def test_connect_error_returns_503(self):
        import kiro.nine_router_client as mod
        import httpx

        client = _mock_client(send_side_effect=httpx.ConnectError("refused"))

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, b"{}")
            assert isinstance(resp, JSONResponse)
            assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_timeout_returns_504(self):
        import kiro.nine_router_client as mod
        import httpx

        client = _mock_client(send_side_effect=httpx.TimeoutException("timed out"))

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, b"{}")
            assert isinstance(resp, JSONResponse)
            assert resp.status_code == 504

    @pytest.mark.asyncio
    async def test_correct_target_url_built(self):
        import kiro.nine_router_client as mod
        resp_mock = _mock_stream_response(200)
        client = _mock_client(response=resp_mock)

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request(path="/v1/chat/completions")
            await mod.forward_to_nine_router(req, b"{}")
            built_url = client.build_request.call_args.kwargs["url"]
            assert built_url == "http://ninerouter:20128/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_query_string_forwarded(self):
        import kiro.nine_router_client as mod
        resp_mock = _mock_stream_response(200)
        client = _mock_client(response=resp_mock)

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request(path="/v1/models", query="foo=bar")
            await mod.forward_to_nine_router(req, b"")
            built_url = client.build_request.call_args.kwargs["url"]
            assert "foo=bar" in built_url

    @pytest.mark.asyncio
    async def test_shared_client_reused_and_not_closed(self):
        """When app.state.http_client exists, it's used directly and never closed."""
        import kiro.nine_router_client as mod
        resp_mock = _mock_stream_response(200)
        shared = _mock_client(response=resp_mock)

        with patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"):
            req = _mock_request(shared_http_client=shared)
            resp = await mod.forward_to_nine_router(req, b"{}")
            assert isinstance(resp, StreamingResponse)
            # Drain stream to trigger finally block
            async for _ in resp.body_iterator:
                pass
            # Connection returned to pool (response closed), but client NOT closed
            resp_mock.aclose.assert_awaited()
            shared.aclose.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_private_client_closed_after_stream(self):
        """When no shared client, a private client is created and closed after stream."""
        import kiro.nine_router_client as mod
        resp_mock = _mock_stream_response(200)
        private = _mock_client(response=resp_mock)

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=private),
        ):
            req = _mock_request()  # shared_http_client=None (default)
            resp = await mod.forward_to_nine_router(req, b"{}")
            async for _ in resp.body_iterator:
                pass
            # Both response and private client are closed
            resp_mock.aclose.assert_awaited()
            private.aclose.assert_awaited()


# ---------------------------------------------------------------------------
# on_usage callback (usage recording for the fallback path)
# ---------------------------------------------------------------------------

async def _drain(streaming_response) -> list[bytes]:
    """Iterate a StreamingResponse body to completion, returning the chunks."""
    chunks = []
    async for chunk in streaming_response.body_iterator:
        chunks.append(chunk)
    return chunks


class TestOnUsageCallback:
    @pytest.mark.asyncio
    async def test_callback_fired_with_openai_usage(self):
        import kiro.nine_router_client as mod
        chunks = [
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n',
            b'data: {"model":"gpt-4o","usage":{"prompt_tokens":12,"completion_tokens":7}}\n\n',
            b"data: [DONE]\n\n",
        ]
        resp_mock = _mock_stream_response(200, chunks=chunks)
        client = _mock_client(response=resp_mock)
        seen = {}

        async def on_usage(it, ot, model):
            seen["input"], seen["output"], seen["model"] = it, ot, model

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, b"{}", on_usage=on_usage)
            body = await _drain(resp)

        assert body == chunks  # stream passed through unchanged
        assert seen == {"input": 12, "output": 7, "model": "gpt-4o"}

    @pytest.mark.asyncio
    async def test_callback_fired_with_anthropic_usage(self):
        import kiro.nine_router_client as mod
        chunks = [
            b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-opus-4-8","usage":{"input_tokens":30}}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":15}}\n\n',
        ]
        resp_mock = _mock_stream_response(200, chunks=chunks)
        client = _mock_client(response=resp_mock)
        seen = {}

        async def on_usage(it, ot, model):
            seen["input"], seen["output"], seen["model"] = it, ot, model

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request(path="/v1/messages")
            resp = await mod.forward_to_nine_router(req, b"{}", on_usage=on_usage)
            await _drain(resp)

        assert seen == {"input": 30, "output": 15, "model": "claude-opus-4-8"}

    @pytest.mark.asyncio
    async def test_callback_fired_with_zeroes_when_no_usage(self):
        import kiro.nine_router_client as mod
        chunks = [b"data: chunk1\n\n", b"data: [DONE]\n\n"]
        resp_mock = _mock_stream_response(200, chunks=chunks)
        client = _mock_client(response=resp_mock)
        seen = {}

        async def on_usage(it, ot, model):
            seen["input"], seen["output"], seen["model"] = it, ot, model

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, b"{}", on_usage=on_usage)
            await _drain(resp)

        assert seen == {"input": 0, "output": 0, "model": "unknown"}

    @pytest.mark.asyncio
    async def test_callback_exception_does_not_break_stream(self):
        import kiro.nine_router_client as mod
        chunks = [
            b'data: {"model":"gpt-4o","usage":{"prompt_tokens":1,"completion_tokens":1}}\n\n',
            b"data: [DONE]\n\n",
        ]
        resp_mock = _mock_stream_response(200, chunks=chunks)
        client = _mock_client(response=resp_mock)

        async def on_usage(it, ot, model):
            raise RuntimeError("tracking blew up")

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, b"{}", on_usage=on_usage)
            body = await _drain(resp)  # must not raise

        assert body == chunks
        resp_mock.aclose.assert_awaited()
        client.aclose.assert_awaited()


# ---------------------------------------------------------------------------
# Multi-level override failover
# ---------------------------------------------------------------------------

class TestMultiLevelFailover:
    @pytest.mark.asyncio
    async def test_fails_over_to_next_candidate_on_non_200(self):
        import kiro.nine_router_client as mod

        # First candidate returns 503, second returns 200 streaming.
        resp_fail = _mock_stream_response(503)
        resp_ok = _mock_stream_response(200, chunks=[b'data: {"model":"good-model"}\n\n', b"data: [DONE]\n\n"])
        client = _mock_client(send_side_effect=[resp_fail, resp_ok])

        body = b'{"model":"gpt-4","messages":[]}'
        rules = [{"from": "gpt", "to": ["bad-model", "good-model"]}]

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch.object(mod, "_get_nine_router_override", AsyncMock(return_value=(True, rules, "auto"))),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, body)
            assert isinstance(resp, StreamingResponse)
            assert resp.status_code == 200

        # Two send attempts: first rewritten to "bad-model", second to "good-model".
        assert client.send.await_count == 2
        sent_contents = [c.kwargs["content"] for c in client.build_request.call_args_list]
        assert b'"model":"bad-model"' in sent_contents[0]
        assert b'"model":"good-model"' in sent_contents[1]

    @pytest.mark.asyncio
    async def test_all_candidates_failed_returns_last_error(self):
        import kiro.nine_router_client as mod

        resp_fail1 = _mock_stream_response(502)
        resp_fail2 = _mock_stream_response(503)
        client = _mock_client(send_side_effect=[resp_fail1, resp_fail2])

        body = b'{"model":"gpt-4","messages":[]}'
        rules = [{"from": "gpt", "to": ["bad-model", "worse-model"]}]

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch.object(mod, "_get_nine_router_override", AsyncMock(return_value=(True, rules, "auto"))),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, body)
            assert isinstance(resp, JSONResponse)
            assert resp.status_code == 503  # last candidate's status

        assert client.send.await_count == 2

    @pytest.mark.asyncio
    async def test_string_rule_still_single_attempt(self):
        import kiro.nine_router_client as mod

        resp_ok = _mock_stream_response(200)
        client = _mock_client(response=resp_ok)
        body = b'{"model":"gpt-4","messages":[]}'
        rules = [{"from": "gpt", "to": "good-model"}]

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch.object(mod, "_get_nine_router_override", AsyncMock(return_value=(True, rules, "auto"))),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, body)
            assert isinstance(resp, StreamingResponse)

        assert client.send.await_count == 1
        assert b'"model":"good-model"' in client.build_request.call_args_list[0].kwargs["content"]

    @pytest.mark.asyncio
    async def test_usage_callback_fires_once_for_winning_candidate(self):
        import kiro.nine_router_client as mod

        resp_fail = _mock_stream_response(500)
        resp_ok = _mock_stream_response(
            200,
            chunks=[b'data: {"model":"good-model","usage":{"prompt_tokens":5,"completion_tokens":3}}\n\n', b"data: [DONE]\n\n"],
        )
        client = _mock_client(send_side_effect=[resp_fail, resp_ok])
        body = b'{"model":"gpt-4","messages":[]}'
        rules = [{"from": "gpt", "to": ["bad-model", "good-model"]}]
        seen = {}

        async def on_usage(it, ot, model):
            seen["input"], seen["output"], seen["model"] = it, ot, model

        with (
            patch.object(mod, "NINE_ROUTER_URL", "http://ninerouter:20128"),
            patch.object(mod, "_get_nine_router_override", AsyncMock(return_value=(True, rules, "auto"))),
            patch("kiro.nine_router_client.httpx.AsyncClient", return_value=client),
        ):
            req = _mock_request()
            resp = await mod.forward_to_nine_router(req, body, on_usage=on_usage)
            await _drain(resp)

        assert seen == {"input": 5, "output": 3, "model": "good-model"}


# ---------------------------------------------------------------------------
# Direct-to-9router mode toggle
# ---------------------------------------------------------------------------

class TestIsNineRouterDirectEnabled:
    @pytest.mark.asyncio
    async def test_db_key_true(self):
        import kiro.nine_router_client as mod
        mod.invalidate_nine_router_direct_cache()

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("kiro.db.engine.async_session_factory", mock_factory),
            patch("kiro.db.repositories.get_config", AsyncMock(return_value="true")),
        ):
            assert await mod.is_nine_router_direct_enabled() is True

    @pytest.mark.asyncio
    async def test_db_key_false(self):
        import kiro.nine_router_client as mod
        mod.invalidate_nine_router_direct_cache()

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("kiro.db.engine.async_session_factory", mock_factory),
            patch("kiro.db.repositories.get_config", AsyncMock(return_value="false")),
        ):
            assert await mod.is_nine_router_direct_enabled() is False

    @pytest.mark.asyncio
    async def test_db_unavailable_falls_back_to_env(self):
        import kiro.nine_router_client as mod
        mod.invalidate_nine_router_direct_cache()

        with (
            patch("kiro.db.engine.async_session_factory", MagicMock(side_effect=Exception("no db"))),
            patch.object(mod, "ENABLE_NINE_ROUTER_DIRECT", True),
        ):
            assert await mod.is_nine_router_direct_enabled() is True

    @pytest.mark.asyncio
    async def test_cache_invalidation_forces_refetch(self):
        import kiro.nine_router_client as mod
        mod.invalidate_nine_router_direct_cache()

        mock_factory = MagicMock()
        mock_factory.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        mock_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        get_config = AsyncMock(side_effect=["true", "false"])
        with (
            patch("kiro.db.engine.async_session_factory", mock_factory),
            patch("kiro.db.repositories.get_config", get_config),
        ):
            assert await mod.is_nine_router_direct_enabled() is True
            # Cached — second call does not hit DB again.
            assert await mod.is_nine_router_direct_enabled() is True
            assert get_config.await_count == 1
            # Invalidate forces a refetch.
            mod.invalidate_nine_router_direct_cache()
            assert await mod.is_nine_router_direct_enabled() is False
            assert get_config.await_count == 2
