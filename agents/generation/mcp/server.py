from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

try:
    from .proof_context import parse_blueprint
except ImportError:  # pragma: no cover - direct module execution
    from proof_context import parse_blueprint

try:
    from .verification_client import (
        expected_attestation,
        proof_digest,
        validate_service_response,
        verify_blueprint_file,
    )
except ImportError:  # pragma: no cover - direct `python mcp/server.py` execution
    from verification_client import (  # type: ignore[no-redef]
        expected_attestation,
        proof_digest,
        validate_service_response,
        verify_blueprint_file,
    )

try:
    from fastmcp import FastMCP
except ImportError:  # pragma: no cover - dependency should be installed via requirements
    FastMCP = None  # type: ignore[assignment]

_SOURCE_REPO_ROOT = Path(__file__).resolve().parents[1]
# The example runner launches this module from a read-only trusted snapshot
# outside the model-writable workspace. In that mode, business data still lives
# under the explicitly bound generation root.
REPO_ROOT = Path(
    os.getenv("RETHLAS_GENERATION_ROOT", str(_SOURCE_REPO_ROOT))
).resolve(strict=True)
MEMORY_ROOT = REPO_ROOT / "memory"
RESULTS_ROOT = REPO_ROOT / "results"
DATA_ROOT = REPO_ROOT / "data"
# The receipt directory is a trust boundary: generation Codex receives write
# access to ``REPO_ROOT`` and must never be able to redirect receipts into that
# workspace or a generally writable temporary directory.
RECEIPTS_ROOT = REPO_ROOT.parent / ".verification_receipts"

THEOREM_SEARCH_URL = "https://leansearch.net/thm/search"
THEOREM_SEARCH_TASK = (
    "Given a math statement, retrieve useful references, such as theorems, "
    "lemmas, and definitions, that are useful for solving the given problem."
)

VERIFY_PROOF_URL = os.getenv(
    "VERIFY_PROOF_URL",
    "http://127.0.0.1:8091/verify",
)

DEFAULT_MEMORY_SEARCH_MAX_CHARS = 20_000
MAX_OMITTED_IDS = 100
BM25_NEAR_TIE_DECIMALS = 6

CHANNEL_FILES: Dict[str, str] = {
    "immediate_conclusions": "immediate_conclusions.jsonl",
    "toy_examples": "toy_examples.jsonl",
    "counterexamples": "counterexamples.jsonl",
    "big_decisions": "big_decisions.jsonl",
    "subgoals": "subgoals.jsonl",
    "proof_steps": "proof_steps.jsonl",
    "failed_paths": "failed_paths.jsonl",
    "verification_reports": "verification_reports.jsonl",
    "branch_states": "branch_states.jsonl",
    "events": "events.jsonl",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_problem_component(raw: str) -> str:
    cleaned = re.sub(r"\s+", "_", raw.strip())
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", cleaned)
    cleaned = re.sub(r"_+", "_", cleaned).strip("._")
    return cleaned


def sanitize_problem_id(raw: str) -> str:
    """Return a safe problem id while preserving relative path components."""
    normalized = raw.strip().replace("\\", "/")
    parts: List[str] = []
    for part in normalized.split("/"):
        stripped = part.strip()
        if stripped in {"", "."}:
            continue
        if stripped == "..":
            raise ValueError("problem_id must not contain '..' path components")
        cleaned = _sanitize_problem_component(stripped)
        if cleaned:
            parts.append(cleaned)
    return "/".join(parts) or "problem"


_VERIFIED_PROBLEM_COMPONENT_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?$"
)


def validate_verified_problem_id(raw: str) -> str:
    """Validate a publication id without lossy normalization."""
    if not isinstance(raw, str) or not raw or raw != raw.strip() or "\\" in raw:
        raise ValueError("problem_id must be a non-empty normalized relative path")
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("problem_id must not contain empty, '.', or '..' path components")
    if any(
        _VERIFIED_PROBLEM_COMPONENT_RE.fullmatch(part) is None
        for part in parts
    ):
        raise ValueError(
            "problem_id components must use ASCII letters, digits, '.', '_', or '-' "
            "and must begin with an alphanumeric character and end with an "
            "alphanumeric character or '-'"
        )
    return raw


def build_problem_id(source: str, identifier: str) -> str:
    return sanitize_problem_id(f"{source}_{identifier}")


def _resolve_path(path_str: str) -> Path:
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = REPO_ROOT / candidate
    return candidate.resolve()


def _problem_dir(problem_id: str) -> Path:
    sanitized_problem_id = sanitize_problem_id(problem_id)
    problem_dir = (MEMORY_ROOT / sanitized_problem_id).resolve()
    memory_root = MEMORY_ROOT.resolve()
    if not problem_dir.is_relative_to(memory_root):
        raise ValueError("problem_id resolves outside memory root")
    return problem_dir


def _channel_path(problem_id: str, channel: str) -> Path:
    if channel not in CHANNEL_FILES:
        allowed = ", ".join(sorted(CHANNEL_FILES))
        raise ValueError(f"Unknown channel '{channel}'. Allowed channels: {allowed}")
    return _problem_dir(problem_id) / CHANNEL_FILES[channel]


def _iter_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                yield payload


def _append_jsonl(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _new_record_id(prefix: str = "mem") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _validate_supersedes(supersedes: Optional[List[str]]) -> List[str]:
    if supersedes is None:
        return []
    if not isinstance(supersedes, list):
        raise ValueError("supersedes must be a JSON array of record ids")

    normalized: List[str] = []
    seen = set()
    for record_id in supersedes:
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("supersedes entries must be non-empty record id strings")
        cleaned = record_id.strip()
        if cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)
    return normalized


def _read_supersedes(raw: Any) -> List[str]:
    """Read legacy supersedes metadata without rejecting the whole memory file."""
    if isinstance(raw, str):
        candidates = [raw]
    elif isinstance(raw, list):
        candidates = raw
    else:
        candidates = []

    normalized: List[str] = []
    seen = set()
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        cleaned = candidate.strip()
        if cleaned not in seen:
            normalized.append(cleaned)
            seen.add(cleaned)
    return normalized


def _legacy_record_id(channel: str, ordinal: int, item: Dict[str, Any]) -> str:
    canonical = json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    material = f"{channel}\0{ordinal}\0{canonical}".encode("utf-8")
    return f"legacy_{hashlib.sha256(material).hexdigest()[:24]}"


def _load_memory_entries(problem_id: str) -> Dict[str, List[Dict[str, Any]]]:
    entries_by_channel: Dict[str, List[Dict[str, Any]]] = {}
    global_ordinal = 0
    for channel in CHANNEL_FILES:
        entries: List[Dict[str, Any]] = []
        for ordinal, raw_item in enumerate(
            _iter_jsonl(_channel_path(problem_id, channel))
        ):
            raw_record_id = raw_item.get("record_id")
            if isinstance(raw_record_id, str) and raw_record_id.strip():
                record_id = raw_record_id.strip()
            else:
                record_id = _legacy_record_id(channel, ordinal, raw_item)

            raw_active = raw_item.get("active", True)
            declared_active = raw_active if isinstance(raw_active, bool) else True
            supersedes = _read_supersedes(raw_item.get("supersedes"))
            entries.append(
                {
                    "record_id": record_id,
                    "declared_active": declared_active,
                    "supersedes": supersedes,
                    "item": raw_item,
                    "channel": channel,
                    "ordinal": ordinal,
                    "global_ordinal": global_ordinal,
                }
            )
            global_ordinal += 1
        entries_by_channel[channel] = entries

    superseded_by: Dict[str, List[str]] = {}
    for entries in entries_by_channel.values():
        for entry in entries:
            for superseded_id in entry["supersedes"]:
                superseders = superseded_by.setdefault(superseded_id, [])
                if entry["record_id"] not in superseders:
                    superseders.append(entry["record_id"])

    for entries in entries_by_channel.values():
        for entry in entries:
            entry["superseded_by"] = superseded_by.get(entry["record_id"], [])
            entry["effective_active"] = (
                entry["declared_active"] and not entry["superseded_by"]
            )
    return entries_by_channel


def _timestamp_rank(item: Dict[str, Any]) -> float:
    raw_timestamp = item.get("timestamp_utc")
    if not isinstance(raw_timestamp, str) or not raw_timestamp.strip():
        return float("-inf")
    try:
        parsed = datetime.fromisoformat(raw_timestamp.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return float("-inf")


def _candidate_rank(candidate: Dict[str, Any], newest_first: bool) -> tuple:
    score = candidate["score"]
    if newest_first:
        # Relevance remains primary. Rounding only groups numerically negligible
        # BM25 differences so recency can settle exact or near ties.
        return (
            round(score, BM25_NEAR_TIE_DECIMALS),
            candidate["timestamp_rank"],
            candidate["global_ordinal"],
            score,
        )
    return (
        score,
        -candidate["timestamp_rank"],
        -candidate["global_ordinal"],
    )


def _compact_json_chars(value: Any) -> int:
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


def _tokenize_bm25(text: str) -> List[str]:
    return re.findall(r"[A-Za-z0-9_]+", text.lower())


def _bm25_score_documents(
    query: str,
    documents: List[List[str]],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> List[float]:
    query_tokens = _tokenize_bm25(query)
    if not query_tokens or not documents:
        return [0.0 for _ in documents]

    query_term_counts = Counter(query_tokens)
    document_frequencies: Counter[str] = Counter()
    document_term_counts = [Counter(document) for document in documents]
    document_lengths = [len(document) for document in documents]
    avg_doc_length = sum(document_lengths) / len(document_lengths) if document_lengths else 0.0
    total_documents = len(documents)

    for document in documents:
        for token in set(document):
            document_frequencies[token] += 1

    scores: List[float] = []
    for doc_counts, doc_length in zip(document_term_counts, document_lengths):
        score = 0.0
        norm = k1 * (1.0 - b + b * (doc_length / avg_doc_length)) if avg_doc_length > 0 else k1
        for token, query_tf in query_term_counts.items():
            term_frequency = doc_counts.get(token, 0)
            if term_frequency <= 0:
                continue
            document_frequency = document_frequencies.get(token, 0)
            idf = math.log(1.0 + ((total_documents - document_frequency + 0.5) / (document_frequency + 0.5)))
            numerator = term_frequency * (k1 + 1.0)
            denominator = term_frequency + norm
            score += query_tf * idf * (numerator / denominator)
        scores.append(score)

    return scores
def search_arxiv_theorems(
    query: str,
    num_results: int = 10,
    endpoint: str = THEOREM_SEARCH_URL,
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    if not query.strip():
        raise ValueError("query must be non-empty")
    if num_results <= 0:
        raise ValueError("num_results must be > 0")

    payload = {
        "query": query,
        "task": THEOREM_SEARCH_TASK,
        "num_results": num_results,
    }

    response = requests.post(endpoint, json=payload, timeout=timeout_seconds)
    response.raise_for_status()

    data = response.json()
    if not isinstance(data, list):
        raise ValueError("The theorem endpoint must return a JSON list")

    normalized: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        normalized.append(
            {
                "title": str(item.get("title", "")),
                "theorem": str(item.get("theorem", "")),
                "arxiv_id": str(item.get("arxiv_id", "")),
                "theorem_id": str(item.get("theorem_id", "")),
            }
        )

    return {
        "query": query,
        "count": len(normalized),
        "results": normalized,
        "endpoint": endpoint,
    }


def verify_proof_service(
    statement: str,
    proof: str,
    endpoint: str = VERIFY_PROOF_URL,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("statement must be non-empty")
    if not isinstance(proof, str):
        raise ValueError("proof must be markdown text")
    if not proof.strip():
        raise ValueError("proof markdown must be non-empty")

    payload = {
        "statement": statement,
        "proof": proof,
    }

    request_kwargs: Dict[str, Any] = {
        "json": payload,
        "timeout": timeout_seconds,
    }
    api_token = os.getenv("VERIFY_API_TOKEN")
    if api_token:
        request_kwargs["headers"] = {"Authorization": f"Bearer {api_token}"}
    response = requests.post(endpoint, **request_kwargs)
    response.raise_for_status()

    try:
        body = response.json()
    except ValueError as exc:
        raise ValueError("verification service returned non-JSON response") from exc

    expected_ids, expected_context_digest = expected_attestation(
        proof=proof,
        statement=statement,
    )
    expected_manifest = parse_blueprint(proof, target_statement=statement)
    return validate_service_response(
        body,
        expected_proof_digest=proof_digest(proof),
        expected_checked_item_ids=expected_ids,
        expected_context_digest=expected_context_digest,
        expected_manifest=expected_manifest,
    )


def _trusted_problem_statement(problem_id: str) -> tuple[str, str]:
    sanitized_problem_id = validate_verified_problem_id(problem_id)
    expected_problem_id = os.getenv("RETHLAS_EXPECTED_PROBLEM_ID")
    if expected_problem_id and sanitized_problem_id != expected_problem_id:
        raise ValueError("problem_id does not match the runner-bound problem")

    data_root = DATA_ROOT.resolve()
    source_path = DATA_ROOT / f"{sanitized_problem_id}.md"
    resolved_source = source_path.resolve(strict=True)
    if not resolved_source.is_relative_to(data_root) or source_path.is_symlink():
        raise ValueError("problem source must be a regular file inside data root")
    descriptor = os.open(
        source_path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("problem source must be a regular file")
        if metadata.st_size > 400_000:
            raise ValueError("problem source is too large")
        raw_statement = os.read(descriptor, 400_001)
        if len(raw_statement) > 400_000:
            raise ValueError("problem source is too large")
    finally:
        os.close(descriptor)
    try:
        statement = raw_statement.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("problem source must be valid UTF-8") from exc
    if not statement.strip():
        raise ValueError("problem source must be non-empty")
    if len(statement) > 100_000:
        raise ValueError("problem source exceeds verifier statement limit")
    statement_digest = hashlib.sha256(raw_statement).hexdigest()
    expected_digest = os.getenv("RETHLAS_EXPECTED_STATEMENT_SHA256")
    if expected_digest and statement_digest != expected_digest:
        raise ValueError("problem source changed after the runner bound its digest")
    return statement, statement_digest


def verify_blueprint_service(
    problem_id: str,
    endpoint: str = VERIFY_PROOF_URL,
    timeout_seconds: int = 3600,
) -> Dict[str, Any]:
    """Verify a draft against its trusted data file and publish with a receipt."""

    sanitized_problem_id = validate_verified_problem_id(problem_id)
    statement, _statement_digest = _trusted_problem_statement(sanitized_problem_id)
    results_root = Path(os.path.abspath(os.fspath(RESULTS_ROOT)))
    result_dir = results_root.joinpath(*sanitized_problem_id.split("/"))
    receipts_root = RECEIPTS_ROOT.resolve()
    if receipts_root.is_relative_to(REPO_ROOT.resolve()):
        raise ValueError("trusted receipt root must be outside the generation workspace")
    receipt_path = (receipts_root / f"{sanitized_problem_id}.json").resolve()
    if not receipt_path.is_relative_to(receipts_root):
        raise ValueError("problem_id resolves outside receipt root")
    return verify_blueprint_file(
        statement=statement,
        draft_path=result_dir / "blueprint.md",
        verified_path=result_dir / "blueprint_verified.md",
        endpoint=endpoint,
        timeout_seconds=timeout_seconds,
        api_token=os.getenv("VERIFY_API_TOKEN") or None,
        receipt_path=receipt_path,
        problem_id=sanitized_problem_id,
        blueprint_root=results_root,
    )


def memory_init(
    problem_id: str,
    meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    sanitized_problem_id = sanitize_problem_id(problem_id)
    problem_dir = _problem_dir(sanitized_problem_id)
    problem_dir.mkdir(parents=True, exist_ok=True)

    created_files: Dict[str, str] = {}
    for channel, filename in CHANNEL_FILES.items():
        channel_path = problem_dir / filename
        channel_path.touch(exist_ok=True)
        created_files[channel] = str(channel_path)

    meta_path = problem_dir / "meta.json"
    existing_meta: Dict[str, Any] = {}
    if meta_path.exists() and meta_path.stat().st_size > 0:
        with meta_path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
            if isinstance(loaded, dict):
                existing_meta = loaded

    merged_meta: Dict[str, Any] = {
        "problem_id": sanitized_problem_id,
        "created_at_utc": existing_meta.get("created_at_utc", _utc_now()),
        "updated_at_utc": _utc_now(),
    }
    merged_meta.update(existing_meta)
    if meta:
        merged_meta.update(meta)

    with meta_path.open("w", encoding="utf-8") as handle:
        json.dump(merged_meta, handle, indent=2, ensure_ascii=False)

    return {
        "problem_id": sanitized_problem_id,
        "memory_dir": str(problem_dir),
        "meta_path": str(meta_path),
        "channels": created_files,
    }


def memory_append(
    problem_id: str,
    channel: str,
    record: Dict[str, Any],
    active: bool = True,
    supersedes: Optional[List[str]] = None,
    return_mode: str = "metadata",
) -> Dict[str, Any]:
    """Append one fact and return a compact receipt unless full mode is requested.

    ``supersedes`` is append-only metadata: referenced records are treated as
    inactive by searches, while legacy records without lifecycle metadata remain
    active by default.
    """
    if not isinstance(record, dict):
        raise ValueError("record must be a JSON object")
    if not isinstance(active, bool):
        raise ValueError("active must be a boolean")
    if not isinstance(return_mode, str) or return_mode not in {"metadata", "full"}:
        raise ValueError("return_mode must be either 'metadata' or 'full'")

    normalized_supersedes = _validate_supersedes(supersedes)
    target = _channel_path(problem_id, channel)

    memory_init(problem_id)

    record_id = _new_record_id()
    entry = {
        "record_id": record_id,
        "timestamp_utc": _utc_now(),
        "channel": channel,
        "active": active,
        "supersedes": normalized_supersedes,
        "record": record,
    }
    _append_jsonl(target, entry)

    if channel != "events":
        event_entry = {
            "record_id": _new_record_id("event"),
            "timestamp_utc": _utc_now(),
            "event_type": "memory_append",
            "channel": channel,
            "active": True,
            "supersedes": [],
            "appended_record_id": record_id,
        }
        _append_jsonl(_channel_path(problem_id, "events"), event_entry)

    response = {
        "status": "ok",
        "problem_id": sanitize_problem_id(problem_id),
        "record_id": record_id,
        "timestamp_utc": entry["timestamp_utc"],
        "channel": channel,
        "path": str(target),
        "active": active,
        "supersedes": normalized_supersedes,
    }
    if return_mode == "full":
        response["entry"] = entry
    return response


def memory_search(
    problem_id: str,
    query: str,
    channels: Optional[List[str]] = None,
    limit_per_channel: int = 10,
    max_chars: int = DEFAULT_MEMORY_SEARCH_MAX_CHARS,
    include_inactive: bool = False,
    newest_first: bool = True,
) -> Dict[str, Any]:
    """Search active memory with an explicit whole-result character budget.

    ``complete`` is false whenever positive-scoring matches are omitted by either
    ``limit_per_channel`` or ``max_chars``. ``returned_chars`` counts compact JSON
    characters for the returned result objects; no stored item is partially cut.
    BM25 relevance is primary and recency only breaks exact or near ties.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty")
    if (
        not isinstance(limit_per_channel, int)
        or isinstance(limit_per_channel, bool)
        or limit_per_channel <= 0
    ):
        raise ValueError("limit_per_channel must be > 0")
    if (
        not isinstance(max_chars, int)
        or isinstance(max_chars, bool)
        or max_chars <= 0
    ):
        raise ValueError("max_chars must be a positive integer")
    if not isinstance(include_inactive, bool):
        raise ValueError("include_inactive must be a boolean")
    if not isinstance(newest_first, bool):
        raise ValueError("newest_first must be a boolean")

    if channels is None:
        search_channels = [name for name in CHANNEL_FILES if name != "events"]
    else:
        if not isinstance(channels, list):
            raise ValueError("channels must be a JSON array")
        search_channels = []
        for channel in channels:
            if not isinstance(channel, str):
                raise ValueError("channels entries must be strings")
            _channel_path(problem_id, channel)
            if channel not in search_channels:
                search_channels.append(channel)

    entries_by_channel = _load_memory_entries(problem_id)
    ranked_by_channel: Dict[str, List[Dict[str, Any]]] = {}
    corpus_count = 0
    for channel in search_channels:
        channel_entries = [
            entry
            for entry in entries_by_channel[channel]
            if include_inactive or entry["effective_active"]
        ]
        corpus_count += len(channel_entries)
        documents = [
            json.dumps(entry["item"], ensure_ascii=False) for entry in channel_entries
        ]
        tokenized_documents = [_tokenize_bm25(document) for document in documents]
        scores = _bm25_score_documents(query, tokenized_documents)

        candidates: List[Dict[str, Any]] = []
        for entry, score in zip(channel_entries, scores):
            if score <= 0:
                continue
            normalized_item = dict(entry["item"])
            normalized_item["record_id"] = entry["record_id"]
            normalized_item["active"] = entry["effective_active"]
            normalized_item["supersedes"] = entry["supersedes"]
            if entry["declared_active"] != entry["effective_active"]:
                normalized_item["declared_active"] = entry["declared_active"]
            if entry["superseded_by"]:
                normalized_item["superseded_by"] = entry["superseded_by"]

            result = {
                "score": score,
                "item": normalized_item,
            }
            candidates.append(
                {
                    "key": (channel, entry["ordinal"]),
                    "channel": channel,
                    "record_id": entry["record_id"],
                    "score": score,
                    "timestamp_rank": _timestamp_rank(entry["item"]),
                    "global_ordinal": entry["global_ordinal"],
                    "result": result,
                    "chars": _compact_json_chars(result),
                }
            )

        candidates.sort(
            key=lambda candidate: _candidate_rank(candidate, newest_first),
            reverse=True,
        )
        ranked_by_channel[channel] = candidates

    budget_candidates: List[Dict[str, Any]] = []
    for channel in search_channels:
        budget_candidates.extend(ranked_by_channel[channel][:limit_per_channel])
    budget_candidates.sort(
        key=lambda candidate: _candidate_rank(candidate, newest_first),
        reverse=True,
    )

    returned_keys = set()
    returned_chars = 0
    for candidate in budget_candidates:
        candidate_chars = candidate["chars"]
        if returned_chars + candidate_chars > max_chars:
            continue
        returned_keys.add(candidate["key"])
        returned_chars += candidate_chars

    all_matches: List[Dict[str, Any]] = []
    for channel in search_channels:
        all_matches.extend(ranked_by_channel[channel])
    all_matches.sort(
        key=lambda candidate: _candidate_rank(candidate, newest_first),
        reverse=True,
    )

    results_by_channel: Dict[str, Dict[str, Any]] = {}
    for channel in search_channels:
        ranked = ranked_by_channel[channel]
        returned = [
            candidate for candidate in ranked if candidate["key"] in returned_keys
        ]
        omitted = [
            candidate for candidate in ranked if candidate["key"] not in returned_keys
        ]
        results_by_channel[channel] = {
            "corpus_count": sum(
                1
                for entry in entries_by_channel[channel]
                if include_inactive or entry["effective_active"]
            ),
            "matched_count": len(ranked),
            "count": len(returned),
            "complete": not omitted,
            "truncated": bool(omitted),
            "omitted_count": len(omitted),
            "omitted_ids": [
                candidate["record_id"] for candidate in omitted[:MAX_OMITTED_IDS]
            ],
            "omitted_ids_complete": len(omitted) <= MAX_OMITTED_IDS,
            "returned_chars": sum(candidate["chars"] for candidate in returned),
            "results": [candidate["result"] for candidate in returned],
        }

    omitted_matches = [
        candidate for candidate in all_matches if candidate["key"] not in returned_keys
    ]

    return {
        "problem_id": sanitize_problem_id(problem_id),
        "query": query,
        "channels": search_channels,
        "limit_per_channel": limit_per_channel,
        "max_chars": max_chars,
        "include_inactive": include_inactive,
        "newest_first": newest_first,
        "corpus_count": corpus_count,
        "matched_count": len(all_matches),
        "count": len(returned_keys),
        "complete": not omitted_matches,
        "truncated": bool(omitted_matches),
        "omitted_count": len(omitted_matches),
        "omitted_ids": [
            candidate["record_id"]
            for candidate in omitted_matches[:MAX_OMITTED_IDS]
        ],
        "omitted_ids_complete": len(omitted_matches) <= MAX_OMITTED_IDS,
        "returned_chars": returned_chars,
        "results_by_channel": results_by_channel,
    }


def branch_update(
    problem_id: str,
    branch_id: str,
    state: Dict[str, Any],
) -> Dict[str, Any]:
    payload = {
        "branch_id": branch_id,
        "state": state,
    }
    return memory_append(problem_id, "branch_states", payload)


def build_mcp_app() -> Optional[Any]:
    if FastMCP is None:
        return None

    app = FastMCP("reasoning-agent")

    @app.tool(name="search_arxiv_theorems")
    def _tool_search_arxiv_theorems(
        query: str,
        num_results: int = 10,
    ) -> Dict[str, Any]:
        return search_arxiv_theorems(query=query, num_results=num_results)

    @app.tool(name="verify_blueprint_service")
    def _tool_verify_blueprint_service(
        problem_id: str,
    ) -> Dict[str, Any]:
        """Verify and atomically publish results/{problem_id}/blueprint.md."""
        return verify_blueprint_service(problem_id=problem_id)

    @app.tool(name="memory_init")
    def _tool_memory_init(
        problem_id: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return memory_init(problem_id=problem_id, meta=meta)

    @app.tool(name="memory_append")
    def _tool_memory_append(
        problem_id: str,
        channel: str,
        record: Dict[str, Any],
        active: bool = True,
        supersedes: Optional[List[str]] = None,
        return_mode: str = "metadata",
    ) -> Dict[str, Any]:
        """Append a record and return compact metadata, or the full entry on request."""
        return memory_append(
            problem_id=problem_id,
            channel=channel,
            record=record,
            active=active,
            supersedes=supersedes,
            return_mode=return_mode,
        )

    @app.tool(name="memory_search")
    def _tool_memory_search(
        problem_id: str,
        query: str,
        channels: Optional[List[str]] = None,
        limit_per_channel: int = 10,
        max_chars: int = DEFAULT_MEMORY_SEARCH_MAX_CHARS,
        include_inactive: bool = False,
        newest_first: bool = True,
    ) -> Dict[str, Any]:
        """BM25-search active records within a whole-record character budget."""
        return memory_search(
            problem_id=problem_id,
            query=query,
            channels=channels,
            limit_per_channel=limit_per_channel,
            max_chars=max_chars,
            include_inactive=include_inactive,
            newest_first=newest_first,
        )

    @app.tool(name="branch_update")
    def _tool_branch_update(
        problem_id: str,
        branch_id: str,
        state: Dict[str, Any],
    ) -> Dict[str, Any]:
        return branch_update(problem_id=problem_id, branch_id=branch_id, state=state)

    return app


APP = build_mcp_app()


def main() -> None:
    if APP is None:
        raise SystemExit(
            "fastmcp is not installed. Install requirements from mcp/requirements.txt first."
        )
    APP.run()


if __name__ == "__main__":
    main()
