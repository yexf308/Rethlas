from __future__ import annotations

from typing import Any

import pytest
import requests

from agents.generation.mcp import server


class _Response:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self._payload


class _StreamingResponse:
    def __init__(
        self, chunks: list[bytes], *, content_length: int | None = None
    ) -> None:
        self._chunks = chunks
        self.headers = (
            {"Content-Length": str(content_length)}
            if content_length is not None
            else {}
        )
        self.closed = False

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int):
        assert chunk_size == 16_384
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


class _CloseFailingResponse(_Response):
    def __init__(self, payload: Any, *, status_code: int | None = None) -> None:
        super().__init__(payload)
        self._status_code = status_code

    def raise_for_status(self) -> None:
        if self._status_code is None:
            return
        response = requests.Response()
        response.status_code = self._status_code
        raise requests.HTTPError(response=response)

    def close(self) -> None:
        raise RuntimeError("close failed")


class _IterFailingCloseResponse:
    headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    def iter_content(self, *, chunk_size: int):
        assert chunk_size == 16_384
        raise requests.exceptions.ChunkedEncodingError("truncated chunk")
        yield b""  # pragma: no cover - keeps this a generator

    def close(self) -> None:
        raise RuntimeError("close failed")


def _matlas_result(*, candidate_id: str = "candidate-7") -> dict[str, str]:
    return {
        "type": "paper",
        "entity_name": "Theorem 3.1",
        "doi": "10.1000/example",
        "title": "A published result",
        "authors": "A. Author; B. Author",
        "journal": "Journal of Examples",
        "year": "2024",
        "statement": "For every admissible object, the bound holds.",
        "candidate_id": candidate_id,
    }


def test_official_matlas_uses_v0_1_protocol_and_truncates_small_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any], dict[str, str], int, bool]] = []

    def post(
        url: str,
        *,
        json: dict[str, Any],
        headers: dict[str, str],
        timeout: int,
        stream: bool,
    ) -> _Response:
        calls.append((url, json, headers, timeout, stream))
        return _Response(
            [_matlas_result(candidate_id=f"candidate-{i}") for i in range(10)]
        )

    monkeypatch.setattr(server.requests, "post", post)
    result = server.search_matlas_theorems(
        "  Singer difference set distance  ",
        num_results=3,
        timeout_seconds=7,
    )

    assert calls == [
        (
            "https://matlas.ai/api/search",
            {"query": "Singer difference set distance", "num_results": 10},
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "rethlas/1.0",
            },
            7,
            True,
        )
    ]
    assert result == {
        "schema_version": "rethlas_external_retrieval_v1",
        "provider": "matlas_official_v0_1",
        "provider_protocol": "matlas_openapi_0_1_0",
        "endpoint": "https://matlas.ai/api/search",
        "query": "Singer difference set distance",
        "requested_count": 3,
        "count": 3,
        "results": [_matlas_result(candidate_id=f"candidate-{i}") for i in range(3)],
        "scope": "published_mathematical_statements",
        "mathematical_evidence_authority": False,
        "fallback_used": False,
        "retrieval_status": "ok",
    }


def test_legacy_arxiv_provider_remains_separate_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []
    legacy_result = {
        "title": "An arXiv preprint",
        "theorem": "A legacy theorem snippet.",
        "arxiv_id": "2601.01234",
        "theorem_id": "thm-7",
    }

    def post(url: str, *, json: dict[str, Any], **_: Any) -> _Response:
        calls.append((url, json))
        return _Response([legacy_result])

    monkeypatch.setattr(server.requests, "post", post)
    result = server.search_arxiv_theorems("named gap", num_results=2)

    assert calls == [
        (
            "https://leansearch.net/thm/search",
            {
                "query": "named gap",
                "task": server.THEOREM_SEARCH_TASK,
                "num_results": 2,
            },
        )
    ]
    assert result["provider"] == "danus_legacy_arxiv_theorem_v1"
    assert result["provider_protocol"] == "danus_legacy_arxiv_theorem_search_v1"
    assert result["fallback_used"] is False
    assert result["results"] == [legacy_result]
    assert result["results"][0].get("candidate_id") is None


def test_legacy_arxiv_provider_truncates_an_overfull_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    upstream = [
        {
            "title": f"Paper {index}",
            "theorem": f"Statement {index}",
            "arxiv_id": f"2601.{index:05d}",
            "theorem_id": f"thm-{index}",
        }
        for index in range(5)
    ]
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *args, **kwargs: _Response(upstream),
    )

    result = server.search_arxiv_theorems("named gap", num_results=2)

    assert result["requested_count"] == 2
    assert result["count"] == 2
    assert result["results"] == upstream[:2]


def test_matlas_outage_returns_auditable_nonfallback_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = server.requests.Response()
    response.status_code = 503

    def unavailable(*args: Any, **kwargs: Any) -> _Response:
        raise server.requests.HTTPError(response=response)

    monkeypatch.setattr(server.requests, "post", unavailable)
    result = server.search_matlas_theorems("named gap", num_results=2)

    assert result["retrieval_status"] == "error"
    assert result["error"] == "http 503"
    assert result["results"] == []
    assert result["provider"] == "matlas_official_v0_1"
    assert result["fallback_used"] is False


@pytest.mark.parametrize(
    "search",
    [server.search_matlas_theorems, server.search_arxiv_theorems],
)
def test_provider_close_failure_after_success_is_audited(
    monkeypatch: pytest.MonkeyPatch,
    search,
) -> None:
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *args, **kwargs: _CloseFailingResponse([]),
    )

    result = search("named gap")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "network response_close_failed"
    assert result["results"] == []


@pytest.mark.parametrize(
    "search",
    [server.search_matlas_theorems, server.search_arxiv_theorems],
)
def test_provider_close_failure_does_not_override_http_error(
    monkeypatch: pytest.MonkeyPatch,
    search,
) -> None:
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *args, **kwargs: _CloseFailingResponse([], status_code=503),
    )

    result = search("named gap")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "http 503"
    assert result["results"] == []


@pytest.mark.parametrize(
    "search",
    [server.search_matlas_theorems, server.search_arxiv_theorems],
)
def test_provider_close_failure_does_not_override_stream_error(
    monkeypatch: pytest.MonkeyPatch,
    search,
) -> None:
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *args, **kwargs: _IterFailingCloseResponse(),
    )

    result = search("named gap")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "network ChunkedEncodingError"
    assert result["results"] == []


@pytest.mark.parametrize("announced_length", [True, False])
def test_matlas_raw_response_body_is_bounded_before_json_parse(
    monkeypatch: pytest.MonkeyPatch,
    announced_length: bool,
) -> None:
    cap = server.MAX_EXTERNAL_RAW_RESPONSE_BYTES
    response = _StreamingResponse(
        [] if announced_length else [b"x" * (cap // 2 + 1), b"x" * (cap // 2 + 1)],
        content_length=cap + 1 if announced_length else None,
    )
    monkeypatch.setattr(server.requests, "post", lambda *args, **kwargs: response)

    result = server.search_matlas_theorems("named gap")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "response_too_large"
    assert result["results"] == []
    assert response.closed is True


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        ({"detail": "not an array"}, "invalid_response_type:dict"),
        ([{"type": "paper"}], "invalid_result:0:fields"),
        ([{**_matlas_result(), "type": "preprint"}], "invalid_result:0:type"),
        ([{**_matlas_result(), "type": []}], "invalid_result:0:type"),
        ([{**_matlas_result(), "type": {}}], "invalid_result:0:type"),
    ],
)
def test_matlas_malformed_results_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
    expected_error: str,
) -> None:
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *args, **kwargs: _Response(payload),
    )

    result = server.search_matlas_theorems("named gap")

    assert result["retrieval_status"] == "error"
    assert result["error"] == expected_error
    assert result["results"] == []


@pytest.mark.parametrize("count", [0, 201])
def test_both_providers_reject_out_of_contract_counts(count: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 200"):
        server.search_matlas_theorems("gap", num_results=count)
    with pytest.raises(ValueError, match="between 1 and 200"):
        server.search_arxiv_theorems("gap", num_results=count)


@pytest.mark.parametrize("count", [True, 1.5, "2", None])
def test_both_providers_reject_noninteger_counts_before_network(
    monkeypatch: pytest.MonkeyPatch,
    count: Any,
) -> None:
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid count reached provider"),
    )

    with pytest.raises(ValueError, match="integer between 1 and 200"):
        server.search_matlas_theorems("gap", num_results=count)
    with pytest.raises(ValueError, match="integer between 1 and 200"):
        server.search_arxiv_theorems("gap", num_results=count)


@pytest.mark.parametrize("query", [None, 123, b"gap", "\ud800"])
def test_both_providers_reject_non_utf8_string_queries_before_network(
    monkeypatch: pytest.MonkeyPatch,
    query: Any,
) -> None:
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *args, **kwargs: pytest.fail("invalid query reached provider"),
    )

    with pytest.raises(ValueError, match="query must"):
        server.search_matlas_theorems(query)
    with pytest.raises(ValueError, match="query must"):
        server.search_arxiv_theorems(query)


def test_both_providers_reject_an_oversized_query_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_post(*args: Any, **kwargs: Any) -> _Response:
        pytest.fail("oversized query reached a provider")

    monkeypatch.setattr(server.requests, "post", unexpected_post)
    query = "x" * (server.MAX_EXTERNAL_QUERY_UTF8_BYTES + 1)

    with pytest.raises(ValueError, match="byte limit"):
        server.search_matlas_theorems(query)
    with pytest.raises(ValueError, match="byte limit"):
        server.search_arxiv_theorems(query)


@pytest.mark.parametrize(
    ("search", "payload"),
    [
        (
            server.search_matlas_theorems,
            [{**_matlas_result(), "statement": "x" * 40_000}],
        ),
        (
            server.search_arxiv_theorems,
            [
                {
                    "title": "Paper",
                    "theorem": "x" * 40_000,
                    "arxiv_id": "2601.01234",
                    "theorem_id": "thm-7",
                }
            ],
        ),
    ],
)
def test_retrieval_success_envelope_is_bounded_without_text_truncation(
    monkeypatch: pytest.MonkeyPatch,
    search,
    payload: Any,
) -> None:
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *args, **kwargs: _Response(payload),
    )

    result = search("named gap")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "response_too_large"
    assert result["count"] == 0
    assert result["results"] == []


@pytest.mark.parametrize(
    ("search", "payload"),
    [
        (
            server.search_matlas_theorems,
            [{**_matlas_result(), "statement": "\ud800"}],
        ),
        (
            server.search_arxiv_theorems,
            [
                {
                    "title": "Paper",
                    "theorem": "\ud800",
                    "arxiv_id": "2601.01234",
                    "theorem_id": "thm-7",
                }
            ],
        ),
    ],
)
def test_retrieval_rejects_non_utf8_provider_text(
    monkeypatch: pytest.MonkeyPatch,
    search,
    payload: Any,
) -> None:
    monkeypatch.setattr(
        server.requests,
        "post",
        lambda *args, **kwargs: _Response(payload),
    )

    result = search("named gap")

    assert result["retrieval_status"] == "error"
    assert result["error"] == "invalid_result:0:encoding"
    assert result["results"] == []


def test_mcp_registry_exposes_both_distinct_retrieval_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool_names: set[str] = set()
    functions: dict[str, Any] = {}

    class FakeMCP:
        def __init__(self, _name: str) -> None:
            pass

        def tool(self, *, name: str):
            tool_names.add(name)

            def decorate(function):
                functions[name] = function
                return function

            return decorate

    monkeypatch.setattr(server, "FastMCP", FakeMCP)
    server.build_mcp_app()

    assert "search_matlas_theorems" in tool_names
    assert "search_arxiv_theorems" in tool_names
    for name in ("search_matlas_theorems", "search_arxiv_theorems"):
        assert functions[name]._rethlas_phase_guarded is True
        assert functions[name]._rethlas_rehydrate_guarded is True
