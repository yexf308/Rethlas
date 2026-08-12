from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from agents.generation import guardian as guardian_module
from agents.generation import guardian_launcher
from agents.generation.guardian import PaidGroup, SystemProcessInspector

ROOT = Path(__file__).resolve().parents[3]
LAUNCHER = ROOT / "agents" / "generation" / "guardian_launcher.py"
GUARDIAN = ROOT / "agents" / "generation" / "guardian.py"
RUNNER = ROOT / "agents" / "generation" / "tests" / "run_example.sh"
CODEX_BIN = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


_SECURE_LAUNCHER_LOADER = r"""
import hashlib, os, sys
path, expected = sys.argv[1:3]
flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
fd = os.open(path, flags)
metadata = os.fstat(fd)
source = os.pread(fd, metadata.st_size, 0)
if hashlib.sha256(source).hexdigest() != expected:
    raise SystemExit("launcher source digest mismatch")
sys.argv = [path, *sys.argv[3:]]
namespace = {
    "__builtins__": __builtins__, "__file__": path, "__name__": "__main__",
    "__package__": None, "__spec__": None,
    "__rethlas_pinned_launcher_fd__": fd,
    "__rethlas_pinned_launcher_path__": path,
    "__rethlas_pinned_launcher_sha256__": expected,
}
exec(compile(source, path, "exec", dont_inherit=True), namespace, namespace)
"""


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def test_offline_failure_claim_binds_full_set_but_samples_256() -> None:
    groups = [
        {
            "role": "root",
            "identity": {
                "pid": 20_000 + index,
                "uid": os.getuid(),
                "pgid": 20_000 + index,
                "start_marker": f"candidate-{index:03d}",
            },
        }
        for index in range(257)
    ]
    failure = guardian_launcher._bounded_offline_failure(  # noqa: SLF001
        [{"stage": "host_capture", "error_type": "host_or_receipt_failure"}],
        groups,
    )

    assert set(failure) == {
        "schema_version",
        "code",
        "detail_sha256",
        "group_count",
        "groups",
        "groups_complete",
        "groups_sha256",
    }
    assert failure["group_count"] == 257
    assert failure["groups_complete"] is False
    assert failure["groups"] == groups[:256]
    assert failure["groups_sha256"] == _digest(groups)


def test_offline_terminal_receipt_requires_success_proof_flags() -> None:
    seed = {
        "schema_version": "rethlas_guardian_offline_finalize_v1",
        "operation_id": "offline-flags",
        "manifest_sha256": "1" * 64,
        "registration_id": "registration-flags",
        "report_sha256": "2" * 64,
        "state": "watchdog_forced",
        "capture_sealed": True,
        "coverage_complete": False,
        "all_empty_verified": True,
        "terminal_sequence": 1,
    }
    receipt = {**seed, "receipt_sha256": _digest(seed)}

    with pytest.raises(guardian_launcher.LauncherError):
        guardian_launcher._validated_offline_finalize_status(  # noqa: SLF001
            {"offline_finalize": receipt}
        )
    assert not guardian_launcher._status_has_durable_terminal_report(  # noqa: SLF001
        {
            "disposition": "guardian_terminal",
            "terminal_report": None,
            "offline_finalize": None,
        }
    )


def _policy_contract() -> tuple[dict[str, object], str, str]:
    review_material = {
        "policy_id": "rethlas_route_review_90m_v1",
        "clock": "earliest_durable_wall_and_same_boot_monotonic",
        "guardian_enforcement_ready": True,
        "approved_guardian_launcher_sha256": hashlib.sha256(
            LAUNCHER.read_bytes()
        ).hexdigest(),
        "approved_guardian_sha256": hashlib.sha256(GUARDIAN.read_bytes()).hexdigest(),
        "approved_guardian_runner_sha256": hashlib.sha256(
            RUNNER.read_bytes()
        ).hexdigest(),
        "guardian_control_schema_sha256": "7" * 64,
        "guardian_launch_manifest_schema_sha256": (
            guardian_launcher.LAUNCH_MANIFEST_SCHEMA_SHA256
        ),
    }
    review_digest = _digest(review_material)
    context_material = {"policy_id": "rethlas_context_guard_v1"}
    context_digest = _digest(context_material)
    material = {
        "schema_version": "rethlas-policy-contract-v1",
        "review_cadence_policy": {
            **review_material,
            "policy_sha256": review_digest,
        },
        "context_guard_policy": {
            **context_material,
            "policy_sha256": context_digest,
        },
    }
    contract_digest = _digest(material)
    return (
        {**material, "contract_sha256": contract_digest},
        contract_digest,
        review_digest,
    )


_FAKE_ADAPTER = r"""from __future__ import annotations
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time

CONTRACT = __CONTRACT__
PRIVILEGED = {
    "RETHLAS_REVIEW_CONTROL_TOKEN",
    "RETHLAS_GUARDIAN_CYCLE_TOKEN",
    "RETHLAS_RUNNER_CYCLE_TOKEN",
    "RETHLAS_STALE_RECOVERY_TOKEN",
}

def canonical(value):
    return json.dumps(value, allow_nan=False, ensure_ascii=False,
                      separators=(",", ":"), sort_keys=True)

def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()

def read_token(fd):
    if fd <= 2 or not stat.S_ISFIFO(os.fstat(fd).st_mode):
        raise RuntimeError("token fd is not a private FIFO")
    raw = b""
    while len(raw) < 64:
        chunk = os.read(fd, 64 - len(raw))
        if not chunk:
            break
        raw += chunk
    trailing = os.read(fd, 1)
    os.close(fd)
    if len(raw) != 64 or trailing or re.fullmatch(b"[0-9a-f]{64}", raw) is None:
        raise RuntimeError("token pipe is not exact")
    return raw.decode("ascii")

args = sys.argv[1:]
token = None
domain = None
if "--control-token-fd" in args:
    index = args.index("--control-token-fd")
    token = read_token(int(args[index + 1]))
    del args[index:index + 2]
if "--control-token-domain" in args:
    index = args.index("--control-token-domain")
    domain = args[index + 1]
    del args[index:index + 2]
db_index = args.index("--db")
database = pathlib.Path(args[db_index + 1])
del args[db_index:db_index + 2]
command = args[0]
if PRIVILEGED & set(os.environ):
    raise RuntimeError("capability leaked through the adapter environment")
if command == "policy-contract":
    if token is not None or domain is not None:
        raise RuntimeError("public policy unexpectedly received a token")
    print(canonical(CONTRACT))
    raise SystemExit(0)
if command in {"run-generator", "guarded-review-drive"}:
    token_index = args.index("--runner-token-fd")
    runner_token = read_token(int(args[token_index + 1]))
    del args[token_index:token_index + 2]
    state = json.loads(database.read_text(encoding="utf-8"))
    runner_token_sha256 = hashlib.sha256(
        runner_token.encode("ascii")
    ).hexdigest()
    if runner_token_sha256 != state["prepare"]["runner_token_sha256"]:
        raise RuntimeError("runner token differs from prepared fence")
    if command == "run-generator":
        setsid_children = []
        behavior = state.get("runner_setsid_behavior")
        if behavior is not None:
            ready_dir = pathlib.Path(behavior["ready_dir"])
            echo_dir = pathlib.Path(behavior["echo_dir"])
            for ordinal in range(behavior["count"]):
                child_pid = os.fork()
                if child_pid == 0:
                    os.setsid()
                    own_pid = os.getpid()
                    (ready_dir / (str(own_pid) + ".ready")).write_text(
                        str(ordinal), encoding="utf-8"
                    )
                    deadline = time.monotonic() + 20.0
                    echo_path = echo_dir / (str(own_pid) + ".echo")
                    echo_count = 0
                    while echo_count < 2 and time.monotonic() < deadline:
                        try:
                            echo_count = int(echo_path.read_text(encoding="utf-8"))
                        except (FileNotFoundError, ValueError):
                            echo_count = 0
                        time.sleep(0.01)
                    if echo_count >= 2:
                        time.sleep(0.10)
                        os._exit(0)
                    time.sleep(20.0)
                    os._exit(71)
                setsid_children.append(child_pid)
            for child_pid in setsid_children:
                os.waitpid(child_pid, 0)
            time.sleep(0.15)
        receipt_index = args.index("--receipt-path")
        receipt_path = pathlib.Path(args[receipt_index + 1])
        del args[receipt_index:receipt_index + 2]
        del args[0]
        if args:
            raise RuntimeError("unexpected fake run-generator arguments")
    else:
        if args != [
            "guarded-review-drive",
            "--run-id",
            "run-guarded-review-zero-codex",
            "--boundary-id",
            "reviewbound_" + "a" * 32,
        ]:
            raise RuntimeError("unexpected guarded review arguments")
        receipt_path = database.with_suffix(".guarded-review.json")
    receipt_path.write_text(canonical({
        "argv_without_token": sys.argv[:-2],
        "command": command,
        "pgid": os.getpgrp(),
        "runner_token_sha256": runner_token_sha256,
        "setsid_children": setsid_children if command == "run-generator" else [],
    }), encoding="utf-8")
    raise SystemExit(0)
if token is None or domain not in {"owner", "guardian"}:
    raise RuntimeError("privileged command omitted its domain-bound FIFO")
state = json.loads(database.read_text(encoding="utf-8"))
expected_digest = (
    state["owner_token_sha256"]
    if domain == "owner"
    else state["prepare"]["guardian_token_sha256"]
)
if hashlib.sha256(token.encode("ascii")).hexdigest() != expected_digest:
    raise RuntimeError("scoped token authentication failed")

ps_visible = False
pid = os.getpid()
for _ in range(8):
    completed = subprocess.run(
        ["/bin/ps", "eww", "-p", str(pid)],
        text=True, capture_output=True, check=False, env={"PATH": "/usr/bin:/bin"},
    )
    ps_visible = ps_visible or token in completed.stdout
    parent = subprocess.run(
        ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
        text=True, capture_output=True, check=False, env={"PATH": "/usr/bin:/bin"},
    ).stdout.strip()
    if not parent.isdecimal() or int(parent) <= 1:
        break
    pid = int(parent)
with database.with_suffix(".calls.jsonl").open("a", encoding="utf-8") as stream:
    stream.write(canonical({
        "command": command,
        "domain": domain,
        "privileged_environment": sorted(PRIVILEGED & set(os.environ)),
        "token_visible_in_ancestor_ps": ps_visible,
    }) + "\n")

envelope = json.loads(sys.stdin.read())
payload = envelope["payload"]
if command == "guardian-prepare":
    if domain != "owner":
        raise RuntimeError("prepare is not owner-authenticated")
    if payload["launch_manifest_sha256"] != digest(payload["launch_manifest"]):
        raise RuntimeError("manifest digest mismatch")
    if payload["command_sha256"] != digest(payload["worker_runtime_command"]):
        raise RuntimeError("runtime command digest mismatch")
    state["prepare"] = payload
    state["launch_intent_sha256"] = digest(payload)
    database.write_text(canonical(state), encoding="utf-8")
    print(canonical({
        "schema_version": "rethlas_guardian_launch_intent_v1",
        "launch_intent_sha256": state["launch_intent_sha256"],
    }))
elif command == "guardian-register":
    if domain != "guardian":
        raise RuntimeError("register is not guardian-authenticated")
    request = payload["request"]
    if request["command_sha256"] != state["prepare"]["command_sha256"]:
        raise RuntimeError("registered command differs from prepare")
    now_wall = time.time()
    now_mono = time.monotonic()
    registration_id = "registration-zero-codex"
    projection = {
        "cycle_started_wall_epoch": now_wall,
        "cycle_started_monotonic": now_mono,
        "internal_interrupt_wall_epoch": now_wall + 5395.0,
        "internal_interrupt_monotonic": now_mono + 5395.0,
        "hard_stop_wall_epoch": now_wall + 5400.0,
        "hard_stop_monotonic": now_mono + 5400.0,
        "projected_wall_epoch": now_wall,
        "projected_monotonic": now_mono,
        "boot_identity": request["boot_identity"],
    }
    ack = {
        "registration_id": registration_id,
        "request_sha256": digest(request),
        "durable": True,
        "release_authorized": True,
        "projection": projection,
    }
    state["registration_ack"] = ack
    state["request"] = request
    state["poll_sequence"] = 0
    database.write_text(canonical(state), encoding="utf-8")
    print(canonical({"registration_ack": ack}))
elif command == "guardian-poll":
    poll_request = {
        "schema_version": "rethlas_guardian_poll_request_v1",
        "registration_id": payload["registration_id"],
        "request_sha256": payload["request_sha256"],
        "discovered_groups": payload["discovered_groups"],
        "expected_previous_snapshot_sha256": (
            payload["expected_previous_snapshot_sha256"]
        ),
    }
    if payload["expected_previous_snapshot_sha256"] != state.get("snapshot_sha256"):
        raise RuntimeError("fake guardian poll CAS is stale")
    state["poll_sequence"] += 1
    durable_discovered = {
        int(item["identity"]["pgid"]): item
        for item in state.get("durable_discovered_groups", [])
    }
    if not state.get("omit_discovered_echo"):
        durable_discovered.update({
            int(item["identity"]["pgid"]): item
            for item in payload["discovered_groups"]
        })
    live_discovered = []
    for pgid in sorted(durable_discovered):
        item = durable_discovered[pgid]
        try:
            os.kill(int(item["identity"]["pid"]), 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            pass
        live_discovered.append(item)
    state["durable_discovered_groups"] = live_discovered
    snapshot = {
        "sequence": state["poll_sequence"],
        "registration_id": state["registration_ack"]["registration_id"],
        "request_sha256": state["registration_ack"]["request_sha256"],
        "boot_identity": state["request"]["boot_identity"],
        "paid_groups": [
            state["request"]["root_group"],
            *live_discovered,
        ],
    }
    behavior = state.get("runner_setsid_behavior")
    if behavior is not None:
        echo_dir = pathlib.Path(behavior["echo_dir"])
        echo_counts = state.setdefault("echo_counts", {})
        for item in live_discovered:
            pgid = str(item["identity"]["pgid"])
            echo_counts[pgid] = int(echo_counts.get(pgid, 0)) + 1
            (echo_dir / (pgid + ".echo")).write_text(
                str(echo_counts[pgid]), encoding="utf-8"
            )
    state.setdefault("poll_history", []).append({
        "submitted": payload["discovered_groups"],
        "echoed": live_discovered,
        "snapshot": snapshot,
    })
    state["snapshot_sha256"] = digest(snapshot)
    database.write_text(canonical(state), encoding="utf-8")
    print(canonical({
        "schema_version": "rethlas_guardian_poll_result_v1",
        "snapshot": snapshot,
        "snapshot_sha256": state["snapshot_sha256"],
        "poll_request_sha256": digest(poll_request),
    }))
elif command in {"guardian-internal-interrupt", "guardian-lifeline-lost"}:
    print(canonical({"state": "committed"}))
elif command == "guardian-finalize":
    if state.get("substitute_offline_terminal"):
        offline_seed = {
            "schema_version": "rethlas_guardian_offline_finalize_v1",
            "operation_id": "offline-terminal-race",
            "manifest_sha256": "4" * 64,
            "registration_id": state["registration_ack"]["registration_id"],
            "report_sha256": "5" * 64,
            "state": "execution_unknown",
            "capture_sealed": False,
            "coverage_complete": False,
            "all_empty_verified": False,
            "terminal_sequence": 99,
        }
        state["offline_finalize"] = {
            **offline_seed,
            "receipt_sha256": digest(offline_seed),
        }
        database.write_text(canonical(state), encoding="utf-8")
        raise RuntimeError("daemon finalize lost race to durable offline terminal")
    state["terminal_report"] = payload["report"]
    database.write_text(canonical(state), encoding="utf-8")
    if state.get("lose_finalize_replies"):
        raise RuntimeError("finalize reply lost after durable commit")
    print(canonical({"state": payload["report"]["state"]}))
elif command == "guardian-status":
    report = state.get("terminal_report")
    offline = state.get("offline_finalize")
    print(canonical({
        "disposition": (
            "execution_unknown"
            if report is not None and state.get("terminal_status_lag")
            else (
                "guardian_terminal"
                if report is not None or offline is not None
                else "guardian_active"
            )
        ),
        "terminal_report": report,
        "offline_finalize": offline,
        "cycle_id": state["prepare"]["expected_cycle_id"],
        "clock_sha256": "8" * 64,
    }))
else:
    raise RuntimeError("unsupported fake guardian command: " + command)
"""


_OPAQUE_WORKER = r"""from __future__ import annotations
import json
import os
import pathlib
import subprocess
import sys

names = {
    "RETHLAS_REVIEW_CONTROL_TOKEN",
    "RETHLAS_GUARDIAN_CYCLE_TOKEN",
    "RETHLAS_RUNNER_CYCLE_TOKEN",
    "RETHLAS_STALE_RECOVERY_TOKEN",
}
open_fds = []
for descriptor in range(3, 128):
    try:
        os.fstat(descriptor)
    except OSError:
        continue
    open_fds.append(descriptor)
visible_names = []
pid = os.getpid()
for _ in range(8):
    output = subprocess.run(
        ["/bin/ps", "eww", "-p", str(pid)],
        text=True, capture_output=True, check=False,
        env={"PATH": "/usr/bin:/bin"},
    ).stdout
    visible_names.extend(name for name in names if name + "=" in output)
    parent = subprocess.run(
        ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
        text=True, capture_output=True, check=False,
        env={"PATH": "/usr/bin:/bin"},
    ).stdout.strip()
    if not parent.isdecimal() or int(parent) <= 1:
        break
    pid = int(parent)
pathlib.Path(sys.argv[1]).write_text(json.dumps({
    "argv": sys.argv,
    "open_fds": open_fds,
    "pgid": os.getpgrp(),
    "pid": os.getpid(),
    "stdin_bytes": sys.stdin.buffer.read().decode("utf-8", errors="replace"),
    "privileged_environment": sorted(names & set(os.environ)),
    "privileged_names_in_ancestor_ps": sorted(set(visible_names)),
}, sort_keys=True), encoding="utf-8")
"""


def _invoke_launcher(
    *,
    generation: Path,
    problem: Path,
    adapter: Path,
    database: Path,
    owner_token: str,
    run_id: str,
    watchdog_id: str,
    contract_sha256: str,
    policy_sha256: str,
    worker_mode: str,
    worker_command: list[str],
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, owner_token.encode("ascii"))
    os.close(write_fd)
    launcher_sha256 = hashlib.sha256(LAUNCHER.read_bytes()).hexdigest()
    try:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                _SECURE_LAUNCHER_LOADER,
                str(LAUNCHER),
                launcher_sha256,
                "--owner-token-fd",
                str(read_fd),
                "--db",
                str(database),
                "--adapter-path",
                str(adapter),
                "--adapter-sha256",
                hashlib.sha256(adapter.read_bytes()).hexdigest(),
                "--guardian-path",
                str(GUARDIAN),
                "--runner-path",
                str(RUNNER),
                "--run-id",
                run_id,
                "--generation-control-instance-id",
                "9" * 32,
                "--watchdog-id",
                watchdog_id,
                "--admission-mode",
                "initial_new_cycle",
                "--expected-cycle-id",
                guardian_launcher.guardian_cycle_id(
                    run_id=run_id, generation=1, watchdog_id=watchdog_id
                ),
                "--expected-generation",
                "1",
                "--capability-revision",
                "1",
                "--policy-contract-sha256",
                contract_sha256,
                "--policy-digest",
                policy_sha256,
                "--worker-cwd",
                str(generation),
                "--problem-path",
                str(problem),
                "--problem-relative-path",
                "data/opaque-probe.md",
                "--worker-mode",
                worker_mode,
                "--",
                *worker_command,
            ],
            cwd=generation,
            env={
                "HOME": os.environ["HOME"],
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            pass_fds=(read_fd,),
            input=stdin_text,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
    finally:
        os.close(read_fd)


def test_opaque_zero_codex_guardian_launch_uses_only_fifo_capabilities(
    tmp_path: Path,
) -> None:
    contract, contract_sha256, policy_sha256 = _policy_contract()
    generation = tmp_path / "agents" / "generation"
    data = generation / "data"
    data.mkdir(parents=True)
    problem = data / "opaque-probe.md"
    problem.write_text("Zero-Codex Guardian transport probe.\n", encoding="utf-8")
    adapter = tmp_path / "agents" / "hotjoin_adapter.py"
    adapter.write_text(
        _FAKE_ADAPTER.replace("__CONTRACT__", repr(contract)), encoding="utf-8"
    )
    worker = tmp_path / "opaque_worker.py"
    worker.write_text(_OPAQUE_WORKER, encoding="utf-8")
    marker = tmp_path / "opaque-worker.json"
    database = tmp_path / "guardian-state.json"
    owner_token = hashlib.sha256(b"opaque-zero-codex-owner").hexdigest()
    database.write_text(
        _canonical(
            {
                "owner_token_sha256": hashlib.sha256(
                    owner_token.encode("ascii")
                ).hexdigest()
            }
        ),
        encoding="utf-8",
    )
    run_id = "run-opaque-zero-codex"
    watchdog_id = "watchdog-opaque-zero-codex"
    completed = _invoke_launcher(
        generation=generation,
        problem=problem,
        adapter=adapter,
        database=database,
        owner_token=owner_token,
        run_id=run_id,
        watchdog_id=watchdog_id,
        contract_sha256=contract_sha256,
        policy_sha256=policy_sha256,
        worker_mode="opaque_guarded_command",
        worker_command=[
            "/usr/bin/python3",
            "-I",
            "-B",
            str(worker),
            str(marker),
        ],
        stdin_text="must-not-reach-the-paid-worker\n",
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["state"] == "completed"
    assert result["report"]["direct_returncode"] == 0
    assert result["release_marker"]["event"] == "worker_released"
    assert result["release_marker_sha256"] == _digest(result["release_marker"])
    projection = result["registration"]["registration_ack"]["projection"]
    assert (
        projection["cycle_started_wall_epoch"] <= result["release_marker"]["wall_epoch"]
    )
    assert (
        projection["cycle_started_monotonic"] <= result["release_marker"]["monotonic"]
    )
    worker_receipt = json.loads(marker.read_text(encoding="utf-8"))
    assert worker_receipt["argv"] == [str(worker), str(marker)]
    assert worker_receipt["open_fds"] == []
    assert worker_receipt["pid"] == result["release_marker"]["pid"]
    assert worker_receipt["pgid"] == result["release_marker"]["pgid"]
    assert worker_receipt["stdin_bytes"] == ""
    assert worker_receipt["privileged_environment"] == []
    assert worker_receipt["privileged_names_in_ancestor_ps"] == []
    assert result["report"]["state"] == "completed"
    assert result["report"]["forced"] is False
    assert result["release_marker"]["pgid"] in result["report"]["already_empty_pgids"]
    calls = [
        json.loads(line)
        for line in database.with_suffix(".calls.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [entry["command"] for entry in calls[:2]] == [
        "guardian-prepare",
        "guardian-register",
    ]
    assert all(entry["privileged_environment"] == [] for entry in calls)
    assert all(entry["token_visible_in_ancestor_ps"] is False for entry in calls)


def test_known_nonzero_worker_exit_is_durable_without_offline_recovery(
    tmp_path: Path,
) -> None:
    contract, contract_sha256, policy_sha256 = _policy_contract()
    generation = tmp_path / "agents" / "generation"
    data = generation / "data"
    data.mkdir(parents=True)
    problem = data / "opaque-probe.md"
    problem.write_text("Known nonzero Guardian terminal probe.\n", encoding="utf-8")
    adapter = tmp_path / "agents" / "hotjoin_adapter.py"
    adapter.write_text(
        _FAKE_ADAPTER.replace("__CONTRACT__", repr(contract)), encoding="utf-8"
    )
    database = tmp_path / "guardian-state.json"
    owner_token = hashlib.sha256(b"known-nonzero-owner").hexdigest()
    database.write_text(
        _canonical(
            {
                "owner_token_sha256": hashlib.sha256(
                    owner_token.encode("ascii")
                ).hexdigest()
            }
        ),
        encoding="utf-8",
    )
    completed = _invoke_launcher(
        generation=generation,
        problem=problem,
        adapter=adapter,
        database=database,
        owner_token=owner_token,
        run_id="run-known-nonzero",
        watchdog_id="watchdog-known-nonzero",
        contract_sha256=contract_sha256,
        policy_sha256=policy_sha256,
        worker_mode="opaque_guarded_command",
        worker_command=[
            "/usr/bin/python3",
            "-I",
            "-B",
            "-c",
            "raise SystemExit(70)",
        ],
    )

    assert completed.returncode == 70, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["state"] == "completed"
    assert result["report"]["state"] == "completed"
    assert result["report"]["direct_returncode"] == 70
    assert result["report"]["forced"] is False
    assert result["report"]["reason"] == "paid_group_empty"
    assert result["offline_finalize"] is None
    state = json.loads(database.read_text(encoding="utf-8"))
    assert state["terminal_report"] == result["report"]
    calls = [
        json.loads(line)["command"]
        for line in database.with_suffix(".calls.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert calls.count("guardian-finalize") == 1
    assert "guardian-offline-stop" not in calls


def test_durable_terminal_report_wins_finalize_reply_and_status_state_race(
    tmp_path: Path,
) -> None:
    contract, contract_sha256, policy_sha256 = _policy_contract()
    generation = tmp_path / "agents" / "generation"
    data = generation / "data"
    data.mkdir(parents=True)
    problem = data / "opaque-probe.md"
    problem.write_text("Terminal status race probe.\n", encoding="utf-8")
    adapter = tmp_path / "agents" / "hotjoin_adapter.py"
    adapter.write_text(
        _FAKE_ADAPTER.replace("__CONTRACT__", repr(contract)), encoding="utf-8"
    )
    database = tmp_path / "guardian-state.json"
    owner_token = hashlib.sha256(b"terminal-race-owner").hexdigest()
    database.write_text(
        _canonical(
            {
                "owner_token_sha256": hashlib.sha256(
                    owner_token.encode("ascii")
                ).hexdigest(),
                "lose_finalize_replies": True,
                "terminal_status_lag": True,
            }
        ),
        encoding="utf-8",
    )
    completed = _invoke_launcher(
        generation=generation,
        problem=problem,
        adapter=adapter,
        database=database,
        owner_token=owner_token,
        run_id="run-terminal-status-race",
        watchdog_id="watchdog-terminal-status-race",
        contract_sha256=contract_sha256,
        policy_sha256=policy_sha256,
        worker_mode="opaque_guarded_command",
        worker_command=["/usr/bin/python3", "-I", "-B", "-c", "pass"],
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["state"] == "completed"
    assert result["report"]["state"] == "completed"
    assert result["report"]["reason"] == "paid_group_empty"
    assert result["offline_finalize"] is None
    calls = [
        json.loads(line)["command"]
        for line in database.with_suffix(".calls.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert calls.count("guardian-finalize") == 2
    assert "guardian-offline-stop" not in calls


def test_durable_offline_finalize_status_wins_daemon_terminal_race(
    tmp_path: Path,
) -> None:
    contract, contract_sha256, policy_sha256 = _policy_contract()
    generation = tmp_path / "agents" / "generation"
    data = generation / "data"
    data.mkdir(parents=True)
    problem = data / "opaque-probe.md"
    problem.write_text("Offline terminal race probe.\n", encoding="utf-8")
    adapter = tmp_path / "agents" / "hotjoin_adapter.py"
    adapter.write_text(
        _FAKE_ADAPTER.replace("__CONTRACT__", repr(contract)), encoding="utf-8"
    )
    database = tmp_path / "guardian-state.json"
    owner_token = hashlib.sha256(b"offline-terminal-race-owner").hexdigest()
    database.write_text(
        _canonical(
            {
                "owner_token_sha256": hashlib.sha256(
                    owner_token.encode("ascii")
                ).hexdigest(),
                "substitute_offline_terminal": True,
            }
        ),
        encoding="utf-8",
    )
    completed = _invoke_launcher(
        generation=generation,
        problem=problem,
        adapter=adapter,
        database=database,
        owner_token=owner_token,
        run_id="run-offline-terminal-race",
        watchdog_id="watchdog-offline-terminal-race",
        contract_sha256=contract_sha256,
        policy_sha256=policy_sha256,
        worker_mode="opaque_guarded_command",
        worker_command=["/usr/bin/python3", "-I", "-B", "-c", "pass"],
    )

    assert completed.returncode != 0
    result = json.loads(completed.stdout)
    assert result["state"] == "execution_unknown"
    assert result["report"] is None
    assert result["offline_finalize"]["state"] == "execution_unknown"
    calls = [
        json.loads(line)["command"]
        for line in database.with_suffix(".calls.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert calls.count("guardian-finalize") == 2
    assert "guardian-offline-stop" not in calls


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
@pytest.mark.parametrize(
    "capture_mode",
    [
        "normal",
        "commit_reply_lost",
        "finalize_reply_lost",
        "reject",
        "late_head_advance",
    ],
)
def test_offline_stop_recaptures_setsid_descendant_after_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_mode: str,
) -> None:
    identities_path = tmp_path / "offline-identities.json"
    daemon_script = (
        "import json,os,time; from pathlib import Path; path=Path(%r); "
        "root=os.fork(); "
        "exec(\"os.setsid(); child=os.fork(); "
        "(os.setsid(), path.write_text(json.dumps({'root':os.getppid(),"
        "'escape':os.getpid()})), time.sleep(30)) if child == 0 else time.sleep(30)\") "
        "if root == 0 else time.sleep(30)"
    ) % str(identities_path)
    daemon = subprocess.Popen(
        [sys.executable, "-c", daemon_script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    inspector = SystemProcessInspector()
    known_pgids: set[int] = {daemon.pid}
    late_process: subprocess.Popen[bytes] | None = None
    try:
        deadline = time.monotonic() + 3.0
        while not identities_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        identities = json.loads(identities_path.read_text(encoding="utf-8"))
        root_pid = int(identities["root"])
        escaped_pid = int(identities["escape"])
        known_pgids.update((root_pid, escaped_pid))
        daemon_identity = inspector.identity(daemon.pid)
        root_identity = inspector.identity(root_pid)
        escaped_identity = inspector.identity(escaped_pid)
        assert daemon_identity is not None and daemon_identity.pid == daemon_identity.pgid
        assert root_identity is not None and root_identity.pid == root_identity.pgid
        assert escaped_identity is not None and escaped_identity.pid == escaped_identity.pgid
        late_identity = None
        if capture_mode == "late_head_advance":
            late_process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            known_pgids.add(late_process.pid)
            late_identity = inspector.identity(late_process.pid)
            assert late_identity is not None
            assert late_identity.pid == late_identity.pgid
            late_kill_attempts: list[int] = []

            class FlakyLateSignaler:
                def killpg(self, pgid: int, sig: int) -> None:
                    if pgid == late_identity.pgid and sig == signal.SIGKILL:
                        late_kill_attempts.append(pgid)
                        if len(late_kill_attempts) == 1:
                            raise OSError("injected first late SIGKILL failure")
                    os.killpg(pgid, sig)

            monkeypatch.setattr(
                guardian_module, "OSGroupSignaler", FlakyLateSignaler
            )
        manifest_seed = {
            "schema_version": "rethlas_guardian_offline_stop_v1",
            "operation_id": "offline_" + "1" * 48,
            "run_id": "run-offline-recapture",
            "cycle_id": "cycle-offline-recapture",
            "registration_id": "registration-offline-recapture",
            "request_sha256": "2" * 64,
            "expected_clock_sha256": "3" * 64,
            "state": "stop_required",
            "hard_stop_wall_epoch": time.time() - 1.0,
            "hard_stop_monotonic": time.monotonic() - 1.0,
            "boot_identity": inspector.boot_identity(),
            "observed_boot_identity": inspector.boot_identity(),
            "daemon_identity": daemon_identity.as_dict(),
            "groups": [PaidGroup("root", root_identity).as_dict()],
            "proven_empty_groups": [],
            "capture_round": 0,
            "capture_sealed": False,
            "previous_cleanup_manifest_sha256": None,
        }

        class OfflineHost:
            finalize_payload: dict[str, object] | None = None
            cleanup_manifest: dict[str, object] | None = None
            captured_groups: list[dict[str, object]] = []
            capture_receipts: dict[str, dict[str, object]] = {}
            lost_capture_replies = 0
            capture_status_calls = 0
            offline_finalize_result: dict[str, object] | None = None
            frozen_finalize_payload: dict[str, object] | None = None

            def invoke(
                self,
                command: str,
                payload: dict[str, object],
                *,
                token: str,
                token_domain: str,
            ) -> dict[str, object]:
                assert token == "a" * 64 and token_domain == "owner"
                if command == "guardian-offline-stop":
                    operation_id = str(payload["operation_id"])
                    manifest = {**manifest_seed, "operation_id": operation_id}
                    self.cleanup_manifest = {
                        **manifest,
                        "manifest_sha256": guardian_launcher.canonical_sha256(
                            manifest
                        ),
                    }
                    return dict(self.cleanup_manifest)
                if command == "guardian-offline-capture-status":
                    assert self.cleanup_manifest is not None
                    self.capture_status_calls += 1
                    if (
                        capture_mode == "late_head_advance"
                        and self.capture_status_calls == 3
                    ):
                        assert late_identity is not None
                        previous = self.cleanup_manifest
                        merged = {
                            item["identity"]["pgid"]: item
                            for item in [
                                *previous["groups"],
                                PaidGroup("root", escaped_identity).as_dict(),
                                PaidGroup("root", late_identity).as_dict(),
                            ]
                        }
                        next_seed = {
                            key: value
                            for key, value in previous.items()
                            if key != "manifest_sha256"
                        }
                        next_seed.update(
                            {
                                "groups": [
                                    merged[key] for key in sorted(merged)
                                ],
                                "capture_round": int(
                                    previous["capture_round"]
                                )
                                + 1,
                                "capture_sealed": False,
                                "previous_cleanup_manifest_sha256": previous[
                                    "manifest_sha256"
                                ],
                            }
                        )
                        self.cleanup_manifest = {
                            **next_seed,
                            "manifest_sha256": (
                                guardian_launcher.canonical_sha256(next_seed)
                            ),
                        }
                    status_seed = {
                        "schema_version": (
                            "rethlas_guardian_offline_capture_status_v1"
                        ),
                        "operation_id": payload["operation_id"],
                        "state": self.cleanup_manifest["state"],
                        "capture_round": self.cleanup_manifest["capture_round"],
                        "cleanup_manifest": self.cleanup_manifest,
                        "cleanup_manifest_sha256": self.cleanup_manifest[
                            "manifest_sha256"
                        ],
                    }
                    return {
                        **status_seed,
                        "receipt_sha256": guardian_launcher.canonical_sha256(
                            status_seed
                        ),
                    }
                if command == "guardian-offline-capture":
                    assert self.cleanup_manifest is not None
                    request = {
                        "schema_version": (
                            "rethlas_guardian_offline_capture_request_v1"
                        ),
                        "operation_id": payload["operation_id"],
                        "previous_cleanup_manifest_sha256": payload[
                            "previous_cleanup_manifest_sha256"
                        ],
                        "discovered_groups": list(payload["discovered_groups"]),
                    }
                    request_sha256 = guardian_launcher.canonical_sha256(request)
                    frozen = self.capture_receipts.get(request_sha256)
                    if frozen is not None:
                        if (
                            capture_mode == "commit_reply_lost"
                            and frozen["accepted_groups"]
                            and self.lost_capture_replies < 2
                        ):
                            self.lost_capture_replies += 1
                            raise RuntimeError("capture response lost after commit")
                        return dict(frozen)
                    previous = self.cleanup_manifest
                    assert payload["previous_cleanup_manifest_sha256"] == previous[
                        "manifest_sha256"
                    ]
                    submitted = list(payload["discovered_groups"])
                    if capture_mode in {"reject", "late_head_advance"}:
                        raise RuntimeError("capture transport unavailable")
                    self.captured_groups.extend(submitted)
                    merged = {
                        item["identity"]["pgid"]: item
                        for item in [*previous["groups"], *submitted]
                    }
                    next_seed = {
                        key: value
                        for key, value in previous.items()
                        if key != "manifest_sha256"
                    }
                    next_seed.update(
                        {
                            "groups": [merged[key] for key in sorted(merged)],
                            "capture_round": int(previous["capture_round"]) + 1,
                            "capture_sealed": not submitted,
                            "previous_cleanup_manifest_sha256": previous[
                                "manifest_sha256"
                            ],
                        }
                    )
                    next_manifest = {
                        **next_seed,
                        "manifest_sha256": guardian_launcher.canonical_sha256(
                            next_seed
                        ),
                    }
                    result_seed = {
                        "schema_version": "rethlas_guardian_offline_capture_v1",
                        "operation_id": payload["operation_id"],
                        "capture_request_sha256": guardian_launcher.canonical_sha256(
                            request
                        ),
                        "previous_cleanup_manifest_sha256": previous[
                            "manifest_sha256"
                        ],
                        "accepted_groups": submitted,
                        "already_empty_groups": [],
                        "cleanup_manifest": next_manifest,
                        "cleanup_manifest_sha256": next_manifest[
                            "manifest_sha256"
                        ],
                    }
                    self.cleanup_manifest = next_manifest
                    result = {
                        **result_seed,
                        "receipt_sha256": guardian_launcher.canonical_sha256(
                            result_seed
                        ),
                    }
                    self.capture_receipts[request_sha256] = result
                    if (
                        capture_mode == "commit_reply_lost"
                        and submitted
                        and self.lost_capture_replies < 2
                    ):
                        self.lost_capture_replies += 1
                        raise RuntimeError("capture response lost after commit")
                    return dict(result)
                if command == "guardian-status":
                    assert self.offline_finalize_result is not None
                    return {
                        "disposition": "guardian_terminal",
                        "terminal_report": None,
                        "offline_finalize": self.offline_finalize_result,
                        "cycle_id": "cycle-offline-recapture",
                        "clock_sha256": "3" * 64,
                    }
                assert command == "guardian-offline-finalize"
                self.finalize_payload = dict(payload)
                if self.frozen_finalize_payload is None:
                    self.frozen_finalize_payload = dict(payload)
                else:
                    assert payload == self.frozen_finalize_payload
                expected_state = (
                    "execution_unknown"
                    if capture_mode in {"reject", "late_head_advance"}
                    else "watchdog_forced"
                )
                result_seed = {
                    "schema_version": "rethlas_guardian_offline_finalize_v1",
                    "operation_id": payload["operation_id"],
                    "manifest_sha256": payload["manifest_sha256"],
                    "registration_id": "registration-offline-recapture",
                    "report_sha256": "9" * 64,
                    "state": expected_state,
                    "capture_sealed": bool(
                        self.cleanup_manifest["capture_sealed"]
                    ),
                    "coverage_complete": (
                        capture_mode != "late_head_advance"
                    ),
                    "all_empty_verified": (
                        capture_mode != "late_head_advance"
                    ),
                    "terminal_sequence": 7,
                }
                result = {
                    **result_seed,
                    "receipt_sha256": guardian_launcher.canonical_sha256(
                        result_seed
                    ),
                }
                self.offline_finalize_result = result
                if capture_mode == "finalize_reply_lost":
                    raise RuntimeError("finalize response lost after commit")
                return result

        host = OfflineHost()
        result = guardian_launcher._offline_stop(  # noqa: SLF001
            guardian_module,
            host,  # type: ignore[arg-type]
            SimpleNamespace(
                run_id="run-offline-recapture",
                watchdog_id="watchdog-offline-recapture",
            ),
            owner_token="a" * 64,
            status={
                "cycle_id": "cycle-offline-recapture",
                "clock_sha256": "3" * 64,
            },
            daemon_pid=daemon.pid,
        )
        assert result["state"] == (
            "execution_unknown"
            if capture_mode in {"reject", "late_head_advance"}
            else "watchdog_forced"
        )
        assert host.finalize_payload is not None
        assert [item["identity"]["pgid"] for item in host.captured_groups] == (
            []
            if capture_mode in {"reject", "late_head_advance"}
            else [escaped_pid]
        )
        assert host.cleanup_manifest is not None
        assert host.finalize_payload["manifest_sha256"] == host.cleanup_manifest[
            "manifest_sha256"
        ]
        assert "locally_captured_groups" not in host.finalize_payload
        failure = host.finalize_payload["failure"]
        if capture_mode in {"reject", "late_head_advance"}:
            assert isinstance(failure, dict)
            assert failure["code"] == "offline_cleanup_failure"
            assert failure["groups_complete"] is True
            assert failure["group_count"] == (
                0 if capture_mode == "late_head_advance" else 1
            )
            assert [
                item["identity"]["pgid"] for item in failure["groups"]
            ] == ([] if capture_mode == "late_head_advance" else [escaped_pid])
            assert failure["groups_sha256"] == _digest(failure["groups"])
            assert host.finalize_payload["failure_sha256"] == _digest(failure)
        else:
            assert failure is None
            assert host.finalize_payload["failure_sha256"] is None
        if late_process is not None:
            late_process.wait(timeout=1.0)
            assert late_process.returncode == -signal.SIGKILL
            assert len(late_kill_attempts) >= 2
        covered = set(host.finalize_payload["killed_pgids"]) | set(
            host.finalize_payload["already_empty_pgids"]
        )
        assert covered == known_pgids
        for pgid in known_pgids:
            assert inspector.identity(pgid) is None
            assert inspector.group_members(pgid) == ()
    finally:
        for pgid in known_pgids:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        try:
            daemon.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            daemon.kill()
            daemon.wait(timeout=1.0)
        if late_process is not None:
            try:
                late_process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                late_process.kill()
                late_process.wait(timeout=1.0)


def test_reboot_offline_manifest_finalizes_unknown_with_zero_process_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_uid = os.getuid()
    old_root = {
        "role": "root",
        "identity": {
            "pid": 81001,
            "uid": old_uid,
            "pgid": 81001,
            "start_marker": "old-boot-root",
        },
    }
    old_daemon = {
        "pid": 81002,
        "uid": old_uid,
        "pgid": 81002,
        "start_marker": "old-boot-daemon",
    }

    class ForbiddenInspector:
        def __init__(self) -> None:
            return None

        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"reboot cleanup inspected process state: {name}")

    class ForbiddenSignaler:
        def __init__(self) -> None:
            raise AssertionError("reboot cleanup constructed a signaler")

    monkeypatch.setattr(
        guardian_module, "SystemProcessInspector", ForbiddenInspector
    )
    monkeypatch.setattr(guardian_module, "OSGroupSignaler", ForbiddenSignaler)
    manifest_seed = {
        "schema_version": "rethlas_guardian_offline_stop_v1",
        "operation_id": "",
        "run_id": "run-reboot-offline",
        "cycle_id": "cycle-reboot-offline",
        "registration_id": "registration-reboot-offline",
        "request_sha256": "2" * 64,
        "expected_clock_sha256": "3" * 64,
        "state": "reboot_proven_terminal",
        "hard_stop_wall_epoch": 10.0,
        "hard_stop_monotonic": 20.0,
        "boot_identity": "old-boot",
        "observed_boot_identity": "new-boot",
        "daemon_identity": old_daemon,
        "groups": [],
        "proven_empty_groups": [old_root],
        "capture_round": 0,
        "capture_sealed": True,
        "previous_cleanup_manifest_sha256": None,
    }

    class RebootHost:
        manifest: dict[str, object] | None = None
        finalize_payload: dict[str, object] | None = None

        def invoke(
            self,
            command: str,
            payload: dict[str, object],
            *,
            token: str,
            token_domain: str,
        ) -> dict[str, object]:
            assert token == "a" * 64 and token_domain == "owner"
            if command == "guardian-offline-stop":
                seed = {**manifest_seed, "operation_id": payload["operation_id"]}
                self.manifest = {
                    **seed,
                    "manifest_sha256": guardian_launcher.canonical_sha256(seed),
                }
                return dict(self.manifest)
            if command == "guardian-offline-capture-status":
                assert self.manifest is not None
                seed = {
                    "schema_version": (
                        "rethlas_guardian_offline_capture_status_v1"
                    ),
                    "operation_id": payload["operation_id"],
                    "state": "already_empty",
                    "capture_round": 0,
                    "cleanup_manifest": self.manifest,
                    "cleanup_manifest_sha256": self.manifest["manifest_sha256"],
                }
                return {
                    **seed,
                    "receipt_sha256": guardian_launcher.canonical_sha256(seed),
                }
            assert command == "guardian-offline-finalize"
            self.finalize_payload = dict(payload)
            result_seed = {
                "schema_version": "rethlas_guardian_offline_finalize_v1",
                "operation_id": payload["operation_id"],
                "manifest_sha256": payload["manifest_sha256"],
                "registration_id": "registration-reboot-offline",
                "report_sha256": "9" * 64,
                "state": "execution_unknown",
                "capture_sealed": True,
                "coverage_complete": True,
                "all_empty_verified": True,
                "terminal_sequence": 8,
            }
            return {
                **result_seed,
                "receipt_sha256": guardian_launcher.canonical_sha256(
                    result_seed
                ),
            }

    host = RebootHost()
    result = guardian_launcher._offline_stop(  # noqa: SLF001
        guardian_module,
        host,  # type: ignore[arg-type]
        SimpleNamespace(
            run_id="run-reboot-offline",
            watchdog_id="watchdog-reboot-offline",
        ),
        owner_token="a" * 64,
        status={
            "cycle_id": "cycle-reboot-offline",
            "clock_sha256": "3" * 64,
        },
        daemon_pid=81002,
    )

    assert result["state"] == "execution_unknown"
    assert host.finalize_payload is not None
    assert host.finalize_payload["stopped_pgids"] == []
    assert host.finalize_payload["killed_pgids"] == []
    assert host.finalize_payload["already_empty_pgids"] == [81001, 81002]
    assert host.finalize_payload["failure"]["code"] == "offline_cleanup_failure"
    assert host.finalize_payload["failure"]["groups"] == []
    assert isinstance(host.finalize_payload["failure_sha256"], str)


def test_runner_control_exec_target_consumes_exact_fifo(tmp_path: Path) -> None:
    contract, contract_sha256, policy_sha256 = _policy_contract()
    generation = tmp_path / "agents" / "generation"
    data = generation / "data"
    data.mkdir(parents=True)
    problem = data / "opaque-probe.md"
    problem.write_text("Zero-Codex runner FD probe.\n", encoding="utf-8")
    adapter = tmp_path / "agents" / "hotjoin_adapter.py"
    adapter.write_text(
        _FAKE_ADAPTER.replace("__CONTRACT__", repr(contract)), encoding="utf-8"
    )
    marker = tmp_path / "runner-worker.json"
    database = tmp_path / "guardian-state.json"
    owner_token = hashlib.sha256(b"runner-zero-codex-owner").hexdigest()
    database.write_text(
        _canonical(
            {
                "owner_token_sha256": hashlib.sha256(
                    owner_token.encode("ascii")
                ).hexdigest()
            }
        ),
        encoding="utf-8",
    )
    completed = _invoke_launcher(
        generation=generation,
        problem=problem,
        adapter=adapter,
        database=database,
        owner_token=owner_token,
        run_id="run-runner-zero-codex",
        watchdog_id="watchdog-runner-zero-codex",
        contract_sha256=contract_sha256,
        policy_sha256=policy_sha256,
        worker_mode="runner_control",
        worker_command=[
            "/usr/bin/python3",
            str(adapter),
            "--db",
            str(database),
            "run-generator",
            "--receipt-path",
            str(marker),
        ],
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    result = json.loads(completed.stdout)
    assert result["state"] == "completed"
    assert result["release_marker"] is None
    receipt = json.loads(marker.read_text(encoding="utf-8"))
    assert receipt["argv_without_token"][-3:] == [
        "run-generator",
        "--receipt-path",
        str(marker),
    ]
    state = json.loads(database.read_text(encoding="utf-8"))
    assert receipt["runner_token_sha256"] == state["prepare"]["runner_token_sha256"]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX sessions")
@pytest.mark.parametrize("omit_discovered_echo", [False, True])
def test_runner_control_durably_attests_real_setsid_descendants(
    tmp_path: Path,
    omit_discovered_echo: bool,
) -> None:
    contract, contract_sha256, policy_sha256 = _policy_contract()
    generation = tmp_path / "agents" / "generation"
    data = generation / "data"
    data.mkdir(parents=True)
    problem = data / "opaque-probe.md"
    problem.write_text("Runner durable discovery probe.\n", encoding="utf-8")
    adapter = tmp_path / "agents" / "hotjoin_adapter.py"
    adapter.write_text(
        _FAKE_ADAPTER.replace("__CONTRACT__", repr(contract)), encoding="utf-8"
    )
    marker = tmp_path / "runner-worker.json"
    ready_dir = tmp_path / "setsid-ready"
    echo_dir = tmp_path / "setsid-echo"
    ready_dir.mkdir()
    echo_dir.mkdir()
    database = tmp_path / "guardian-state.json"
    owner_token = hashlib.sha256(b"runner-durable-discovery-owner").hexdigest()
    database.write_text(
        _canonical(
            {
                "owner_token_sha256": hashlib.sha256(
                    owner_token.encode("ascii")
                ).hexdigest(),
                "omit_discovered_echo": omit_discovered_echo,
                "runner_setsid_behavior": {
                    "count": 2,
                    "echo_dir": str(echo_dir),
                    "ready_dir": str(ready_dir),
                },
            }
        ),
        encoding="utf-8",
    )
    completed = _invoke_launcher(
        generation=generation,
        problem=problem,
        adapter=adapter,
        database=database,
        owner_token=owner_token,
        run_id="run-runner-durable-discovery",
        watchdog_id="watchdog-runner-durable-discovery",
        contract_sha256=contract_sha256,
        policy_sha256=policy_sha256,
        worker_mode="runner_control",
        worker_command=[
            "/usr/bin/python3",
            str(adapter),
            "--db",
            str(database),
            "run-generator",
            "--receipt-path",
            str(marker),
        ],
    )

    state = json.loads(database.read_text(encoding="utf-8"))
    child_pgids = {int(path.stem) for path in ready_dir.glob("*.ready")}
    assert len(child_pgids) == 2
    submitted_pgids = {
        int(item["identity"]["pgid"])
        for poll in state["poll_history"]
        for item in poll["submitted"]
    }
    assert child_pgids <= submitted_pgids
    inspector = SystemProcessInspector()
    root_pgid = int(state["request"]["root_group"]["identity"]["pgid"])
    for pgid in child_pgids | {root_pgid}:
        assert inspector.identity(pgid) is None
        assert inspector.group_members(pgid) == ()

    result = json.loads(completed.stdout)
    if not omit_discovered_echo:
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert result["state"] == "completed"
        assert result["report"]["direct_returncode"] == 0
        echoed_pgids = {
            int(item["identity"]["pgid"])
            for poll in state["poll_history"]
            for item in poll["echoed"]
        }
        assert child_pgids <= echoed_pgids
        for pgid in child_pgids:
            echoed_indexes = [
                index
                for index, poll in enumerate(state["poll_history"])
                if pgid
                in {
                    int(item["identity"]["pgid"])
                    for item in poll["echoed"]
                }
            ]
            assert len(echoed_indexes) >= 2
            assert any(
                pgid
                not in {
                    int(item["identity"]["pgid"])
                    for item in state["poll_history"][index]["submitted"]
                }
                for index in echoed_indexes
            )
            assert any(
                index > echoed_indexes[-1]
                and {
                    int(item["identity"]["pgid"])
                    for item in poll["snapshot"]["paid_groups"]
                }
                == {root_pgid}
                for index, poll in enumerate(state["poll_history"])
            )
        assert child_pgids <= set(result["report"]["already_empty_pgids"])
        receipt = json.loads(marker.read_text(encoding="utf-8"))
        assert set(receipt["setsid_children"]) == child_pgids
    else:
        assert completed.returncode == 70, completed.stdout + completed.stderr
        assert result["state"] == "execution_unknown"
        assert "durable host poll omitted a discovered paid group" in result[
            "report"
        ]["reason"]
        assert state["terminal_report"] == result["report"]
        assert child_pgids <= set(result["report"]["stopped_pgids"])
        assert child_pgids <= set(result["report"]["killed_pgids"])
        assert root_pgid in (
            set(result["report"]["killed_pgids"])
            | set(result["report"]["already_empty_pgids"])
        )
        assert not marker.exists()


def test_runner_control_guarded_review_is_exact_and_stays_in_guardian_root(
    tmp_path: Path,
) -> None:
    contract, contract_sha256, policy_sha256 = _policy_contract()
    generation = tmp_path / "agents" / "generation"
    data = generation / "data"
    data.mkdir(parents=True)
    problem = data / "opaque-probe.md"
    problem.write_text("Zero-Codex guarded review FD probe.\n", encoding="utf-8")
    adapter = tmp_path / "agents" / "hotjoin_adapter.py"
    adapter.write_text(
        _FAKE_ADAPTER.replace("__CONTRACT__", repr(contract)), encoding="utf-8"
    )
    database = tmp_path / "guardian-state.json"
    owner_token = hashlib.sha256(b"guarded-review-zero-codex-owner").hexdigest()
    database.write_text(
        _canonical(
            {
                "owner_token_sha256": hashlib.sha256(
                    owner_token.encode("ascii")
                ).hexdigest()
            }
        ),
        encoding="utf-8",
    )
    boundary_id = "reviewbound_" + "a" * 32
    completed = _invoke_launcher(
        generation=generation,
        problem=problem,
        adapter=adapter,
        database=database,
        owner_token=owner_token,
        run_id="run-guarded-review-zero-codex",
        watchdog_id="watchdog-guarded-review-zero-codex",
        contract_sha256=contract_sha256,
        policy_sha256=policy_sha256,
        worker_mode="runner_control",
        worker_command=[
            "/usr/bin/python3",
            str(adapter),
            "--db",
            str(database),
            "guarded-review-drive",
            "--run-id",
            "run-guarded-review-zero-codex",
            "--boundary-id",
            boundary_id,
        ],
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    receipt = json.loads(
        database.with_suffix(".guarded-review.json").read_text(encoding="utf-8")
    )
    state = json.loads(database.read_text(encoding="utf-8"))
    assert receipt["command"] == "guarded-review-drive"
    assert receipt["runner_token_sha256"] == state["prepare"]["runner_token_sha256"]
    assert receipt["pgid"] == state["request"]["root_group"]["identity"]["pgid"]
    assert receipt["argv_without_token"][-5:] == [
        "guarded-review-drive",
        "--run-id",
        "run-guarded-review-zero-codex",
        "--boundary-id",
        boundary_id,
    ]


@pytest.mark.parametrize(
    "tail",
    [
        ["guarded-review-drive", "--run-id", "wrong", "--boundary-id", "reviewbound_" + "a" * 32],
        ["guarded-review-drive", "--run-id", "run-1", "--boundary-id", "bad"],
        ["guarded-review-drive", "--run-id", "run-1"],
    ],
)
def test_runner_control_rejects_inexact_guarded_review_target(
    tmp_path: Path,
    tail: list[str],
) -> None:
    adapter = tmp_path / "hotjoin_adapter.py"
    adapter.write_text("# pinned test adapter\n", encoding="utf-8")
    source = guardian_launcher.PinnedSource.open(adapter)
    try:
        configuration = SimpleNamespace(
            worker_command=(sys.executable, str(adapter), "--db", str(tmp_path / "db"), *tail),
            database_path=tmp_path / "db",
            run_id="run-1",
        )
        with pytest.raises(guardian_launcher.LauncherError, match="exact pinned adapter"):
            guardian_launcher._pinned_runner_control_command(  # noqa: SLF001
                configuration, source, source.descriptor
            )
    finally:
        source.close()


def test_daemon_discovery_mode_is_bound_to_the_exact_runner_closure(
    tmp_path: Path,
) -> None:
    adapter = tmp_path / "hotjoin_adapter.py"
    adapter.write_text("# pinned test adapter\n", encoding="utf-8")
    database = tmp_path / "messages.sqlite3"
    source = guardian_launcher.PinnedSource.open(adapter)
    host = guardian_launcher.PinnedAdapterClient(source, database)
    runner_token_fd = 97

    def configuration(tail: tuple[str, ...]) -> SimpleNamespace:
        return SimpleNamespace(
            worker_mode="runner_control",
            worker_command=(
                sys.executable,
                str(adapter),
                "--db",
                str(database),
                *tail,
            ),
            database_path=database,
            run_id="run-1",
        )

    generator = configuration(("run-generator", "--problem", "probe"))
    guarded_review = configuration(
        (
            "guarded-review-drive",
            "--run-id",
            "run-1",
            "--boundary-id",
            "reviewbound_" + "a" * 32,
        )
    )
    try:
        generator_target = guardian_launcher._pinned_runner_control_command(  # noqa: SLF001
            generator, source, source.descriptor
        )
        review_target = guardian_launcher._pinned_runner_control_command(  # noqa: SLF001
            guarded_review, source, source.descriptor
        )
        assert guardian_launcher._durably_attest_discovered_groups(  # noqa: SLF001
            generator,
            host,
            worker_adapter_fd=source.descriptor,
            runner_token_fd=runner_token_fd,
            blocked_command=(
                *generator_target,
                "--runner-token-fd",
                str(runner_token_fd),
            ),
        )
        assert not guardian_launcher._durably_attest_discovered_groups(  # noqa: SLF001
            guarded_review,
            host,
            worker_adapter_fd=source.descriptor,
            runner_token_fd=runner_token_fd,
            blocked_command=(
                *review_target,
                "--runner-token-fd",
                str(runner_token_fd),
            ),
        )
        with pytest.raises(
            guardian_launcher.LauncherError,
            match="differs from its pinned launch closure",
        ):
            guardian_launcher._durably_attest_discovered_groups(  # noqa: SLF001
                generator,
                host,
                worker_adapter_fd=source.descriptor,
                runner_token_fd=runner_token_fd,
                blocked_command=(
                    *review_target,
                    "--runner-token-fd",
                    str(runner_token_fd),
                ),
            )
        assert guardian_launcher._durably_attest_discovered_groups(  # noqa: SLF001
            SimpleNamespace(worker_mode="opaque_guarded_command"),
            host,
            worker_adapter_fd=None,
            runner_token_fd=runner_token_fd,
            blocked_command=(sys.executable, "-c", "pass"),
        )
    finally:
        source.close()


@pytest.mark.skipif(not CODEX_BIN.is_file(), reason="desktop Codex binary absent")
def test_large_desktop_codex_binary_is_stream_pinned_without_loading() -> None:
    pinned = guardian_launcher.PinnedExecutable.open(CODEX_BIN)
    try:
        assert pinned.size > guardian_launcher._MAX_SOURCE_BYTES
        assert pinned.size <= guardian_launcher._MAX_EXECUTABLE_BYTES
        assert not hasattr(pinned, "content")
        assert pinned.sha256 == _file_sha256(CODEX_BIN)
        pinned.attest_unchanged()
    finally:
        pinned.close()


def test_launcher_direct_path_execution_fails_before_prepare(tmp_path: Path) -> None:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, ("a" * 64).encode("ascii"))
    os.close(write_fd)
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(LAUNCHER),
            "--owner-token-fd",
            str(read_fd),
            "--db",
            str(tmp_path / "absent.sqlite3"),
            "--adapter-path",
            str(tmp_path / "absent.py"),
            "--adapter-sha256",
            "0" * 64,
            "--guardian-path",
            str(GUARDIAN),
            "--runner-path",
            str(RUNNER),
            "--run-id",
            "run-direct-rejected",
            "--generation-control-instance-id",
            "1" * 32,
            "--watchdog-id",
            "watchdog-direct-rejected",
            "--admission-mode",
            "initial_new_cycle",
            "--expected-cycle-id",
            guardian_launcher.guardian_cycle_id(
                run_id="run-direct-rejected",
                generation=1,
                watchdog_id="watchdog-direct-rejected",
            ),
            "--expected-generation",
            "1",
            "--capability-revision",
            "1",
            "--policy-contract-sha256",
            "2" * 64,
            "--policy-digest",
            "3" * 64,
            "--worker-cwd",
            str(tmp_path),
            "--problem-path",
            str(tmp_path / "absent.md"),
            "--problem-relative-path",
            "data/absent.md",
            "--",
            "/usr/bin/true",
        ],
        env={"PATH": "/usr/bin:/bin"},
        pass_fds=(read_fd,),
        text=True,
        capture_output=True,
        check=False,
    )
    os.close(read_fd)
    assert completed.returncode == 70
    assert "pinned-FD loader" in completed.stderr
