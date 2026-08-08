"""Fail-closed verification client and atomic blueprint promotion."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

import requests

try:
    from .proof_context import aggregate_context_digest, parse_blueprint
except ImportError:  # pragma: no cover - direct module execution
    from proof_context import aggregate_context_digest, parse_blueprint


_OUTPUT_FIELDS = {
    "verification_report",
    "verdict",
    "repair_hints",
    "checked_item_ids",
    "proof_digest",
    "context_digest",
}
_REPORT_FIELDS = {"summary", "critical_errors", "gaps"}
_FINDING_FIELDS = {"location", "issue"}
_ITEM_ID_RE = re.compile(r"^pi_[0-9a-f]{24}$")
_HEX_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_BLUEPRINT_CHARS = int(os.getenv("VERIFY_MAX_PROOF_CHARS", "2000000"))
MAX_BLUEPRINT_BYTES = int(os.getenv("VERIFY_MAX_PROOF_BYTES", "8000000"))
_LOOPBACK_HTTP_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _absolute_path(path: Path) -> Path:
    """Return a normalized absolute path without resolving any symlinks."""

    return Path(os.path.abspath(os.fspath(path)))


def _validate_endpoint(endpoint: str) -> str:
    if not isinstance(endpoint, str) or not endpoint:
        raise ValueError("verification endpoint must be a non-empty URL")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        # Accessing port also rejects malformed and out-of-range values.
        parsed.port
    except ValueError as exc:
        raise ValueError("verification endpoint is not a valid URL") from exc
    if hostname is None or parsed.username is not None or parsed.password is not None:
        raise ValueError(
            "verification endpoint must have a host and must not contain userinfo"
        )
    if parsed.scheme == "https":
        return endpoint
    if parsed.scheme == "http" and hostname.lower() in _LOOPBACK_HTTP_HOSTS:
        return endpoint
    raise ValueError(
        "verification endpoint must use HTTPS or HTTP on "
        "127.0.0.1, localhost, or ::1"
    )


def _open_directory(path: Path, *, label: str) -> int:
    """Open a directory without following its final path component."""

    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} must be an existing non-symlink directory: {path}") from exc
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):  # pragma: no cover - O_DIRECTORY
        os.close(descriptor)
        raise ValueError(f"{label} must be a directory: {path}")
    return descriptor


def _assert_directory_binding(path: Path, descriptor: int, *, label: str) -> None:
    """Require *path* to still name the directory held by *descriptor*."""

    held = os.fstat(descriptor)
    try:
        current_descriptor = _open_directory(path, label=label)
    except ValueError as exc:
        raise ValueError(f"{label} changed during verification: {path}") from exc
    try:
        current = os.fstat(current_descriptor)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError(f"{label} changed during verification: {path}")
    finally:
        os.close(current_descriptor)


def _directory_parts_beneath(
    root: Path,
    target: Path,
    *,
    label: str,
) -> tuple[str, ...]:
    """Return lexical target components below *root* without resolving links."""

    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must be inside trusted blueprint root: {root}") from exc
    parts = relative.parts
    if any(part in {"", ".", ".."} or "/" in part for part in parts):
        raise ValueError(f"{label} has unsafe path components: {target}")
    return parts


def _open_directory_at(
    root_fd: int,
    parts: tuple[str, ...],
    *,
    label: str,
) -> int:
    """Walk directory components relative to a held root without following links."""

    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.dup(root_fd)
    try:
        for part in parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):  # pragma: no cover
            raise ValueError(f"{label} must be a directory")
        return descriptor
    except Exception as exc:
        os.close(descriptor)
        if isinstance(exc, ValueError):
            raise
        raise ValueError(
            f"{label} must be reachable through non-symlink directories"
        ) from exc


def _assert_directory_at_binding(
    root_fd: int,
    parts: tuple[str, ...],
    descriptor: int,
    *,
    label: str,
) -> None:
    """Require the root-relative path to still name the held directory."""

    held = os.fstat(descriptor)
    try:
        current_descriptor = _open_directory_at(root_fd, parts, label=label)
    except ValueError as exc:
        raise ValueError(f"{label} changed during verification") from exc
    try:
        current = os.fstat(current_descriptor)
        if (held.st_dev, held.st_ino) != (current.st_dev, current.st_ino):
            raise ValueError(f"{label} changed during verification")
    finally:
        os.close(current_descriptor)


def _read_regular_blueprint_at(
    directory_fd: int,
    filename: str,
    *,
    display_path: Path,
    label: str,
) -> str:
    """Read a bounded regular file relative to an already trusted directory."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(filename, flags, dir_fd=directory_fd)
    except OSError as exc:
        raise ValueError(
            f"{label} must be an existing regular file: {display_path}"
        ) from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"{label} must be a regular file: {display_path}")
        if metadata.st_size > MAX_BLUEPRINT_BYTES:
            raise ValueError(f"{label} exceeds VERIFY_MAX_PROOF_BYTES")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = -1
            raw = handle.read(MAX_BLUEPRINT_BYTES + 1)
        if len(raw) > MAX_BLUEPRINT_BYTES:
            raise ValueError(f"{label} exceeds VERIFY_MAX_PROOF_BYTES")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError(f"{label} must be valid UTF-8") from exc
        if len(text) > MAX_BLUEPRINT_CHARS:
            raise ValueError(f"{label} exceeds VERIFY_MAX_PROOF_CHARS")
        return text
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_lock_file_at(
    directory_fd: int,
    filename: str,
    *,
    display_path: Path,
) -> Any:
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    while True:
        try:
            descriptor = os.open(
                filename,
                os.O_RDWR | nofollow,
                dir_fd=directory_fd,
            )
            break
        except FileNotFoundError:
            try:
                descriptor = os.open(
                    filename,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | nofollow,
                    0o600,
                    dir_fd=directory_fd,
                )
                break
            except FileExistsError:
                # Another publisher won the lock-file creation race. Open the
                # now-existing inode without O_CREAT on the next pass.
                continue
            except OSError as exc:
                raise ValueError(
                    f"publication lock must not be a symlink: {display_path}"
                ) from exc
        except OSError as exc:
            raise ValueError(
                f"publication lock must not be a symlink: {display_path}"
            ) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError(
                f"publication lock must be a regular file: {display_path}"
            )
        return os.fdopen(descriptor, "r+", encoding="utf-8")
    except Exception:
        os.close(descriptor)
        raise


def _lstat_at(directory_fd: int, filename: str) -> os.stat_result | None:
    try:
        return os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _write_all(descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:  # pragma: no cover - os.write either writes or raises
            raise OSError("short write while publishing verified blueprint")
        view = view[written:]


def _atomic_replace_at(
    directory_fd: int,
    filename: str,
    content: bytes,
) -> tuple[int, int]:
    """Atomically replace *filename* using only operations relative to *directory_fd*."""

    temporary_name = f".{filename}.{secrets.token_hex(16)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        try:
            _write_all(descriptor, content)
            os.fsync(descriptor)
            os.replace(
                temporary_name,
                filename,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            published = os.fstat(descriptor)
            os.fsync(directory_fd)
            return published.st_dev, published.st_ino
        finally:
            os.close(descriptor)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass


def _unlink_if_identity_at(
    directory_fd: int,
    filename: str,
    identity: tuple[int, int],
) -> None:
    metadata = _lstat_at(directory_fd, filename)
    if metadata is None or (metadata.st_dev, metadata.st_ino) != identity:
        return
    os.unlink(filename, dir_fd=directory_fd)
    os.fsync(directory_fd)


def _unlink_path_if_identity(path: Path, identity: tuple[int, int]) -> None:
    path = _absolute_path(path)
    try:
        directory_fd = _open_directory(path.parent, label="receipt parent")
    except ValueError:
        return
    try:
        _unlink_if_identity_at(directory_fd, path.name, identity)
    finally:
        os.close(directory_fd)


def _write_receipt_atomic(
    path: Path,
    payload: Dict[str, Any],
) -> tuple[int, int]:
    path = _absolute_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_fd = _open_directory(path.parent, label="receipt parent")
    try:
        metadata = _lstat_at(directory_fd, path.name)
        if metadata is not None and stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"receipt target must not be a symlink: {path}")
        if metadata is not None and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"receipt target must be a regular file: {path}")
        encoded = (
            json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        _assert_directory_binding(path.parent, directory_fd, label="receipt parent")
        # Check again immediately before replace so a symlink installed while
        # serializing is rejected rather than silently treated as a receipt.
        metadata = _lstat_at(directory_fd, path.name)
        if metadata is not None and stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"receipt target must not be a symlink: {path}")
        if metadata is not None and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"receipt target must be a regular file: {path}")
        identity = _atomic_replace_at(directory_fd, path.name, encoded)
        try:
            _assert_directory_binding(path.parent, directory_fd, label="receipt parent")
        except ValueError:
            # The receipt is meaningful only while its pathname names the
            # directory we opened. Remove our just-published file from the held
            # directory before failing closed.
            _unlink_if_identity_at(directory_fd, path.name, identity)
            raise
        return identity
    finally:
        os.close(directory_fd)


def proof_digest(proof: str) -> str:
    return hashlib.sha256(proof.encode("utf-8")).hexdigest()


def expected_attestation(
    *,
    proof: str,
    statement: str,
) -> tuple[list[str], str]:
    manifest = parse_blueprint(proof, target_statement=statement)
    return list(manifest.item_ids), aggregate_context_digest(manifest)


def _validate_findings(value: object, name: str) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a list")
    for index, finding in enumerate(value):
        if not isinstance(finding, dict) or set(finding) != _FINDING_FIELDS:
            raise ValueError(
                f"{name}[{index}] must contain exactly location and issue"
            )
        if any(
            not isinstance(finding[field], str) or not finding[field]
            for field in _FINDING_FIELDS
        ):
            raise ValueError(f"{name}[{index}] fields must be non-empty strings")
    return value


def validate_service_response(
    payload: object,
    *,
    expected_proof_digest: str,
    expected_checked_item_ids: list[str],
    expected_context_digest: str,
) -> Dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != _OUTPUT_FIELDS:
        raise ValueError("verification service returned an invalid output shape")

    report = payload["verification_report"]
    if not isinstance(report, dict) or set(report) != _REPORT_FIELDS:
        raise ValueError("verification_report has an invalid output shape")
    if not isinstance(report["summary"], str):
        raise ValueError("verification_report.summary must be a string")
    critical_errors = _validate_findings(
        report["critical_errors"], "verification_report.critical_errors"
    )
    gaps = _validate_findings(report["gaps"], "verification_report.gaps")

    verdict = payload["verdict"]
    repair_hints = payload["repair_hints"]
    if verdict not in {"correct", "wrong"}:
        raise ValueError("verification service returned an unknown verdict")
    if not isinstance(repair_hints, str):
        raise ValueError("repair_hints must be a string")
    has_findings = bool(critical_errors or gaps)
    if verdict == "correct" and (has_findings or repair_hints != ""):
        raise ValueError("correct verdict is inconsistent with findings or hints")
    if verdict == "wrong" and (not has_findings or not repair_hints.strip()):
        raise ValueError("wrong verdict requires findings and repair hints")

    checked_item_ids = payload["checked_item_ids"]
    if (
        not isinstance(checked_item_ids, list)
        or any(
            not isinstance(item_id, str) or _ITEM_ID_RE.fullmatch(item_id) is None
            for item_id in checked_item_ids
        )
        or len(set(checked_item_ids)) != len(checked_item_ids)
    ):
        raise ValueError("checked_item_ids must be a unique list of proof-item ids")
    if checked_item_ids != expected_checked_item_ids:
        raise ValueError("checked_item_ids does not exactly match the blueprint manifest")

    if (
        not isinstance(payload["proof_digest"], str)
        or _HEX_DIGEST_RE.fullmatch(payload["proof_digest"]) is None
        or payload["proof_digest"] != expected_proof_digest
    ):
        raise ValueError("verification service proof_digest does not match the draft")
    if (
        not isinstance(payload["context_digest"], str)
        or _HEX_DIGEST_RE.fullmatch(payload["context_digest"]) is None
        or payload["context_digest"] != expected_context_digest
    ):
        raise ValueError("verification service context_digest does not match the manifest")

    return payload


def verify_blueprint_file(
    *,
    statement: str,
    draft_path: Path,
    verified_path: Path,
    endpoint: str,
    timeout_seconds: int = 3600,
    api_token: str | None = None,
    receipt_path: Path | None = None,
    problem_id: str | None = None,
    blueprint_root: Path | None = None,
) -> Dict[str, Any]:
    """Verify a draft and promote it only if its content is still unchanged."""

    if not isinstance(statement, str) or not statement.strip():
        raise ValueError("statement must be non-empty")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be > 0")
    endpoint = _validate_endpoint(endpoint)
    draft_path = _absolute_path(draft_path)
    verified_path = _absolute_path(verified_path)
    if blueprint_root is not None:
        blueprint_root = _absolute_path(blueprint_root)
    if receipt_path is not None:
        receipt_path = _absolute_path(receipt_path)
    if draft_path == verified_path:
        raise ValueError("draft and verified paths must be different")
    if receipt_path is not None and not problem_id:
        raise ValueError("problem_id is required when writing a receipt")
    if receipt_path in {draft_path, verified_path}:
        raise ValueError("receipt path must be different from blueprint paths")

    # Open both parent directories before reading or making the network call.
    # Production additionally binds them component-by-component beneath a held
    # results-root descriptor, closing containment-check-to-open symlink races.
    blueprint_root_fd = -1
    draft_parent_fd = -1
    verified_parent_fd = -1
    draft_parent_parts: tuple[str, ...] = ()
    verified_parent_parts: tuple[str, ...] = ()

    def assert_blueprint_bindings() -> None:
        if blueprint_root is not None:
            assert blueprint_root_fd >= 0
            _assert_directory_binding(
                blueprint_root,
                blueprint_root_fd,
                label="trusted blueprint root",
            )
            _assert_directory_at_binding(
                blueprint_root_fd,
                draft_parent_parts,
                draft_parent_fd,
                label="blueprint draft parent",
            )
            _assert_directory_at_binding(
                blueprint_root_fd,
                verified_parent_parts,
                verified_parent_fd,
                label="verified blueprint parent",
            )
            return
        _assert_directory_binding(
            draft_path.parent,
            draft_parent_fd,
            label="blueprint draft parent",
        )
        _assert_directory_binding(
            verified_path.parent,
            verified_parent_fd,
            label="verified blueprint parent",
        )

    try:
        if blueprint_root is None:
            verified_path.parent.mkdir(parents=True, exist_ok=True)
            draft_parent_fd = _open_directory(
                draft_path.parent,
                label="blueprint draft parent",
            )
            verified_parent_fd = _open_directory(
                verified_path.parent,
                label="verified blueprint parent",
            )
        else:
            draft_parent_parts = _directory_parts_beneath(
                blueprint_root,
                draft_path.parent,
                label="blueprint draft parent",
            )
            verified_parent_parts = _directory_parts_beneath(
                blueprint_root,
                verified_path.parent,
                label="verified blueprint parent",
            )
            blueprint_root_fd = _open_directory(
                blueprint_root,
                label="trusted blueprint root",
            )
            draft_parent_fd = _open_directory_at(
                blueprint_root_fd,
                draft_parent_parts,
                label="blueprint draft parent",
            )
            verified_parent_fd = _open_directory_at(
                blueprint_root_fd,
                verified_parent_parts,
                label="verified blueprint parent",
            )
        assert_blueprint_bindings()

        proof = _read_regular_blueprint_at(
            draft_parent_fd,
            draft_path.name,
            display_path=draft_path,
            label="blueprint draft",
        )
        if not proof.strip():
            raise ValueError("blueprint draft must be non-empty")
        proof_bytes = proof.encode("utf-8")
        expected_digest = proof_digest(proof)
        expected_ids, expected_context_digest = expected_attestation(
            proof=proof,
            statement=statement,
        )

        request_kwargs: Dict[str, Any] = {
            "json": {"statement": statement, "proof": proof},
            "timeout": timeout_seconds,
        }
        if api_token:
            request_kwargs["headers"] = {"Authorization": f"Bearer {api_token}"}
        response = requests.post(endpoint, **request_kwargs)
        response.raise_for_status()
        try:
            raw_payload = response.json()
        except ValueError as exc:
            raise ValueError("verification service returned non-JSON response") from exc

        payload = validate_service_response(
            raw_payload,
            expected_proof_digest=expected_digest,
            expected_checked_item_ids=expected_ids,
            expected_context_digest=expected_context_digest,
        )
        result = dict(payload)
        result["published"] = False

        if payload["verdict"] != "correct":
            return result

        assert_blueprint_bindings()
        lock_name = f".{verified_path.name}.lock"
        lock_path = verified_path.parent / lock_name
        with _open_lock_file_at(
            verified_parent_fd,
            lock_name,
            display_path=lock_path,
        ) as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                assert_blueprint_bindings()
                current_proof = _read_regular_blueprint_at(
                    draft_parent_fd,
                    draft_path.name,
                    display_path=draft_path,
                    label="blueprint draft",
                )
                if proof_digest(current_proof) != expected_digest:
                    raise ValueError("blueprint draft changed during verification")

                existing_metadata = _lstat_at(
                    verified_parent_fd,
                    verified_path.name,
                )
                if existing_metadata is not None and stat.S_ISREG(
                    existing_metadata.st_mode
                ):
                    existing = _read_regular_blueprint_at(
                        verified_parent_fd,
                        verified_path.name,
                        display_path=verified_path,
                        label="verified blueprint",
                    )
                    if proof_digest(existing) != expected_digest:
                        raise ValueError(
                            "a different verified blueprint already exists; "
                            "refusing to overwrite it"
                        )
                elif existing_metadata is not None and not stat.S_ISLNK(
                    existing_metadata.st_mode
                ):
                    raise ValueError(
                        "verified blueprint target must be a regular file or symlink"
                    )

                assert_blueprint_bindings()

                # Always replace the target with the captured, verified bytes.
                # A same-content symlink becomes a stable regular file, and the
                # dirfd prevents a parent-path swap from redirecting the write.
                published_identity = _atomic_replace_at(
                    verified_parent_fd,
                    verified_path.name,
                    proof_bytes,
                )
                try:
                    assert_blueprint_bindings()
                except ValueError:
                    _unlink_if_identity_at(
                        verified_parent_fd,
                        verified_path.name,
                        published_identity,
                    )
                    raise

                if receipt_path is not None:
                    receipt = {
                        "schema_version": "rethlas-publication-v1",
                        "problem_id": problem_id,
                        "statement_digest": proof_digest(statement),
                        "proof_digest": expected_digest,
                        "context_digest": expected_context_digest,
                        "checked_item_ids": expected_ids,
                        "verified_path": str(verified_path),
                        "published_bytes": len(proof_bytes),
                    }
                    try:
                        receipt_identity = _write_receipt_atomic(receipt_path, receipt)
                    except Exception:
                        _unlink_if_identity_at(
                            verified_parent_fd,
                            verified_path.name,
                            published_identity,
                        )
                        raise
                    try:
                        assert_blueprint_bindings()
                    except ValueError:
                        _unlink_path_if_identity(receipt_path, receipt_identity)
                        _unlink_if_identity_at(
                            verified_parent_fd,
                            verified_path.name,
                            published_identity,
                        )
                        raise
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

        result["published"] = True
        result["published_path"] = str(verified_path)
        if receipt_path is not None:
            result["publication_receipt_path"] = str(receipt_path)
        return result
    finally:
        if verified_parent_fd >= 0:
            os.close(verified_parent_fd)
        if draft_parent_fd >= 0:
            os.close(draft_parent_fd)
        if blueprint_root_fd >= 0:
            os.close(blueprint_root_fd)


__all__ = [
    "expected_attestation",
    "proof_digest",
    "validate_service_response",
    "verify_blueprint_file",
]
