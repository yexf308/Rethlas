#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROBLEM_FILE="${PROBLEM_FILE:-data/example.md}"
MODEL="${MODEL:-gpt-5.6-sol}"
REASONING_EFFORT="${REASONING_EFFORT:-max}"
MAX_ITERATIONS="${MAX_ITERATIONS:-10}"
TIMER_INTERVAL_SECONDS="${TIMER_INTERVAL_SECONDS:-30}"

# The generation runtime is content-attested below. Never create interpreter
# caches in that trusted tree: bytecode is executable input, not a disposable
# artifact, and therefore cannot be excluded safely from the trust decision.
export PYTHONDONTWRITEBYTECODE=1

REQUIRED_GENERATION_MODULES=(
  fastmcp
  requests
  numpy
  scipy
  sympy
  mpmath
  gmpy2
)

# Resolve and constrain the interpreter with shell built-ins/tools before using
# Python for any trust decision. In particular, never execute a model-writable
# virtual environment and then ask that same interpreter whether it is safe.
python_command="$(command -v python3 || true)"
if [[ "$python_command" != /* ]] || [[ ! -x "$python_command" ]]; then
  echo "python3 must resolve to an absolute executable path." >&2
  exit 1
fi
if ! command -v realpath >/dev/null 2>&1; then
  echo "realpath is required to validate the generation Python environment." >&2
  exit 1
fi
TRUSTED_PYTHON_BIN="$(cd "$(dirname "$python_command")" && pwd -P)/$(basename "$python_command")"
python_target="$(realpath "$TRUSTED_PYTHON_BIN")"
temporary_root="$(cd "${TMPDIR:-/tmp}" && pwd -P)"
for candidate in "$TRUSTED_PYTHON_BIN" "$python_target"; do
  if [[ "$candidate" == "$ROOT_DIR" || "$candidate" == "$ROOT_DIR"/* \
     || "$candidate" == "$temporary_root" || "$candidate" == "$temporary_root"/* ]]; then
    echo "Python environment must be outside the generation workspace and temporary directory: $candidate" >&2
    exit 1
  fi
done

# Process .pth files before starting Python with site initialization enabled.
# Executable .pth lines run before the in-process preflight can inspect
# sys.path/spec origins, so a PEP 660/editable hook could otherwise execute
# model-writable code first. Use -S here to keep this scan ahead of all site
# hooks, and require an isolated environment rather than system-site fallback.
if ! "$TRUSTED_PYTHON_BIN" -I -S -B - \
  "$ROOT_DIR" "$temporary_root" "$TRUSTED_PYTHON_BIN" <<'PY'
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
temporary_root = Path(sys.argv[2]).resolve(strict=True)
expected_executable = Path(sys.argv[3]).absolute()
executable = Path(sys.executable).absolute()


def fail(message: str) -> None:
    print(f"generation math-research runtime .pth preflight failed: {message}", file=sys.stderr)
    raise SystemExit(2)


def resolved_outside_writable(value: object, label: str) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        fail(f"{label} is not a filesystem path: {value!r}: {exc}")
    candidate = Path(os.fsdecode(raw))
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        fail(f"cannot resolve {label} {candidate}: {exc}")
    for boundary_label, boundary in (
        ("generation workspace", root),
        ("temporary directory", temporary_root),
    ):
        if resolved == boundary or resolved.is_relative_to(boundary):
            fail(f"{label} resolves inside the model-writable {boundary_label}: {resolved}")
    return resolved


if executable != expected_executable:
    fail(f"Python executable changed during .pth validation: {executable}")
scripts_dir = resolved_outside_writable(expected_executable.parent, "Python bin directory")
environment_root = resolved_outside_writable(scripts_dir.parent, "Python environment")
venv_config = environment_root / "pyvenv.cfg"
if venv_config.exists() or venv_config.is_symlink():
    metadata = venv_config.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        fail(f"pyvenv.cfg must be a regular non-symlink file: {venv_config}")
    if metadata.st_size > 65536:
        fail(f"pyvenv.cfg is unexpectedly large: {venv_config}")
    try:
        config_text = venv_config.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        fail(f"cannot read pyvenv.cfg: {exc}")
    for line in config_text.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip().casefold() == "include-system-site-packages":
            if value.strip().casefold() == "true":
                fail("include-system-site-packages must be false")

site_directories: set[Path] = set()
for library_name in ("lib", "lib64"):
    library_root = environment_root / library_name
    if not library_root.is_dir():
        continue
    for version_directory in library_root.glob("python*"):
        candidate = version_directory / "site-packages"
        if candidate.is_dir() or candidate.is_symlink():
            site_directories.add(
                resolved_outside_writable(candidate, "Python site-packages directory")
            )
windows_site = environment_root / "Lib" / "site-packages"
if windows_site.is_dir() or windows_site.is_symlink():
    site_directories.add(
        resolved_outside_writable(windows_site, "Python site-packages directory")
    )

for site_directory in sorted(site_directories):
    try:
        pth_files = sorted(site_directory.glob("*.pth"))
    except OSError as exc:
        fail(f"cannot enumerate .pth files in {site_directory}: {exc}")
    for pth_file in pth_files:
        try:
            metadata = pth_file.lstat()
        except OSError as exc:
            fail(f"cannot inspect .pth file {pth_file}: {exc}")
        if not stat.S_ISREG(metadata.st_mode):
            fail(f".pth entry must be a regular non-symlink file: {pth_file}")
        if metadata.st_size > 1_000_000:
            fail(f".pth file exceeds 1 MB: {pth_file}")
        try:
            text = pth_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            fail(f"cannot read .pth file {pth_file}: {exc}")
        for line_number, line in enumerate(text.splitlines(), start=1):
            processed = line.rstrip()
            stripped = processed.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith(("import ", "import\t")):
                fail(f"executable .pth line is forbidden: {pth_file}:{line_number}")
            resolved_outside_writable(
                site_directory / processed,
                f".pth path entry {pth_file}:{line_number}",
            )
PY
then
  echo "Use a wheel-installed, isolated generation environment without executable .pth hooks." >&2
  exit 1
fi

# This is the interpreter used both by the immutable MCP snapshot and by the
# model's local math shell. Validate it, then import every declared runtime
# module before creating run state, taking a snapshot, or invoking Codex.
if ! "$TRUSTED_PYTHON_BIN" -I -B - \
  "$ROOT_DIR" "$TRUSTED_PYTHON_BIN" "${REQUIRED_GENERATION_MODULES[@]}" <<'PY'
import importlib
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
expected_executable = Path(sys.argv[2]).absolute()
module_names = sys.argv[3:]
executable = Path(sys.executable).absolute()
prefix = Path(sys.prefix).resolve(strict=True)
temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)


def fail(message: str) -> None:
    print(f"generation math-research runtime preflight failed: {message}", file=sys.stderr)
    raise SystemExit(2)


class UnsafeRuntimePath(RuntimeError):
    pass


def audit_filesystem_path(value: object, label: str) -> None:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise UnsafeRuntimePath(f"{label} is not a filesystem path: {value!r}") from exc
    if not isinstance(raw, str):
        raw = os.fsdecode(raw)
    candidate = Path.cwd() if raw == "" else Path(raw)
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise UnsafeRuntimePath(f"cannot resolve {label} {candidate}: {exc}") from exc
    for boundary_label, boundary in (
        ("generation workspace", root),
        ("temporary directory", temporary_root),
    ):
        if resolved == boundary or resolved.is_relative_to(boundary):
            raise UnsafeRuntimePath(
                f"{label} resolves inside the model-writable {boundary_label}: {resolved}"
            )


def audit_path_collection(values: object, label: str) -> list[object]:
    try:
        entries = list(values)  # type: ignore[arg-type]
    except BaseException as exc:
        raise UnsafeRuntimePath(f"cannot inspect {label}: {type(exc).__name__}: {exc}") from exc
    for index, entry in enumerate(entries):
        audit_filesystem_path(entry, f"{label}[{index}]")
    return entries


def audit_spec(spec: object, label: str) -> None:
    origin = getattr(spec, "origin", None)
    locations = getattr(spec, "submodule_search_locations", None)
    if origin not in (None, "built-in", "frozen"):
        audit_filesystem_path(origin, f"{label} spec.origin")
    if locations is not None:
        entries = audit_path_collection(locations, f"{label} spec search path")
        if origin is None and not entries:
            raise UnsafeRuntimePath(f"{label} namespace package has no search locations")
    elif origin is None:
        raise UnsafeRuntimePath(f"{label} has neither an origin nor package search locations")


def audit_sys_path(stage: str) -> None:
    for index, entry in enumerate(sys.path):
        audit_filesystem_path(entry, f"sys.path[{index}] during {stage}")


def audit_loaded_module_tree(module_name: str) -> None:
    prefix = module_name + "."
    for loaded_name, loaded_module in list(sys.modules.items()):
        if loaded_module is None or not (
            loaded_name == module_name or loaded_name.startswith(prefix)
        ):
            continue
        spec = getattr(loaded_module, "__spec__", None)
        if spec is not None:
            audit_spec(spec, f"loaded module {loaded_name}")
        for attribute in ("__file__", "__cached__"):
            value = getattr(loaded_module, attribute, None)
            if value is not None:
                audit_filesystem_path(value, f"loaded module {loaded_name}.{attribute}")
        package_path = getattr(loaded_module, "__path__", None)
        if package_path is not None:
            audit_path_collection(package_path, f"loaded module {loaded_name}.__path__")


if executable != expected_executable:
    fail(f"Python executable changed during validation: {executable}")
if prefix.is_relative_to(root) or prefix.is_relative_to(temporary_root):
    fail(
        "Python environment must be outside the generation workspace and "
        f"temporary directory: {prefix}"
    )
try:
    audit_sys_path("initial runtime validation")
except UnsafeRuntimePath as exc:
    fail(str(exc))

scripts_dir = expected_executable.parent.resolve(strict=True)
if os.pathsep in str(scripts_dir) or "\n" in str(scripts_dir):
    fail(f"Python bin directory cannot be represented safely in PATH: {scripts_dir}")
expected_target = expected_executable.resolve(strict=True)
for command_name in ("python", "python3"):
    candidate = scripts_dir / command_name
    try:
        candidate_target = candidate.resolve(strict=True)
    except OSError as exc:
        fail(f"{command_name} is missing from the trusted Python bin directory: {exc}")
    if not os.access(candidate, os.X_OK) or candidate_target != expected_target:
        fail(f"{candidate} does not resolve to the selected interpreter")

errors: list[str] = []
for module_name in module_names:
    try:
        spec = importlib.util.find_spec(module_name)
    except BaseException as exc:  # fail closed for broken package metadata/hooks
        errors.append(f"{module_name}: find_spec raised {type(exc).__name__}: {exc}")
        continue
    if spec is None:
        errors.append(f"{module_name}: module not found")
        continue
    try:
        audit_spec(spec, f"required module {module_name}")
    except BaseException as exc:
        errors.append(f"{module_name}: unsafe module spec: {type(exc).__name__}: {exc}")
        continue
    try:
        importlib.import_module(module_name)
    except BaseException as exc:  # an installed package can still be unusable
        errors.append(f"{module_name}: import raised {type(exc).__name__}: {exc}")
        continue
    try:
        audit_sys_path(f"import of {module_name}")
        audit_loaded_module_tree(module_name)
    except BaseException as exc:
        errors.append(f"{module_name}: unsafe imported path: {type(exc).__name__}: {exc}")

if errors:
    fail("; ".join(errors))
PY
then
  echo "Install agents/generation/requirements-math-research.txt into the selected external Python environment." >&2
  exit 1
fi
trusted_python_command="$TRUSTED_PYTHON_BIN"
trusted_python_dir="$(cd "$(dirname "$trusted_python_command")" && pwd -P)"
SAFE_SHELL_PATH="$trusted_python_dir:/usr/bin:/bin:/usr/sbin:/sbin"
TRUSTED_SHELL_ENVIRONMENT_POLICY_TOML="$(
  "$TRUSTED_PYTHON_BIN" -I -B -c \
    'import json, sys; print("{inherit=\"none\",set={PATH=" + json.dumps(sys.argv[1]) + "}}")' \
    "$SAFE_SHELL_PATH"
)"

if [[ "$PROBLEM_FILE" = /* ]]; then
  echo "PROBLEM_FILE must be relative to agents/generation: $PROBLEM_FILE" >&2
  exit 1
fi

if [[ "$PROBLEM_FILE" == ".." || "$PROBLEM_FILE" == ../* || "$PROBLEM_FILE" == */.. || "$PROBLEM_FILE" == */../* ]]; then
  echo "PROBLEM_FILE must not contain '..': $PROBLEM_FILE" >&2
  exit 1
fi

if [[ "$PROBLEM_FILE" != data/*.md ]]; then
  echo "PROBLEM_FILE must point to a markdown file under data/: $PROBLEM_FILE" >&2
  exit 1
fi

if [[ ! -f "$ROOT_DIR/$PROBLEM_FILE" ]]; then
  echo "Problem file not found: $ROOT_DIR/$PROBLEM_FILE" >&2
  exit 1
fi

if ! [[ "$MAX_ITERATIONS" =~ ^[0-9]+$ ]] || [[ "$MAX_ITERATIONS" -le 0 ]]; then
  echo "MAX_ITERATIONS must be a positive integer: $MAX_ITERATIONS" >&2
  exit 1
fi

# data/algebra/prob1.md -> algebra/prob1
problem_rel="${PROBLEM_FILE#data/}"
problem_rel="${problem_rel%.md}"
problem_name="$(basename "$PROBLEM_FILE" .md)"
ref_dir="data/${problem_rel}.refs"
ref_prompt="Use reference_dir=${ref_dir} if it exists."

# The publication tool intentionally uses a lossless, restricted identifier.
# Reject names that its path validator would otherwise normalize differently.
IFS='/' read -r -a problem_parts <<< "$problem_rel"
for component in "${problem_parts[@]}"; do
  if ! [[ "$component" =~ ^[A-Za-z0-9]([A-Za-z0-9._-]*[A-Za-z0-9-])?$ ]]; then
    echo "Unsupported problem path component '$component'. Use ASCII letters, digits, '.', '_', or '-'; do not use leading/trailing './_'." >&2
    exit 1
  fi
done

prepare_references() {
  local abs_ref_dir="$ROOT_DIR/$ref_dir"
  if [[ ! -d "$abs_ref_dir" ]]; then
    return
  fi

  local pdf_count=0
  while IFS= read -r -d '' pdf; do
    pdf_count=$((pdf_count + 1))
    if ! command -v pdftotext >/dev/null 2>&1; then
      echo "WARNING: found PDF references, but pdftotext is not installed; PDFs will be ignored." >&2
      return
    fi

    local rel_pdf="${pdf#"$abs_ref_dir"/}"
    local txt="$abs_ref_dir/.extracted/${rel_pdf%.pdf}.txt"
    mkdir -p "$(dirname "$txt")"
    if [[ ! -f "$txt" || "$pdf" -nt "$txt" ]]; then
      pdftotext -layout "$pdf" "$txt"
    fi
  done < <(find "$abs_ref_dir" -type f -iname '*.pdf' -not -path "$abs_ref_dir/.extracted/*" -print0)

  if [[ $pdf_count -gt 0 ]]; then
    ref_prompt="Use reference_dir=${ref_dir} if it exists. PDF references have been extracted to ${ref_dir}/.extracted; read those extracted .txt files instead of the PDFs."
  fi
}

format_duration() {
  local total="$1"
  printf "%02d:%02d:%02d" \
    $((total / 3600)) $(((total % 3600) / 60)) $((total % 60))
}

LOG_DIR="${LOG_DIR:-$ROOT_DIR/logs/$problem_rel/iter}"
verified_path="$ROOT_DIR/results/$problem_rel/blueprint_verified.md"
trusted_receipts_root="$(cd "$ROOT_DIR/.." && pwd -P)/.verification_receipts"
if [[ -n "${RETHLAS_RECEIPTS_ROOT:-}" && "$RETHLAS_RECEIPTS_ROOT" != "$trusted_receipts_root" ]]; then
  echo "RETHLAS_RECEIPTS_ROOT is fixed outside the generation workspace and cannot be overridden." >&2
  exit 1
fi
if [[ -L "$trusted_receipts_root" ]]; then
  echo "Trusted receipt root must not be a symlink: $trusted_receipts_root" >&2
  exit 1
fi
mkdir -p "$trusted_receipts_root"
export RETHLAS_RECEIPTS_ROOT="$trusted_receipts_root"
export RETHLAS_EXPECTED_PROBLEM_ID="$problem_rel"
export RETHLAS_EXPECTED_STATEMENT_SHA256="$(
  "$TRUSTED_PYTHON_BIN" -B -c 'import hashlib, pathlib, sys; print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())' \
    "$ROOT_DIR/$PROBLEM_FILE"
)"
receipt_path="$RETHLAS_RECEIPTS_ROOT/$problem_rel.json"
mkdir -p "$LOG_DIR"

trusted_runtime_manifest() {
  local manifest_root="${1:-$ROOT_DIR}"
  "$TRUSTED_PYTHON_BIN" -B - "$manifest_root" <<'PY'
import hashlib
import os
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve(strict=True)
explicit = [
    root / "AGENTS.md",
    root / "requirements-math-research.txt",
    root / "tests" / "run_example.sh",
]
trees = [root / ".codex", root / ".agents", root / "mcp"]

def fail(message: str) -> None:
    print(f"unsafe trusted generation runtime: {message}", file=sys.stderr)
    raise SystemExit(2)


entries: list[tuple[str, Path, os.stat_result]] = []
for path in explicit:
    try:
        metadata = path.lstat()
    except OSError as exc:
        fail(f"cannot inspect {path}: {exc}")
    if not stat.S_ISREG(metadata.st_mode):
        fail(f"expected a regular file: {path}")
    entries.append(("file", path, metadata))

for tree in trees:
    try:
        tree_metadata = tree.lstat()
    except OSError as exc:
        fail(f"cannot inspect {tree}: {exc}")
    if not stat.S_ISDIR(tree_metadata.st_mode):
        fail(f"expected a non-symlink directory: {tree}")
    entries.append(("directory", tree, tree_metadata))

    for current, directories, names in os.walk(tree, followlinks=False):
        current_path = Path(current)
        directories.sort()
        names.sort()
        for name in list(directories):
            candidate = current_path / name
            if name == "__pycache__":
                fail(f"Python bytecode cache directory is forbidden: {candidate}")
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                fail(f"cannot inspect {candidate}: {exc}")
            if not stat.S_ISDIR(metadata.st_mode):
                fail(f"directory entry is a symlink or special file: {candidate}")
            entries.append(("directory", candidate, metadata))

        for name in names:
            candidate = current_path / name
            if name.endswith((".pyc", ".pyo")):
                fail(f"Python bytecode is forbidden: {candidate}")
            try:
                metadata = candidate.lstat()
            except OSError as exc:
                fail(f"cannot inspect {candidate}: {exc}")
            if not stat.S_ISREG(metadata.st_mode):
                fail(f"file entry is a symlink or special file: {candidate}")
            entries.append(("file", candidate, metadata))

if len(entries) > 2000:
    fail("trusted runtime has more than 2000 filesystem entries")
total = 0
manifest = hashlib.sha256()
seen: set[Path] = set()
for kind, path, metadata in sorted(
    entries,
    key=lambda item: (str(item[1].relative_to(root)), item[0]),
):
    if path in seen:
        fail(f"duplicate runtime entry: {path}")
    seen.add(path)
    relative = str(path.relative_to(root)).encode("utf-8")
    kind_bytes = kind.encode("ascii")
    manifest.update(len(kind_bytes).to_bytes(1, "big"))
    manifest.update(kind_bytes)
    manifest.update(len(relative).to_bytes(4, "big"))
    manifest.update(relative)
    manifest.update(stat.S_IMODE(metadata.st_mode).to_bytes(4, "big"))
    if kind == "file":
        if metadata.st_size > 8_000_000:
            fail(f"trusted runtime file exceeds 8 MB: {path}")
        total += metadata.st_size
        if total > 32_000_000:
            fail("trusted runtime files exceed 32 MB in total")
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(65536):
                digest.update(chunk)
        manifest.update(digest.digest())
print(manifest.hexdigest())
PY
}

TRUSTED_RUNTIME_MANIFEST="$(trusted_runtime_manifest)" || {
  echo "Could not establish the trusted generation runtime manifest." >&2
  exit 1
}

# Codex can restart a failed MCP server within one session. A before/after hash
# of the writable source tree alone cannot detect code that was changed,
# executed on restart, and restored. Pin every MCP start/restart to an exact
# snapshot outside the generation workspace instead.
trusted_runtime_parent="$ROOT_DIR/../.trusted_generation_runtime"
if [[ -L "$trusted_runtime_parent" ]]; then
  echo "Trusted runtime snapshot root must not be a symlink: $trusted_runtime_parent" >&2
  exit 1
fi
mkdir -p "$trusted_runtime_parent"
trusted_runtime_parent="$(cd "$trusted_runtime_parent" && pwd -P)"
trusted_runtime_dir="$(mktemp -d "$trusted_runtime_parent/runtime.XXXXXX")"
mkdir -p "$trusted_runtime_dir/tests"
cp -p "$ROOT_DIR/AGENTS.md" "$trusted_runtime_dir/AGENTS.md"
cp -p "$ROOT_DIR/requirements-math-research.txt" \
  "$trusted_runtime_dir/requirements-math-research.txt"
cp -p "$ROOT_DIR/tests/run_example.sh" "$trusted_runtime_dir/tests/run_example.sh"
cp -pR "$ROOT_DIR/.codex" "$trusted_runtime_dir/.codex"
cp -pR "$ROOT_DIR/.agents" "$trusted_runtime_dir/.agents"
cp -pR "$ROOT_DIR/mcp" "$trusted_runtime_dir/mcp"
SNAPSHOT_RUNTIME_MANIFEST="$(trusted_runtime_manifest "$trusted_runtime_dir")" || {
  echo "Could not attest the trusted generation runtime snapshot." >&2
  exit 1
}
if [[ "$SNAPSHOT_RUNTIME_MANIFEST" != "$TRUSTED_RUNTIME_MANIFEST" ]]; then
  echo "Trusted generation runtime changed while its snapshot was created." >&2
  exit 70
fi
chmod -R a-w "$trusted_runtime_dir"
export RETHLAS_GENERATION_ROOT="$ROOT_DIR"
TRUSTED_PYTHON_COMMAND_TOML="$(
  "$TRUSTED_PYTHON_BIN" -B -c 'import json, sys; print(json.dumps(sys.argv[1]))' \
    "$trusted_python_command"
)"
TRUSTED_MCP_ARGS_TOML="$(
  "$TRUSTED_PYTHON_BIN" -B -c 'import json, sys; print(json.dumps(["-B", sys.argv[1]]))' \
    "$trusted_runtime_dir/mcp/server.py"
)"
TRUSTED_MCP_CWD_TOML="$(
  "$TRUSTED_PYTHON_BIN" -B -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$ROOT_DIR"
)"

trusted_runtime_unchanged() {
  local current_manifest
  current_manifest="$(trusted_runtime_manifest)" || return 1
  [[ "$current_manifest" == "$TRUSTED_RUNTIME_MANIFEST" ]]
}

receipt_is_valid() {
  "$TRUSTED_PYTHON_BIN" -B - "$ROOT_DIR" "$receipt_path" "$verified_path" "$problem_rel" \
    "$RETHLAS_EXPECTED_STATEMENT_SHA256" "$ROOT_DIR/$PROBLEM_FILE" <<'PY'
import hashlib
import json
import os
import re
import stat
import sys
from pathlib import Path

root = Path(sys.argv[1]).absolute()
receipt_path = Path(sys.argv[2]).absolute()
verified_path = Path(sys.argv[3]).absolute()
problem_id = sys.argv[4]
statement_digest = sys.argv[5]
problem_path = Path(sys.argv[6]).absolute()
receipt_root = root.parent / ".verification_receipts"
results_root = root / "results"
max_receipt_bytes = 65536
max_blueprint_bytes = 8_000_000

def open_parent(root_path: Path, parts: list[str]) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(root_path, flags)
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("unsafe root")
        for part in parts:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
            if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise ValueError("unsafe parent")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise

def bounded_regular_bytes(parent: Path, relative_parent: list[str], name: str, limit: int) -> bytes:
    parent_fd = open_parent(parent, relative_parent)
    descriptor = -1
    try:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise ValueError("unsafe or oversized file")
        chunks = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise ValueError("oversized file")
        return raw
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)

try:
    components = problem_id.split("/")
    if not components or any(
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9-])?", part) is None
        for part in components
    ):
        raise ValueError("unsafe problem id")
    if receipt_path != receipt_root.joinpath(*components[:-1], components[-1] + ".json"):
        raise ValueError("wrong receipt path")
    if verified_path != results_root.joinpath(*components, "blueprint_verified.md"):
        raise ValueError("wrong verified path")
    receipt_raw = bounded_regular_bytes(
        receipt_root, components[:-1], components[-1] + ".json", max_receipt_bytes
    )
    verified_raw = bounded_regular_bytes(
        results_root, components, "blueprint_verified.md", max_blueprint_bytes
    )
    problem_raw = problem_path.read_bytes()
    if hashlib.sha256(problem_raw).hexdigest() != statement_digest:
        raise ValueError("problem changed")
    receipt = json.loads(receipt_raw.decode("utf-8"))
    expected_keys = {
        "schema_version", "problem_id", "statement_digest", "proof_digest",
        "context_digest", "adaptive_context_digest", "item_context_attestations",
        "checked_item_ids", "verified_path", "published_bytes",
    }
    if not isinstance(receipt, dict) or set(receipt) != expected_keys:
        raise ValueError("invalid receipt shape")
    if receipt["schema_version"] != "rethlas-publication-v2":
        raise ValueError("invalid receipt version")
    if receipt["problem_id"] != problem_id:
        raise ValueError("wrong problem")
    if receipt["statement_digest"] != statement_digest:
        raise ValueError("stale statement")
    if receipt["proof_digest"] != hashlib.sha256(verified_raw).hexdigest():
        raise ValueError("verified bytes changed")
    if isinstance(receipt["published_bytes"], bool) or receipt["published_bytes"] != len(verified_raw):
        raise ValueError("verified byte count changed")
    if receipt["verified_path"] != str(verified_path):
        raise ValueError("wrong verified path")
    ids = receipt["checked_item_ids"]
    if not isinstance(ids, list) or not ids or any(
        not isinstance(item_id, str)
        or re.fullmatch(r"pi_[0-9a-f]{24}", item_id) is None
        for item_id in ids
    ):
        raise ValueError("invalid item coverage")
    if re.fullmatch(r"[0-9a-f]{64}", receipt["context_digest"]) is None:
        raise ValueError("invalid context digest")
    sys.path.insert(0, str(root / "mcp"))
    from proof_context import (
        aggregate_adaptive_context_digest,
        aggregate_context_digest,
        build_item_context,
        parse_blueprint,
    )
    proof_text = verified_raw.decode("utf-8")
    statement_text = problem_raw.decode("utf-8")
    manifest = parse_blueprint(proof_text, target_statement=statement_text)
    if ids != list(manifest.item_ids):
        raise ValueError("receipt item coverage does not match verified blueprint")
    if receipt["context_digest"] != aggregate_context_digest(manifest):
        raise ValueError("receipt context digest does not match verified blueprint")
    attestations = receipt["item_context_attestations"]
    if not isinstance(attestations, list) or len(attestations) != len(ids):
        raise ValueError("invalid adaptive context coverage")
    fields = {
        "item_id", "disposition", "final_round", "expanded_proof_ids",
        "max_chars", "context_digest", "verdict",
    }
    for item_id, record in zip(ids, attestations, strict=True):
        if not isinstance(record, dict) or set(record) != fields:
            raise ValueError("invalid item context attestation shape")
        if record["item_id"] != item_id:
            raise ValueError("item context attestation order mismatch")
        rebuilt = build_item_context(
            manifest,
            item_id,
            max_chars=record["max_chars"],
            expanded_proof_ids=record["expanded_proof_ids"],
            round_index=record["final_round"],
        )
        if not rebuilt["complete"] or rebuilt["digest"] != record["context_digest"]:
            raise ValueError("item context attestation mismatch")
    if receipt["adaptive_context_digest"] != aggregate_adaptive_context_digest(
        manifest, attestations
    ):
        raise ValueError("adaptive context digest mismatch")
except (OSError, ValueError, TypeError, UnicodeError, json.JSONDecodeError, ImportError) as exc:
    if os.environ.get("RETHLAS_RECEIPT_DEBUG") == "1":
        print(f"invalid publication receipt: {exc}", file=sys.stderr)
    raise SystemExit(1)
PY
}

if [[ -e "$verified_path" || -L "$verified_path" ]]; then
  if receipt_is_valid; then
    echo "Existing verified blueprint has a valid publication receipt."
  else
    echo "Ignoring untrusted/stale verified blueprint at $verified_path"
  fi
fi

prepare_references

CODEX_VERSION="$(codex --version 2>/dev/null || echo 'unknown')"

echo "========================================"
echo " Codex:      $CODEX_VERSION"
echo " Model:      $MODEL"
echo " Effort:     $REASONING_EFFORT"
echo " Problem:    $PROBLEM_FILE"
echo " Problem ID: $problem_rel"
echo " References: $ref_dir"
echo " Math Python: $trusted_python_command"
echo " Max iters:  $MAX_ITERATIONS"
echo " Logs:       $LOG_DIR"
echo " Stop file:  $verified_path"
echo "========================================"
echo ""

VERIFY_HEALTH_URL="${VERIFY_HEALTH_URL:-${VERIFY_URL:-http://127.0.0.1:8091/health}}"
verify_base_url="${VERIFY_HEALTH_URL%/health}"
export VERIFY_PROOF_URL="${VERIFY_PROOF_URL:-${verify_base_url%/}/verify}"
if ! "$TRUSTED_PYTHON_BIN" -B - "$VERIFY_PROOF_URL" <<'PY'
import sys
from urllib.parse import urlsplit
url = urlsplit(sys.argv[1])
if url.scheme == "https":
    raise SystemExit(0)
if url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"}:
    raise SystemExit(0)
raise SystemExit(1)
PY
then
  echo "VERIFY_PROOF_URL must use HTTPS unless it targets loopback: $VERIFY_PROOF_URL" >&2
  exit 1
fi
if ! curl -sf "$VERIFY_HEALTH_URL" >/dev/null 2>&1; then
  echo "WARNING: verification service not reachable at ${VERIFY_HEALTH_URL}"
  echo "         The agent may be unable to produce blueprint_verified.md."
  echo "         Start it first if you need verified proofs."
  echo ""
fi
TRUSTED_MCP_ENV_TOML="$("$TRUSTED_PYTHON_BIN" -B - <<'PY'
import json
import os

names = (
    "PYTHONDONTWRITEBYTECODE",
    "RETHLAS_EXPECTED_PROBLEM_ID",
    "RETHLAS_EXPECTED_STATEMENT_SHA256",
    "RETHLAS_GENERATION_ROOT",
    "RETHLAS_RECEIPTS_ROOT",
    "VERIFY_API_TOKEN",
    "VERIFY_PROOF_URL",
)
entries = [
    f"{json.dumps(name)} = {json.dumps(os.environ[name])}"
    for name in names
    if name in os.environ
]
print("{" + ", ".join(entries) + "}")
PY
)"
TRUSTED_REASONING_AGENT_MCP_TOML="{command=$TRUSTED_PYTHON_COMMAND_TOML,args=$TRUSTED_MCP_ARGS_TOML,cwd=$TRUSTED_MCP_CWD_TOML,env=$TRUSTED_MCP_ENV_TOML,tool_timeout_sec=3600,default_tools_approval_mode=\"approve\"}"

START_EPOCH=$(date +%s)

elapsed_timer() {
  while true; do
    sleep "$TIMER_INTERVAL_SECONDS"
    local now
    now=$(date +%s)
    local secs=$((now - START_EPOCH))
    printf "\r  [elapsed %s] still running..." "$(format_duration "$secs")"
  done
}

elapsed_timer &
TIMER_PID=$!

cleanup_timer() {
  kill "$TIMER_PID" 2>/dev/null || true
  wait "$TIMER_PID" 2>/dev/null || true
}
trap cleanup_timer EXIT

for ((iter = 0; iter < MAX_ITERATIONS; iter += 1)); do
  log_file="$LOG_DIR/${problem_name}_iter_${iter}.md"

  if ! trusted_runtime_unchanged; then
    echo "Trusted generation runtime changed; refusing to start another session." >&2
    exit 70
  fi

  if receipt_is_valid; then
    echo "Solved problem_id=$problem_rel before iter=$iter"
    break
  fi

  echo "Starting iter=$iter -> $log_file"

  if [[ "$iter" -eq 0 ]]; then
    prompt="Use AGENTS.md exactly to solve the math problem in ${PROBLEM_FILE}. Use problem_id=${problem_rel}. ${ref_prompt} A trusted math-research runtime is available as both python and python3, with NumPy, SciPy, SymPy, mpmath, and gmpy2 importable. This is iteration 0 in a fresh session. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run."
    web_mode="live"
  elif ((iter % 2 == 1)); then
    prompt="Start a fresh reasoning session and continue problem_id=${problem_rel}. Read AGENTS.md, ${PROBLEM_FILE}, the current results/${problem_rel}/blueprint.md if it exists, and retrieve only relevant persisted memory through memory_search. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run. This is iteration ${iter}. Do not use arXiv theorem search or web search; think deeply from the persisted artifacts."
    web_mode="disabled"
  else
    prompt="Start a fresh reasoning session and continue problem_id=${problem_rel}. Read AGENTS.md, ${PROBLEM_FILE}, the current results/${problem_rel}/blueprint.md if it exists, and retrieve only relevant persisted memory through memory_search. Ignore any pre-existing blueprint_verified.md: only verify_blueprint_service and its trusted receipt can finish this run. This is iteration ${iter}. You may use arXiv theorem search and web search, but also reason deeply from the persisted artifacts."
    web_mode="live"
  fi

  if (
    cd "$ROOT_DIR"
    codex exec \
      -C "$ROOT_DIR" \
      -m "$MODEL" \
      --config "model_reasoning_effort=\"$REASONING_EFFORT\"" \
      --config "web_search=\"$web_mode\"" \
      --config "shell_environment_policy=$TRUSTED_SHELL_ENVIRONMENT_POLICY_TOML" \
      --config "mcp_servers.reasoning_agent=$TRUSTED_REASONING_AGENT_MCP_TOML" \
      --sandbox workspace-write \
      --ephemeral \
      "$prompt"
  ) >"$log_file" 2>&1; then
    codex_rc=0
  else
    codex_rc=$?
  fi

  if [[ "$codex_rc" -ne 0 ]]; then
    echo "codex exited with code $codex_rc at iter=$iter (see $log_file for details)" >&2
    exit "$codex_rc"
  fi

  if ! trusted_runtime_unchanged; then
    echo "Trusted generation runtime was modified during iter=$iter; refusing to continue or accept publication." >&2
    exit 70
  fi

  echo "Finished problem_id=$problem_rel iter=$iter -> $log_file"
done

cleanup_timer
trap - EXIT

END_EPOCH=$(date +%s)
TOTAL=$((END_EPOCH - START_EPOCH))
printf "\n"

if receipt_is_valid; then
  echo "Solved problem_id=$problem_rel -> $verified_path"
  printf "Total time: %s\n" "$(format_duration "$TOTAL")"
  echo ""
  echo "To view results in the browser, run:"
  echo "  ./site/serve.sh"
  echo "Then open http://localhost:3264"
  exit 0
fi

echo "Reached MAX_ITERATIONS=$MAX_ITERATIONS without verified blueprint for problem_id=$problem_rel" >&2
printf "Total time: %s\n" "$(format_duration "$TOTAL")"
exit 1
