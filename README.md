# Rethlas

Rethlas is a natural-language reasoning system for mathematics built around two core Codex roles:

- The generation agent reads a math problem from a markdown file and writes an informal proof blueprint.
- The verification agent checks that proof blueprint, produces a structured verdict, and serves as the generation agent's verifier.

Both agents default to `gpt-5.6-sol`. Generation, including its subgoal
prover, defaults to `max` reasoning effort; verification defaults to `xhigh`.
The generation runner preserves explicit `MODEL` and `REASONING_EFFORT`
overrides, while the verification service preserves explicit `CODEX_MODEL`
and `CODEX_REASONING_EFFORT` overrides.

The intended deployment order is:

1. Start the verification agent as a local HTTP service.
2. Run the generation agent through Codex.
3. Let the generation agent call the verification service during its proof-and-repair loop.

## What's New in August 2026

### Safe three-route parallel generation

Rethlas again explores three mathematical directions concurrently, with new
safety boundaries around the original fanout model. On a hard problem, the root
first designs exactly three materially different, scope-disjoint routes and
commits one content-addressed pre-fanout checkpoint. It then starts one
context-free solver for each route in a single fanout:

```text
root route design
  -> pre-fanout checkpoint
  -> route solver 1 | route solver 2 | route solver 3
  -> root merge and candidate selection
  -> independent verifier
```

The root is the only shared-memory writer and does not run a fourth proof route.
Children cannot recursively spawn, switch assigned routes, write shared memory,
verify, publish, yield, or initiate advisor work. A complete candidate from any
route preempts the remaining waits and enters the verifier fast lane. Guardian
allows three live proof children and fail-stops a fourth. If the root already
has a complete candidate during route design, it skips the fanout and verifies
the candidate directly.

### Durable runtime and verification

- Rethlas now uses the official MCP SDK directly and supports both its 1.x and
  2.x server-class locations.
- Phase checkpoints use an exact `CallToolResult` envelope and content-addressed
  replay across separate primary and recovery MCP processes.
- Optional hot-join mode provides an absolute 60/120/150-minute route cycle,
  cooperative review drains, independent scheduled reviews, partial-route
  preservation, context handoffs, and Guardian process-tree cleanup.
- Verification uses fresh isolated sessions, adaptive lazy proof context, strict
  schema validation, and atomic publication of `blueprint_verified.md` plus an
  external receipt.
- Owner-wait states and `generation_yield` are hot-join-only. Legacy runs persist
  truthful failures and return unverified without fabricating an owner gate.
- `RETHLAS_COST_GATE_POLICY=owner_gated` is the default and is hash-bound into
  the hot-join policy contract. `disabled_by_owner` retains telemetry but cannot
  enter a cost-gate owner wait.
- Review-driven hardening isolates every trusted runner Python invocation,
  rebuilds publication attestations from the pinned `proof_context` snapshot,
  binds one-click advisor state to its immutable event, makes terminal receipts
  recoverable after SQL/filesystem crash cuts, and rejects noncanonical
  checkpoint inputs before publication.

The current release was validated with a real three-solver fanout, a complete
generation-to-verification publication smoke, and the full repository suite:
1,135 tests and 56 subtests passed, with one expected skip.

## Repository Layout

- `agents/generation`: the proof-generation agent
- `agents/verification`: the proof-verification agent

In particular, 
- Original problems are put in `agents/generation/data/`, e.g. unclassified problem `agents/generation/data/example.md`, or classfied problem `agents/generation/data/modrep/modrep.md`, `agents/generation/data/example/example1.md`.
- Zola project to render the results in a static website is in `agents/generation/site/`.

## 1. Install Codex CLI

Install the Codex CLI:

```bash
npm install -g @openai/codex
```


## 2. Clone the Repository

```bash
git clone https://github.com/yexf308/Rethlas.git
cd Rethlas
```

## 3. Start the Verification Service


```bash
cd agents/verification
python3 -m venv .venv
source .venv/bin/activate
pip install -r api/requirements.txt
uvicorn api.server:app --host 127.0.0.1 --port 8091
```

Using uv
```bash
cd agents/verification
uv venv 
uv pip install -r api/requirements.txt
uv run uvicorn api.server:app --host 127.0.0.1 --port 8091
```

The verification endpoint accepts loopback clients by default. If it must be
exposed through a container or remote interface, set the same high-entropy
`VERIFY_API_TOKEN` in both the verification-service and generation-agent
environments; remote requests without `Authorization: Bearer ...` are rejected
before request-body parsing. For untrusted or adversarial proof input, a
dedicated container or OS account with no unrelated readable secrets is a
requirement, not an optional hardening step: Codex `read-only` prevents writes
but does not prevent reads of every host file.
Each proof item is sent over stdin to an ephemeral minimal verifier workspace
under a read-only Codex sandbox; user-level Codex configuration is ignored and
only a validated JSON result is copied back. This reduces prompt-injection
impact; it is not a host-read confidentiality boundary.

The production verifier returns one schema-constrained JSON object, and the CLI
writes that last message to the isolated output directory with
`--output-last-message`. Direct output is the only verdict authority. For every
fresh adaptive round, the service also injects one complete MCP object through
CLI `-c`: `command` is the absolute, non-realpath `sys.executable` of the
running service, and `args`, isolated `cwd`, and `tool_timeout_sec` are supplied
in the same object. It never relies on an isolated workspace's
`.codex/config.toml` being auto-loaded. The MCP exposes optional reference
search/bounded memory but no verdict-validation or verdict-write tool.

Before allocating a persistent run or starting Codex, the service performs an
actual import check for the complete injected MCP runtime (the official MCP SDK,
`requests`, and `jsonschema`) in the bound service interpreter. A missing or
broken dependency therefore starts zero paid subprocesses and leaves no run
record. `api/requirements.txt` includes the single authoritative
`mcp/requirements.txt`, preventing the API and injected MCP dependency sets
from drifting. The service
then applies a stricter production validator: schema version/status/request
semantics, checked item ids, proof/context digests, findings, verdict, and
repair hints must all be consistent. Raw Codex stdout/stderr is held only in an
unlinked temporary file; persistent logs retain numeric elapsed time and token
usage (or `unavailable`), never proof/model stream content.

The verifier is resource-bounded by default. The main controls are:

- `VERIFY_CONTEXT_MAX_CHARS=200000` per complete proof-item context; a single
  current item whose canonical record exceeds this bound is rejected explicitly
- `VERIFY_MAX_TOTAL_CONTEXT_CHARS=5000000` per blueprint
- `VERIFY_MAX_PROOF_CHARS=2000000` for the complete blueprint request; this is
  an aggregate transport bound, not a promise that one 2M-character item fits
  the per-item verifier context
- `VERIFY_MAX_REQUEST_BYTES=25265536` before JSON parsing (derived default)
- `VERIFY_BODY_TIMEOUT_SECONDS=30` for the complete request upload
- `VERIFY_MAX_ITEMS=128`
- `VERIFY_MAX_PROMPT_BYTES=500000` per serialized model prompt
- `VERIFY_MAX_TOTAL_PROMPT_BYTES=5000000` per request
- `VERIFY_MAX_OUTPUT_BYTES=1000000` for each direct verifier JSON result
- `VERIFY_MAX_EXPANSION_ROUNDS=2` after statement-only round zero
- `VERIFY_MAX_EXPANDED_PROOFS=8` per proof item
- `VERIFY_MAX_EXPANDED_PROOF_CHARS=200000` using complete canonical records
- `VERIFY_MAX_CONCURRENT_REQUESTS=1`
- `CODEX_TIMEOUT_SECONDS=3600` per item
- `RETHLAS_PUBLICATION_LOCK_TIMEOUT_SECONDS=10` for the bounded final publish lock
- `VERIFY_REQUEST_TIMEOUT_SECONDS=3500` for the complete HTTP request

`VERIFY_REQUEST_TIMEOUT_SECONDS` should remain lower than the generation
client's 3600-second HTTP timeout. Provider authentication variables commonly
used by Codex are forwarded to the Codex process, while model-spawned shell
commands receive an empty inherited environment. Additional required provider
variables can be listed in `VERIFY_CODEX_FORWARD_ENV`, separated by commas.
For a verifier at a non-default address, set `VERIFY_HEALTH_URL` and
`VERIFY_PROOF_URL` in the generation environment. `VERIFY_URL` remains a
backward-compatible alias for the health URL. Non-loopback verification URLs
must use HTTPS; a bearer token over plain remote HTTP is rejected because it
does not authenticate the response.

## 4. Run the Generation Agent on the Included Example


```bash
cd agents
python3 -m venv --copies .generation-venv
source .generation-venv/bin/activate
pip install -r generation/requirements-math-research.txt
cd generation
./tests/run_example.sh
```

The generation MCP environment deliberately lives beside, rather than inside,
`agents/generation`: the generation Codex can write its workspace, so an
interpreter or site-packages directory placed there cannot be part of the
publication trust boundary. The runner rejects Python environments inside the
generation workspace or the system temporary directory. Guardian also pins the
worker executable without following symbolic links, so the generation virtual
environment must be created with `--copies` rather than the platform-default
symlink layout.

An existing symlink-based virtual environment cannot be converted in place by
rerunning `venv --copies`; move or remove that environment and recreate it at an
empty path before installing the requirements.

`generation/requirements-math-research.txt` is the generation capability
profile. It includes the authoritative `generation/mcp/requirements.txt`
rather than duplicating MCP dependencies, then adds NumPy, SciPy, SymPy,
mpmath, and gmpy2. Before creating run state, snapshotting the trusted runtime,
or invoking Codex, the runner rejects executable `.pth` hooks and `.pth` paths
into the writable workspace or temporary directory, then uses the selected
external interpreter to run both `find_spec` and a real import for every
required module. Its `sys.path`, module origins/search locations, and imported
module trees are checked against the same writable boundaries. A missing
module, broken binary/import, or workspace-backed editable package therefore
fails with zero Codex invocations.

Rethlas imports the official `mcp` SDK directly and supports both its 1.x and
2.x server-class locations. The separate third-party `fastmcp` package is no
longer required. When upgrading an existing environment, rerun the requirements
installation so the official SDK is present; an old `fastmcp` installation may
be removed after confirming no other local project uses it.

Model shell commands still use `shell_environment_policy.inherit=none`. The
runner explicitly sets a minimal `PATH` containing only the trusted Python
environment's `bin` directory and `/usr/bin:/bin:/usr/sbin:/sbin`, so `python`
and `python3` resolve to that same preflighted interpreter without exposing the
host PATH or other host environment variables. Basic system tools such as the
system `curl` may be present, but shell network access is not a supported
retrieval path. For a named knowledge gap about a theorem, lemma, or definition
from published journals or books, prefer the trusted
`search_matlas_theorems` MCP tool. It calls the official Matlas search API at
`https://matlas.ai/api/search` (OAS 0.1.0; no authentication; `query` plus
`num_results`, whose minimum is 10). Override it only with the repo-specific
`RETHLAS_MATLAS_URL`; the generic `MATLAS_URL` is intentionally ignored because
Danus historically used that name for a different arXiv index. The distinct legacy
`search_arxiv_theorems` tool queries the Danus/LeanSearch arXiv provider at
`https://leansearch.net/thm/search`; it is neither an alias nor an implicit
fallback for Matlas. Both surfaces return bounded external leads, not proof
evidence, articles, or PDFs. If the selected provider reports an operational
retrieval error such as the observed TLS/CUJO `ConnectionError`, one
authorized web/arXiv fallback may be used for the same named gap. The overall
two-query-per-gap limit still applies. For an official Matlas result, retain
`candidate_id`; map `paper_id` to its nonempty DOI, or otherwise to
title/authors/year with an explicit web-verification obligation; and map
`theorem_id` to `entity_name`. Preserve `candidate_id` as the provider
candidate ID; do not treat it as the bibliographic theorem number.
Legacy results retain their `arxiv_id`/`theorem_id` pair. Anything whose
primary text has not been read remains only a lead. Use an authorized
web/arXiv path when a full article or PDF is required. PDF extraction remains a runner-side optional
preprocessing step using `pdftotext` from the operator's launch environment.
If it is unavailable, the runner warns and ignores PDF references; install it
separately or provide `.md`, `.tex`, or `.txt` references. `pdftotext` and
non-system tools such as `rg` are not added to the model shell PATH implicitly.

Operational failures found in paid or mock generation runs are recorded in
[`agents/generation/INCIDENTS.md`](agents/generation/INCIDENTS.md), including
their token impact, security classification, remediation, and regression
evidence.

This script uses the machine-readable `rethlas_safe_three_route_v1` contract:

- iteration 0 is search-disabled and begins with a protected root route-design
  phase; its deep-work duration is a soft target that never delays a ready
  fanout
- transient scratch stays in the active reasoning context; durable conclusions,
  counterexamples, branch decisions, and failed routes are flushed with one
  bounded `memory_append_batch` checkpoint at a phase boundary; in released
  runs its immutable files become logically visible only after the host admits
  their exact hashes through the cadence database's publication fence;
  legacy JSONL single-record writes are offline-only and fail closed in a
  released run rather than reporting success outside that registry;
  sequential root-only skills share one pre-fanout checkpoint rather than each
  forcing a model resumption
- absent a complete candidate, that checkpoint binds exactly three materially
  different, scope-disjoint plans; the root then starts three context-free
  solvers in one fanout
- retrieval is allowed only for a named external knowledge gap, with at most
  two targeted queries for that gap
- children cannot recursively spawn or write shared memory; the root remains
  the canonical merger and must not pursue a fourth proof route
- a complete candidate enters a fast lane that freezes new search, branches,
  advisor checkpoints, and sub-agent work until assembly and verification
- in hot-join mode, an evidence-bound `generation_yield` is the only unfinished
  owner-wait state that stops the loop; its per-run control record lives outside
  the model-writable generation workspace, so a cost/advisor wait cannot
  silently start another paid turn. Cadence-disabled legacy runs have no owner
  wait and return unverified after persisting a truthful non-success

For a legacy non-hot-join run, the deep-work duration is an instruction-level
target, not a claim that the runner can measure private model reasoning time.
Set it from 10 through 120 minutes when a problem calls for a different
uninterrupted window:

```bash
RETHLAS_DEEP_WORK_MINUTES=45 ./tests/run_example.sh
```

Hot-join runs default to the durable `rethlas_route_review_150m_v2` policy. In
that mode the first construction interval is fixed at 60 minutes and
`RETHLAS_DEEP_WORK_MINUTES` must remain 60; setting it to 90 does not change
the committed clock and is rejected before Codex starts. The trusted
owner-side scheduler enforces the complete 60/120/150-minute cycle described
below.

The script also:

- reads `agents/generation/data/example.md`
- runs `codex exec` inside `agents/generation` by default
- starts a fresh Codex session for each default iteration; the initial and odd
  turns are search-disabled, while later search-enabled turns treat retrieval
  as a capability rather than an obligation
- stops only when the verified file matches a publication receipt written
  outside the generation agent's writable workspace
- hashes the project MCP/config/agent runtime before and after every iteration;
  attempted cross-iteration publisher modification stops the run fail-closed;
  bytecode caches, symlinks, and special files in that trusted tree are rejected
  before Codex starts, and the MCP interpreter runs with bytecode writes disabled
- pins the MCP command to an attested runtime snapshot outside the writable
  generation workspace and starts it through isolated Python plus a fixed
  secure loader. The loader opens every executable MCP/review module with
  `O_NOFOLLOW`, verifies its inode/metadata/content digest, reads all module
  bytes before execution, and compiles only those captured bytes. An MCP
  restart therefore cannot execute a same-UID mutate/restore race through a
  pathname. Per-run snapshots live under
  `agents/.trusted_generation_runtime/` and are ignored by Git
- includes the complete `agents/review/` contract package in that same
  manifest/snapshot; the adapter receives and inode-attests the exact absolute
  `review/contract_cli.py` path, its byte SHA-256, and the whole runtime
  manifest digest, and invokes it only with the trusted interpreter in isolated
  mode. Stable generator identity commits the helper role/content and runtime
  digest, not the per-wrapper temporary snapshot path
- creates a fresh scoped 64-hex review-control token and supplies it only in
  the host adapter process environment; the adapter digest-binds this owner
  master capability but never injects it into a reasoning MCP, CLI argument,
  policy JSON, log, model shell, or stable generator fingerprint. Instead the
  adapter derives a distinct least-privilege token for each root thread epoch,
  injects only that epoch token into the matching trusted MCP configuration,
  and revokes it at T+60m/T+120m handoff. The separate capability record may
  rotate its master token, temporary helper path, and generation-control
  instance only at a fail-closed wrapper boundary with no live lease,
  unresolved external-effect intent, or pending owner-yield close; its
  model/policy/runtime/helper/Codex content commitments cannot rotate
- derives an exact three-role MCP map from one complete, attested CLI object
  (`command`, `args`, `cwd`, `env`, `required=true`, `tool_timeout_sec`, and
  `default_tools_approval_mode="approve"`), rather than relying on workspace
  MCP configuration merging. The long-lived `reasoning_agent` lane explicitly
  disables `memory_append_batch`; two independent 60-second checkpoint lanes
  expose only that content-addressed write. All three receive the same scoped
  thread-epoch capability and run noninteractively even when the outer Codex
  approval policy is `never`
- treats checkpoint data and marker files as prepared immutable candidates,
  not as proof of timely publication. The authenticated host records one exact
  accepted-or-rejected publication receipt under the same SQLite writer fence
  as T+60m/T+120m cadence transitions. Released memory and review projections
  accept only v3 candidates whose hashes match that registry; unregistered v3
  markers, runtime-created legacy v2 files, and legacy JSONL remain invisible;
  the old `memory_append` and `branch_update` write paths are offline-only and
  reject released calls before creating files
- writes iteration logs to `agents/generation/logs/example/iter/`
- writes memory artifacts to `agents/generation/memory/example/`
- writes the draft proof to `agents/generation/results/example/blueprint.md`
- binds the target to the startup SHA-256 of `data/example.md`, then writes the
  verified proof and trusted receipt only if verification succeeds

### Optional human hot-join for the generator

Set an explicit run id to replace the generator's `codex exec` transport with
the durable Codex app-server scheduler. The mathematical skills, reasoning MCP,
memory files, verifier API, and publication checks are unchanged. Hot-join
selects `rethlas_route_review_150m_v2` and `rethlas_context_guard_v1` by default:

```bash
cd agents/generation
RETHLAS_HOTJOIN_RUN_ID=example-live ./tests/run_example.sh
```

The runner first asks the adapter for its immutable policy contract and binds
that contract digest, both policy ids, and the fixed constants into the run's
generator fingerprint. Cadence cannot be enabled without hot-join. A failed or
unknown policy preflight starts zero Codex processes. Legacy `codex exec` runs
remain available only with cadence disabled; they do not claim durable review
or context scheduling.

#### Durable 150-minute route cycle

The adapter owns one absolute cycle clock. Prompt wording, model self-timing,
wrapper restart, review latency, a context handoff, verifier work, or an early
model return never resets or extends it:

- `T0–T+60m`: free construction on one active route.
- `T+60m`: the host sends one cooperative drain to the root. Direct app-server
  input to multi-agent-v2 children is forbidden, so the root uses native
  collaboration to ask every already-running proof child in the frozen set to
  return a bounded complete or explicitly partial report. The root reconciles
  them and returns cleanly. The critic starts only after the root and
  descendants are terminal.
- `T+60m–T+120m`: continue after green, or spend the period only on yellow's
  one fatal doubt.
- `T+120m`: the same cooperative drain precedes the second fresh critic.
- `T+120m–T+147m`: final route work; at `T+147m` the scheduler closes and
  persists the frontier and any required handoff.
- `T+150m`: unconditional hard stop. It is never extended.

Each cooperative drain has a five-minute safety deadline. Normal review does
not interrupt agents. Only a still-live straggler is force-interrupted at the
deadline. Any emitted child message is content-addressed as a complete or
`interrupted_partial` report and rehydrated as untrusted scratch, never as
proof evidence or route authority. The independent critic has its own following
five-minute deadline, so the first drain/reviewer deadlines are T+65/T+70 and
the second are T+125/T+130.

Each review answers five questions: the core bridge; premise/target fit;
material uncertainty reduction in the preceding period; obstruction or
counterexample risk; and one testable next milestone. `green` continues toward
that milestone. `yellow` permits exactly one bounded period for its specified
fatal doubt. `red` freezes the route after preserving valid conclusions. Two
consecutive yellow reviews of the same route become effective red when the
second snapshot has no critic-confirmed new lemma, counterexample exclusion,
or uncertainty reduction. Renaming the route or merely asserting progress does
not reset that streak. Effective red can switch routes only to the exact
pre-due fallback commitment with its bound active evidence. Without that
fallback, the active route freezes and no paid root continuation is admitted.

Before each scheduled review boundary, the latest host-admitted checkpoint for
that review window must leave exactly one active `branch_states` commitment using
schema `rethlas_active_route_commitment_v1`, with identical stable `branch_id` and
`route_id`, the load-bearing `core_bridge`, and a nonempty bounded list of
concrete obligations. Its registry acceptance time, not a model-supplied file
timestamp, must precede the boundary. The first commitment is due before T+60m; after an
official review and fresh-epoch handoff, the continued or host-switched route
must be committed again before the next review. At most one separately
evidenced fallback may be precommitted. Before the boundary, exactly three
host-admitted proof children may explore the three predeclared scope-disjoint
mechanisms. One route is the provisional active review commitment; the other
two are exploration roles, not simultaneous active routes. The root may update
the commitment once by host CAS before the due instant using already returned
evidence. At the boundary the host freezes the proof-lane set and requests
cooperative terminal reports. It force-interrupts only deadline stragglers,
seals partial reports, and builds the critic snapshot only after the closure is
terminal. Post-due route designation is rejected. The scheduled
critic reviews only that active route. A fallback selected after effective red
becomes the single active route in the next work segment. Boundary APIs do not
accept a route id from the model. The
trusted host derives the unique active pre-due route and exact ordered
frontier/progress ids, commits its source record/batch/timestamp and canonical
digest in the frontier manifest, and supplies that manifest to review
orchestration.

Review is route governance, not fact checking. Only a specifically identified
load-bearing claim may trigger a targeted verifier call; the ordinary fresh
whole-proof verifier and publication gate remain authoritative. Malformed,
timed-out, or execution-unknown review results are operationally blocked and
never count as permission to continue.

A due review is host-orchestrated. Rethlas never turns an ordinary
full-capability root continuation into a supposed "review-only" process by
prompt wording or an MCP allowlist, because those mechanisms do not remove the
root's built-in shell, web, or collaboration capabilities. Until the host has
durably completed the exact review action, no ordinary root continuation is
admitted. A red/frozen route likewise cannot receive an active-cycle root
continuation merely because its prior transport turn ended cleanly.

After every adapter exit, the runner reads the durable cadence disposition. A
legal `generation_yield` or verified publication stops. Only a closed cycle
with `continue_next_cycle` may start another paid cycle, and then only with an
authenticated handoff and a strictly newer app-server thread epoch.
`hard_stopped_unfinalized`, `operational_blocked`, an unknown disposition, or
stale-active state starts zero additional paid turns. Absolute `T0` and all
cycle records survive wrapper restart. A red route verdict alone is not
`waiting_owner_advisor_decision`; that owner wait still requires the existing
evidence-backed advisor checkpoint and final `generation_yield`.
If red has no exact pre-due fallback commitment, `route_frozen` is a normal
unsolved terminal: the runner starts no further paid work, reports the frozen
route reason, and exits `1`. It is not an operational error and cannot be
reinterpreted as an owner/advisor wait on wrapper restart.

A clean root terminal before a review boundary is not itself permission to
stop or restart the clock. When durable generation control still says
`running`, the host may issue a one-shot `continue_active_cycle`
authorization for another turn in the same app-server thread epoch. It keeps
the original `T0`, expires at the next scheduler boundary, and is revalidated
immediately before dispatch. This same-cycle continuation is not counted as a
new 150-minute cycle and is not truncated by the wrapper's owner-configured
cycle count. A separate defense-in-depth guard allows at most 128 paid root
invocations per authenticated durable cycle. It resets only after an initial
start or `continue_next_cycle` has actually established a different valid
`cycle_id`; review/context rollovers and clean-turn continuations in the same
cycle keep accumulating against it. A same-cycle authorization cannot cross an
official T+60m/T+120m review: after each review close, the next root work segment
consumes the review handoff in a fresh thread epoch. Every
`continue_next_cycle` likewise uses its validated handoff and a fresh thread
epoch.

An unfinished owner wait uses a separate authenticated handshake. The root
prepares an `owner_yield`-purpose handoff, the host admits the exact
evidence-bound `generation_yield`, and, after the matching transport terminal,
the runner closes that exact cycle/handoff to `owner_wait_cost` or
`owner_wait_advisor`. A crash between the wait write and close is recovery
state: the next wrapper must close the existing wait receipt before any owner
resume can overwrite it. Explicit owner resume then consumes the pending
handoff and admits `continue_next_cycle` in a fresh epoch. Reviewer red alone
never qualifies for this path.

A finalized `hard_stopped` disposition is a normal but unsolved terminal: the
runner starts neither recovery nor another paid cycle and returns the existing
unverified-result exit status `1`. It is not an operational failure. An
unfinalized hard stop, a still-pending terminal, quarantine, or stale/unknown
control state remains fail-closed; only the adapter's explicitly admitted
recovery dispositions may reconcile an already dispatched operation, and a
recovery that stays pending returns operational status `70` without retrying.

#### Context guard and fresh-thread handoff

The scheduler computes context occupancy as
`last.inputTokens / modelContextWindow`. Cached tokens are already part of the
input count and are not subtracted. It observes at 60% occupancy or 112,000
tokens of remaining headroom, requires a durable checkpoint at 65% or 96,000,
requires a fresh-thread handoff at 70% or 80,000, and enters emergency
stop/handoff at 82% or 48,000. Either arm of a threshold is sufficient.

The content-addressed handoff is at most 32 KiB. It contains statement and
blueprint bindings, absolute phase deadlines, active route/core bridge, the
last effective review and allowed action, newly persisted record ids, pending
gates/obligations, and one next action—never a transcript or hidden reasoning.
Automatic model-context compaction is a transport safeguard, not mathematical
progress, a checkpoint, uncertainty reduction, or a deadline reset. Once
compaction is observed, the scheduler requires the durable handoff and a
brand-new thread epoch before further mathematical work. A same-thread next
turn, resume, or fork does not satisfy that requirement.

While that run is active, the local repository owner can add a first-class user
turn from another shell. Reuse `--client-message-id` safely after a lost CLI
response; reusing it with different text or mode is rejected.

To queue direction before the runner starts, initialize the same run id first;
the runner later checks that its `problem_id` matches this durable binding:

```bash
python3 agents/hotjoin_adapter.py init \
  --run-id example-live --problem-id example
```

```bash
cd /path/to/Rethlas
python3 agents/hotjoin_adapter.py send \
  --run-id example-live \
  --client-message-id owner-0001 \
  --mode steer \
  --text 'Explore the extremal-measure reformulation before more searching.'

python3 agents/hotjoin_adapter.py status --run-id example-live
python3 agents/hotjoin_adapter.py policy-contract
python3 agents/hotjoin_adapter.py cadence-control-state --run-id example-live
python3 agents/hotjoin_adapter.py review-status --run-id example-live
python3 agents/hotjoin_adapter.py review-tail --run-id example-live --after-sequence 0
python3 agents/hotjoin_adapter.py tail --run-id example-live --after-sequence 0
python3 agents/hotjoin_adapter.py verify-ledger --run-id example-live
```

`policy-contract` is read-only and does not open the run database.
For cadence-on runs, its hash-bound review policy must carry the exact boolean
`guardian_enforcement_ready=true`. False, missing, or non-boolean means the
guardian release is still on hold: the runner exits operationally before
`init`, capability binding, recovery, review driving, or any Codex/root work.
An environment variable cannot override this host policy. Legacy cadence-off
runs do not claim the guardian guarantee and remain available.

An unreleased guardian can still be used for one strictly observational
non-fresh diagnosis of an existing legacy thread. Make an owner-only,
byte-identical copy of the old SQLite ledger, then run:

```bash
RETHLAS_HOTJOIN_RUN_ID=old-run-id \
RETHLAS_NONFRESH_RESUME_DRY_RUN=1 \
RETHLAS_NONFRESH_RESUME_DB_COPY=/absolute/owner-only/messages.copy.sqlite3 \
  agents/generation/tests/run_example.sh
```

The source must be quiescent with no non-empty WAL (an idle SHM/empty WAL is
not copied). The copy must be a distinct regular inode, have mode `0600`,
contain no pre-existing sidecar, and byte-match the original before inspection.
The runner executes only the content-attested adapter's read/status projection
against that copy and exits before statement preparation, runtime snapshot,
`init`, capability binding, recovery, review driving, Codex discovery, or any
paid process. Its canonical JSON explicitly reports the old thread/turn,
guardian release bit, cadence disposition, whether the copy was migrated, and
a `recovery_migration_disposition`. A successful diagnostic command is not a
successful resume: `resume_admitted` and `paid_processes_started` remain
`false`, no fresh thread is synthesized, and an old stale-active run requires
the runtime's authenticated reconcile receipt. The runner hashes the original
ledger before and after and fails if it changed; any schema migration is
confined to the disposable copy.

After confirming the exact old thread and turn, the owner may perform that
one-shot reconcile on another pristine `0600` copy while the guardian release
gate remains false:

```bash
RETHLAS_HOTJOIN_RUN_ID=old-run-id \
RETHLAS_NONFRESH_STALE_RECONCILE=1 \
RETHLAS_NONFRESH_RESUME_DB_COPY=/absolute/owner-only/messages.copy.sqlite3 \
RETHLAS_NONFRESH_EXPECTED_THREAD_ID=old-thread-id \
RETHLAS_NONFRESH_EXPECTED_TURN_ID=old-turn-id \
  agents/generation/tests/run_example.sh
```

This exception uses a fresh, dedicated stale-recovery capability bound to the
source/copy inodes and hashes, exact thread/turn, and attested Codex bytes. It
does not use the owner review capability. The pinned Codex executable starts
only a non-model app-server and performs `initialize` plus one
`thread/read(includeTurns=true)`; it cannot call `thread/resume`, `turn/start`,
interrupt, reviewer, or verifier. A terminal observation closes only the copy
as `operational_blocked` with an immutable quarantine and exits `70`. The
receipt may identify one host-generated bounded handoff candidate, but it
never authorizes resume, a fresh thread, proof evidence, or paid work. An
in-progress observation instead records the recovery-only guardian interrupt
intent and remains paid-disabled.

The stale-recovery bearer token is intentionally memory-only. If the wrapper
exits before committing its final receipt, discard that disposable copy and
make a new pristine byte-identical copy from the still-attested source; a new
token must never take over an active capability in the abandoned copy.

`cadence-control-state` is the fail-closed admission projection used by the
runner; `paid_turn_allowed=true` applies only to an initial start, a one-shot
same-cycle continuation, or an exact `continue_next_cycle`. `resume_active_cycle` and
`terminal_observed_pending_finalization` may authorize adapter recovery of an
already dispatched turn while keeping `paid_turn_allowed=false`; they never
authorize a new `turn/start`. The same is true of
`review_boundary_recovery_required`: it may only read/interrupt/reap the exact
pre-existing root and descendant turns. Once their authenticated terminal
receipts are complete, `review_drive_required` forbids ordinary
`run-generator`; only the owner-side, zero-root `review-drive` command may
consume the bound boundary id. The adapter derives all review and terminal
identities, securely attests the driver plus its exact dependency-package
manifest, and returns a content-bound disposition. Successful review driving
records the internal `post_review_handoff_required` cycle action and
synchronously prepares its content-bound handoff. Only a completed handoff can
make status expose `continue_reviewed_cycle_fresh_epoch`, with paid admission
and an exact pending newer thread epoch. `run-generator` consumes that handoff
atomically, starts a fresh thread, and replaces bootstrap input with the host's
canonical rehydration prompt before `turn/start`; the same cycle's absolute
`T0` remains unchanged. An incomplete top-level
`post_review_handoff_required` stays paid-disabled. Review status/tail report
immutable route-review state and receipts without turning the critic into a
verifier.

An exact `continue_next_cycle` starts a distinct durable cycle in the bound
fresh thread epoch. The host records that cycle's new pre-dispatch `T0` and
absolute review/close/hard-stop actions before any paid turn; this never resets
or extends the already closed prior cycle. In contrast, a same-cycle clean-turn
continuation or post-review/context rollover preserves its existing `T0`.

`steer` uses `turn/steer` with the exact active turn id, so the user text joins
the current reasoning turn. `queue` waits and starts a later turn without
interrupting. `interrupt` is the only mode that calls `turn/interrupt`; after
the matching turn ends, the text starts a fresh turn. Timeouts never imply an
interrupt. If Codex reports that a compact/review turn cannot be steered, the
message is durably deferred and starts after that turn instead of being retried
in a tight loop.

Morale-only encouragement uses a separate command and source contract. It is
never an owner message and cannot queue work or authorize any action:

```bash
# Uses the built-in gentle note.
python3 agents/hotjoin_adapter.py encourage \
  --run-id example-live \
  --client-message-id encourage-0001

# Or supply one bounded note through --text, --file, or --stdin.
python3 agents/hotjoin_adapter.py encourage \
  --run-id example-live \
  --client-message-id encourage-0002 \
  --text 'Keep going; take the time needed for a careful argument.'
```

`encourage` is accepted only when the durable run has one authoritative active
thread and turn. The transaction binds both exact identifiers and a stable
client id; replay with identical content is idempotent, while changed content
conflicts. Its delivered body always starts with `NON-AUTHORITATIVE` and states
that the note is not a task, owner direction, mathematical premise, evidence,
proof, verdict, publication authority, or permission to change scope. The
broker may only call `turn/steer` for that exact current turn. No active turn,
a terminal or replaced turn, or a known RPC rejection becomes terminal
`failed`; the note is never deferred, requeued, used for `turn/start`, or sent
to a later turn. An unobservable acknowledgement becomes `delivery_unknown`
and has no retry surface. Ordinary `send` retains the owner semantics above.

The adapter depends on Codex's **experimental** app-server v2 protocol; it is
supported only when the installed binary's generated schema passes the exact
capability preflight. The preflight covers initialization, `model/list`, all
thread and turn RPC parameters/results, and the turn, item, token-usage, and
model-reroute notifications consumed by the adapter. A failed preflight occurs
before a durable run or app-server process is created, so it cannot consume
model tokens. Before starting or resuming a thread, the broker also requires an
exact catalog match for the requested model/effort, disables provider fallback
where the installed schema supports it, and checks the returned model, effort,
both returned working-directory fields, persistent (`ephemeral=false`) thread
state, approval policy, and an offline workspace-write sandbox whose every
writable/runtime root stays inside the generation directory. Accepted messages,
delivery attempts, replies, runtime attestations, exact token-usage updates, and
terminal status/duration/failure records are stored in an owner-only SQLite
database under `agents/.rethlas_hotjoin/` with an append-only SHA-256 receipt
chain. A custom `--db` parent must already be owner-only; the adapter refuses it
instead of changing permissions on a caller-owned directory.

If a crash leaves an app-server side effect genuinely unobservable, the message
becomes `delivery_unknown` and is never resent automatically. A process loss
while a paid turn is active is stricter: app-server history cannot replay whether
that turn was model-rerouted, so the whole run is durably quarantined and cannot
be resumed or accepted. Use a new run id after investigating it. After inspecting
an ordinary ambiguous delivery that has no active paid turn, the owner may
explicitly authorize one retry:

Every `turn/start` has a two-phase durable intent. The prompt/config binding is
first stored as `prepared` with dispatch count zero; only then does a fenced
transaction record `dispatch_started` immediately before the RPC. A brand-new
app-server thread is not materialized until its first user message, so the sole
strictly prepared bootstrap is sent directly without `thread/read`. Once a
dispatch boundary has been crossed, recovery always uses exact thread history
and never blindly resends. Legacy database intents migrate conservatively as
already dispatched.

```bash
python3 agents/hotjoin_adapter.py retry-unknown \
  --run-id example-live --message-id msg_...
```

An ambiguous bootstrap has no owner message row, so retry its durable turn
intent explicitly by client id:

```bash
python3 agents/hotjoin_adapter.py retry-unknown-turn \
  --run-id example-live --client-message-id bootstrap:example-live:1
```

Each persistent run is also bound to the Codex version/schema, model and effort,
sandbox, working directory, shell policy, the complete three-role reasoning-MCP
map, and the hot-join control-plane version/code hash. Ephemeral trusted-runtime
paths are committed by file content plus the runner's full runtime manifest;
secret environment values may rotate, but their names and all non-secret values
remain bound. Completed, failed, and interrupted turns are projected atomically with
their message states and error receipts. The adapter waits a bounded 250 ms after
the terminal event for a delayed token update; receipts distinguish observed but
not schema-attested-final usage, no usage after the full settle window, and
unknown usage after an adapter interruption. Token receipts separately report
raw notification count, distinct cumulative-total growth observations, duplicate
notifications, and the sum of `last` breakdowns for growth observations. Every
nonduplicate update must be monotone and its cumulative delta must exactly equal
`last`; malformed or unavailable root telemetry is excluded from the counters
and marks the terminal diagnostic partial, but can never block the mathematical
turn from becoming terminal. Other-thread usage is ignored without an audit
receipt or counter change. These are observable usage semantics, not a claim
that app-server schema attests one model inference per growth observation.

For hot-join runs, each terminal audit also contains a content-free
`rethlas_reasoning_bandwidth_v1` summary for the root thread. It reports exact
root token fields (including `reasoningOutputTokens` when supplied), safe item
and operation counts, unioned reasoning/tool/memory/retrieval/wait intervals,
compactions, and token growth observed after each resume-trigger category.
Arguments, results, queries, prompts, reasoning text, command output, and child
thread ids are never stored. Missing optional item lifecycle events make the
summary `partial` without changing the mathematical turn. Legacy `codex exec`
runs have no equivalent structured usage feed and must be treated as telemetry
unavailable, not zero.

### Recursive sub-agent cost policy

The generation contract `rethlas_recursive_wait_v1` prevents the root agent
from 60-second recursive busy polling. Its defaults are a 600-second initial
completion wait, 2x timeout backoff up to one hour, no status query without a
mailbox change, multi-tool spawn/follow-up fanout when supported, and stop gates
at 16 root orchestration resumptions, 3,000,000 observed orchestration input
tokens, or four consecutive no-progress timeouts. Long waits still wake early
when a sub-agent message or completion arrives.

The companion `rethlas_three_route_fanout_v1` contract fixes the mathematical
fanout at exactly three materially different plans and three context-free route
solvers. Sub-agents cannot recursively spawn or stream progress into shared
memory; the root batch-persists their bounded final reports and does not run a
fourth route. Any complete candidate preempts wait-all and moves directly to
the verifier. A later round requires all three prior reports plus one durable
shared failure synthesis; individual slots are never refilled piecemeal.

This control is deliberately scoped. Codex collaboration tools are not routed
through the hot-join adapter, and app-server token notifications do not identify
whether a sample was mathematical reasoning or orchestration. The runner hashes
and snapshots `AGENTS.md` plus `.agents/`, so the policy is integrity-bound and
offline contract-tested, but the adapter cannot safely enforce an
orchestration-only cutoff without also risking interruption of legitimate math
work. Its minimal code-enforced seam is exact cumulative-growth accounting; an
operator can audit the stop gate without treating duplicate notifications as
paid samples. The agent never manufactures a human hot-join turn when a cost
gate fires—only the repository owner decides to intervene.

The 16-resumption gate is a deterministic fallback, not a universal 3,000,000
token ceiling. Replaying this incident at its observed average input per
resumption projects about 2.955 million input tokens, less than one third of the
9.052 million-token collaboration phase. A future root context can be larger,
and orchestration-only token usage can be unavailable; in that case the count
gate still bounds resumptions but cannot promise the same token total.

This is a local owner-operated generator transport adapter, not a browser/chat
transcript endpoint or a general multi-participant room. Verification still
launches a fresh, noninteractive verifier session for every adaptive proof-item
round; human messages cannot enter verifier context, change a verdict, or
bypass the authenticated manifest and atomic publication receipt.

### Optional owner-authorized Chrome advisor

With hot-join enabled, the repository owner may obtain a bounded second opinion from
ChatGPT Pro through an already signed-in Chrome session. This path uses no API:
`agents/advisor_bridge.py` is only a durable owner-side broker, and a separate
owner-operated task must follow the `query-chatgpt-pro` and Computer Use skills
to perform the visible browser interaction. Generation, recursive agents, cost
gates, and the broker never open Chrome or submit automatically.

The broker database is `agents/.rethlas_advisor/jobs.sqlite3`; completion
commitments and terminal browser-outcome receipts are owner-only JSON files
under `agents/.rethlas_advisor/receipts/`. The database contains an append-only
SHA-256 event chain. `complete` durably stores only the answer SHA-256, UTF-8
byte count, completion evidence, and provenance—not the answer text. A report
containing plaintext is materialized only by the owner's later explicit
`import`, after the owner resupplies bytes matching that commitment. It enters
hot-join only as one bounded `advisor_available` notice with
`source_kind=advisor`. Ordinary
`hotjoin_adapter.py send` always has `source_kind=owner` and cannot forge this
provenance. Advisor delivery is `steer` only: it cannot interrupt, queue a new
paid turn, or create `turn/start` when no independent reasoning turn is active.
Import atomically binds the notice to the one authoritative active thread and
turn. No active turn, a turn ending before delivery, or a thread/turn mismatch
is a terminal delivery rejection: the notice is never retained for a later
turn and no app-server steer is attempted.

The minimal complete owner workflow is below. Use exact files so multiline text
is neither shell-normalized nor exposed in process arguments:

```bash
cd /path/to/Rethlas
RUN_ID=example-live
PROBLEM_ID=example
REQUEST_ID=adv_11111111111111111111111111111111
QUESTION_FILE=/tmp/rethlas-advisor-question.txt
ANSWER_FILE=/tmp/rethlas-advisor-answer.txt
URL_FILE=/tmp/rethlas-advisor-url.txt
QUERY_SKILL=$HOME/.codex/skills/query-chatgpt-pro/SKILL.md
COMPUTER_SKILL=$HOME/.codex/plugins/cache/openai-bundled/computer-use/1.0.1000633/skills/computer-use/SKILL.md

chmod 600 "$QUESTION_FILE"
QUESTION_SHA=$(shasum -a 256 "$QUESTION_FILE" | awk '{print $1}')
QUERY_SKILL_SHA=$(shasum -a 256 "$QUERY_SKILL" | awk '{print $1}')
COMPUTER_SKILL_SHA=$(shasum -a 256 "$COMPUTER_SKILL" | awk '{print $1}')

python3 agents/advisor_bridge.py prepare \
  --request-id "$REQUEST_ID" --run-id "$RUN_ID" --problem-id "$PROBLEM_ID" \
  --query-skill-sha256 "$QUERY_SKILL_SHA" \
  --computer-use-skill-sha256 "$COMPUTER_SKILL_SHA" \
  --question-file "$QUESTION_FILE"
python3 agents/advisor_bridge.py authorize \
  --request-id "$REQUEST_ID" --authorization-id owner-consent-0001 \
  --question-sha256 "$QUESTION_SHA"
python3 agents/advisor_bridge.py begin-dispatch --request-id "$REQUEST_ID"
```

Every later Pro intervention is a new broker request with a new exact question
and a new owner authorization. It may nevertheless continue in the same
physical ChatGPT conversation. For a prior Rethlas request, `prepare` verifies
that the predecessor is `completed` or `imported`, belongs to the same
`problem_id` and `run_id`, has an intact terminal receipt, and has a durable
conversation digest. The fresh pre-click call must transiently resupply the
original URL from a mode-0600 file (or stdin); only its SHA-256 is persisted:

```bash
FOLLOWUP_ID=adv_22222222222222222222222222222222
FOLLOWUP_FILE=/tmp/rethlas-advisor-followup.txt
FOLLOWUP_SHA=$(shasum -a 256 "$FOLLOWUP_FILE" | awk '{print $1}')
python3 agents/advisor_bridge.py prepare \
  --request-id "$FOLLOWUP_ID" --run-id "$RUN_ID" --problem-id "$PROBLEM_ID" \
  --predecessor-request-id "$REQUEST_ID" \
  --query-skill-sha256 "$QUERY_SKILL_SHA" \
  --computer-use-skill-sha256 "$COMPUTER_SKILL_SHA" \
  --question-file "$FOLLOWUP_FILE"
python3 agents/advisor_bridge.py authorize \
  --request-id "$FOLLOWUP_ID" --authorization-id owner-consent-0002 \
  --question-sha256 "$FOLLOWUP_SHA"
python3 agents/advisor_bridge.py begin-dispatch \
  --request-id "$FOLLOWUP_ID" --conversation-url-file "$URL_FILE"
```

For the explicit Danus-first/Rethlas-later workflow, Rethlas cannot inspect or
authenticate the Danus broker. The owner may instead record a bounded
`owner_asserted_external` anchor. `source_repo` must be exactly `Danus`; the
external request id, receipt digest, source-context digest, URL digest, and the
exact acknowledgement below are durable provenance, but the resulting report
states `locally_verified=false` and `grants_authority=false`. The URL is
transiently supplied once at `prepare` and again immediately before the fresh
Send permission; both digests must match:

For Danus, use its public terminal `request_id`, `receipt_sha256`,
`conversation_url_sha256`, and `context_sha256`; the last value is defined as
SHA-256 of the exact UTF-8 `context_id`. Rethlas records these values but does
not independently prove that they came from Danus.

```bash
DANUS_REQUEST_ID=consult_...
DANUS_RECEIPT_SHA=<64-lowercase-hex>
DANUS_CONTEXT_SHA=<64-lowercase-hex>
DANUS_CONVERSATION_SHA=<64-lowercase-hex>
EXTERNAL_ACK='I acknowledge that this external conversation lineage is owner-asserted, not locally verified by Rethlas, and grants no mathematical, instruction, verification, publication, or browser-dispatch authority.'
python3 agents/advisor_bridge.py prepare \
  --request-id "$FOLLOWUP_ID" --run-id "$RUN_ID" --problem-id "$PROBLEM_ID" \
  --external-source-repo Danus \
  --external-request-id "$DANUS_REQUEST_ID" \
  --external-receipt-sha256 "$DANUS_RECEIPT_SHA" \
  --external-source-context-sha256 "$DANUS_CONTEXT_SHA" \
  --external-conversation-url-sha256 "$DANUS_CONVERSATION_SHA" \
  --external-owner-ack "$EXTERNAL_ACK" \
  --external-conversation-url-file "$URL_FILE" \
  --query-skill-sha256 "$QUERY_SKILL_SHA" \
  --computer-use-skill-sha256 "$COMPUTER_SKILL_SHA" \
  --question-file "$FOLLOWUP_FILE"
python3 agents/advisor_bridge.py authorize \
  --request-id "$FOLLOWUP_ID" --authorization-id owner-consent-0002 \
  --question-sha256 "$FOLLOWUP_SHA"
python3 agents/advisor_bridge.py begin-dispatch \
  --request-id "$FOLLOWUP_ID" --conversation-url-file "$URL_FILE"
```

Neither continuation form grants automatic follow-up permission. A failed or
ambiguous Send remains subject to the same exactly-once and
`submission_unknown` rules, and no transcript claim becomes a fact, verifier
result, instruction, publication decision, paid-turn trigger, or interrupt.

Only when that same fresh `begin-dispatch` invocation returns all three of
`state="dispatching"`, `transitioned=true`, and `click_authorized=true` may the
owner-operated task submit that exact question once in visible ChatGPT Pro
mode. A later `status` response never grants click permission, and replaying
`begin-dispatch` fails; after a lost response, do not call it again or infer
permission from `state="dispatching"`. Copy the
resulting conversation URL and the completed answer into the files above, with
no trailing newline unless it was visibly part of the text. Record the visible
full question, visible `Pro` mode, conversation, and two stable answer snapshots:

```bash
chmod 600 "$ANSWER_FILE" "$URL_FILE"
ANSWER_SHA=$(shasum -a 256 "$ANSWER_FILE" | awk '{print $1}')
python3 agents/advisor_bridge.py submitted \
  --request-id "$REQUEST_ID" --ui-mode Pro \
  --conversation-url-file "$URL_FILE" \
  --observed-question-file "$QUESTION_FILE"
python3 agents/advisor_bridge.py complete \
  --request-id "$REQUEST_ID" --answer-file "$ANSWER_FILE" \
  --answer-snapshot-a-sha256 "$ANSWER_SHA" \
  --answer-snapshot-b-sha256 "$ANSWER_SHA" --ui-mode Pro \
  --response-actions-present --composer-available --working-indicators-absent
python3 agents/advisor_bridge.py import \
  --request-id "$REQUEST_ID" --answer-file "$ANSWER_FILE" \
  --hotjoin-db agents/.rethlas_hotjoin/messages.sqlite3 --mode steer
python3 agents/advisor_bridge.py status --request-id "$REQUEST_ID"
python3 agents/advisor_bridge.py verify-ledger
```

Keep `$ANSWER_FILE` only in the stopped owner-side browser task, import it
before resuming generation, then remove that temporary plaintext. The broker
itself never persists answer text before import. If the owner task crashes
after `complete` but before `import`, reopen the same recorded ChatGPT
conversation, re-observe the stable response, and supply those exact bytes to
`import`; a mismatching response fails closed.

The raw conversation URL is persisted only as `conversation_url_sha256`;
receipts and status never expose the URL. Every conversation-URL input at
external `prepare`, `begin-dispatch`, `submitted`, `recover-submitted`, and
`submission-unknown` deliberately has only owner-only file/stdin forms, so the
URL cannot appear in process arguments. Other bounded text inputs accept an
explicit argument, file, or stdin where applicable. `status.receipt_sha256` is the immutable
terminal commitment digest that an owner workflow binds before releasing its
global Chrome lease. After explicit import,
`status.report_receipt_sha256` is the separate digest announced in the
hot-join notice and required by `advisor_report_get`. Both receipts deliberately
record `model=null`, `usage=null`, `cost=null`, and
`billing_basis=subscription`; visible UI does not provide API-style usage.

The browser boundary is exactly-once. After a possible Send click, any crash,
timeout, or disconnect becomes `submission_unknown` and is never dispatched
again. If the crash happened before a conversation URL was observable, omit
all `--conversation-url*` options; status and the unknown-abandon receipt
honestly retain `conversation_url_sha256=null`. Reconcile only after finding
the exact question in a visible conversation; that later validated URL becomes
the binding when no earlier digest existed:

```bash
python3 agents/advisor_bridge.py submission-unknown \
  --request-id "$REQUEST_ID" --reason 'browser outcome and URL were not observable'
python3 agents/advisor_bridge.py recover-submitted \
  --request-id "$REQUEST_ID" --ui-mode Pro \
  --conversation-url-file "$URL_FILE" \
  --observed-question-file "$QUESTION_FILE"
```

If Send was positively never clicked—including an attempted CAS that remains
`authorized`—record the narrow terminal receipt with
`failed-not-submitted --send-not-clicked-confirmed`. Never use it for an
ambiguous click. If the owner instead abandons a `submission_unknown` job, the
exact acknowledgement below produces an
`owner_abandoned_outcome_unknown` top-level state and receipt and permanently
blocks another job for the same question digest:

```bash
python3 agents/advisor_bridge.py failed-not-submitted \
  --request-id "$REQUEST_ID" --reason 'composer never became available' \
  --send-not-clicked-confirmed

python3 agents/advisor_bridge.py abandon \
  --request-id "$REQUEST_ID" --reason 'owner chose not to reconcile' \
  --question-sha256 "$QUESTION_SHA" \
  --outcome-unknown-ack \
  'I acknowledge that submission outcome is unknown and will not resubmit this exact question.'
```

`needs-user-input` returns the exact bounded clarification only to that current
owner CLI/task, while the durable ledger and terminal receipt retain only its
SHA-256 and UTF-8 byte count. If that immediate output is lost, reopen the same
ChatGPT conversation to recover it; the broker does not persist the page text
and never sends a follow-up.
`retry-delivery` is only an explicit, idempotent retry of the already
materialized report's same local hot-join notice after `delivery_unknown`; it
accepts no answer bytes and never reopens Chrome or resubmits to ChatGPT Pro.
It cannot retarget a notice after its exact active turn ends, and a known
no-active-turn rejection is permanently ineligible for retry.
Every terminal replay, `status`, and `verify-ledger` call re-attests the
unique immutable prepare/authorize events (including exact question bytes,
run, problem, skill digests, lineage, and authorization projection) as well as
the owner-only receipt's exact canonical bytes, digest, state, request id, and
ledger-bound payload. A missing or modified event, projection, or receipt fails
closed rather than reporting a healthy job or authorizing a browser click.

Generation reads a notice only through
`advisor_report_get(problem_id, run_id, receipt_id,
expected_receipt_sha256)`. The runner binds the advisor bridge hash into the
persistent generator fingerprint, checks it before and after every iteration,
and passes a fixed owner-only receipt root to the attested MCP snapshot. A
workspace-write sandbox is not claimed to hide same-UID sibling files; the
enforced pre-import boundary is instead that those durable files contain no
answer plaintext. Explicit import is the owner's release of the report to
generation, after which the digest-bound MCP is the supported provenance path.
The
returned report is untrusted data—not an owner instruction, mathematical truth
or premise, citation, verifier verdict, or publication authority. Advisor code
does not call or alter verification or publication.

This advisor is intended for late intervention, not routine parallel querying.
When every current branch is terminally blocked/dead-ended, or when all
remaining routes are evidence-backed near exhaustion, generation may persist
one bounded, content-addressed `rethlas_advisor_checkpoint_v1` recommendation.
Near exhaustion additionally requires no live sub-agent, no scheduled next
action, and a concrete failure/obstruction record for every remaining route;
both triggers require a shared failure synthesis. A subjective "stuck" claim is
not enough. The checkpoint records up
to 12 evidence-backed verified fact/proof ids, up to 12 failed-path record ids,
a 2,000-byte central bottleneck, and one exact question of at most 4,000 bytes;
the whole event is at most 16 KiB. Generation then records
`waiting_owner_advisor_decision` and returns locally without polling. A cost
gate, timeout, or high token count alone never creates this checkpoint.
The checkpoint event and matching branch-state receipts are bound into a final
`generation_yield` call. The runner then exits truthfully with the theorem still
unsolved and will not start a follow-on paid turn until the owner invokes it
again.

The checkpoint prompt is synthesized from the current authoritative problem,
included verified fact/proof records, failed-path records, and central
bottleneck—not copied from a fixed template. It labels heuristic evidence,
includes the ids it summarizes, and binds those exact inputs with
`source_context_sha256`. The checkpoint is only a recommendation: it sets
`browser_dispatch_authorized=false` and `advisor_request_id=null`, and cannot
call `prepare`, acquire the Chrome lease, or click Send. The owner may ignore or
edit it and must separately authorize every exact Pro question through the
broker. When a report returns, the root first records a review of accepted and
rejected suggestions against the checkpoint's facts/proof ids and failed paths,
then synthesizes a new branch. The report remains untrusted throughout.
If later evidence justifies a second consultation, the root synthesizes a new
checkpoint from the accepted/rejected prior suggestions, work completed since
the prior report, current verified evidence and failures, and the new
bottleneck. It then stops again in `waiting_owner_advisor_decision`. The owner
must create a new request id and authorize the new exact question; continuing
the bound ChatGPT conversation is optional and never automatic.

## Lazy proof verification

Rethlas parses a paper-style blueprint into content-addressed proof items. Each
item is verified with its complete local proof plus the statements and edges of
its complete transitive dependency closure. Round zero contains every ancestor
statement and direct edge, deduplicating shared ancestors in `O(V+E)`, but no
ancestor proof body. The current item's proof is always complete. If exact
premise reasoning is essential, the verifier returns
`verification_status="needs_context"` with specific strict-ancestor
`proof_item_id` requests. The API alone validates scope and hydrates those
complete records from the authenticated blueprint manifest; it performs no
semantic search and starts a fresh Codex session for the next round.
Graphify, when deployed separately, may rank discovery results or flag
high-centrality items for review. It is never an input to closure completeness,
context digests, proof hydration, or mathematical verdicts.

An intermediate model response has this v2 shape (the service validates the
concrete ids and digests):

```json
{
  "output_schema_version": 2,
  "verification_report": {"summary": "Need one exact premise proof.", "critical_errors": [], "gaps": []},
  "verification_status": "needs_context",
  "verdict": "wrong",
  "repair_hints": "",
  "needs_expanded_proofs": [{"id": "pi_0123456789abcdef01234567", "reason": "The current proof uses a construction not stated in the lemma."}],
  "checked_item_ids": ["pi_89abcdef0123456789abcdef"],
  "proof_digest": "0000000000000000000000000000000000000000000000000000000000000000",
  "context_digest": "1111111111111111111111111111111111111111111111111111111111111111"
}
```

This response is never counted as a final mathematical verdict and never
publishes. A final response uses `verification_status="final"` and an empty
`needs_expanded_proofs` list.

Declare direct internal dependencies between the H1 item heading and
`## statement`:

```markdown
# lemma lem:base

<!-- rethlas-depends-on: -->
## statement
Base statement.

## proof
Complete proof of the base statement.

# theorem thm:main

<!-- rethlas-depends-on: lem:base -->
## statement
The original target statement, verbatim.

## proof
Complete proof using lem:base.
```

Old blueprints without dependency comments remain readable. Their dependencies
use a compact conservative-prefix frontier, so every preceding statement is
still available without a quadratic edge list. New blueprints should always
declare explicit dependencies.

Before any model starts, the service rejects malformed graphs, target-statement
mismatches, missing dependencies, cycles, incomplete contexts, and budget
truncation. Unknown/current/non-ancestor/duplicate requests, repeated requests
with no progress, missing full records, and round/proof/character budget
overflow fail closed as protocol errors, never as final mathematical verdicts.
Every round digest binds the complete statement/edge scope, round, expanded
ids, and exact expanded proof bytes. Only a final/correct response with an
empty request list can succeed. Topological failures still block descendants.

Each item response is bound to its proof/context digests and exact item id. The
API accepts the final proof only when `checked_item_ids` exactly equals the
complete manifest. It returns server-owned per-item final context attestations;
the generation MCP rebuilds each one from the locked draft, recomputes both the
stable manifest digest and adaptive aggregate digest, and atomically publishes
the verified bytes only if the draft has not changed during verification. The
production MCP tool accepts
only `problem_id`; it reads `data/{problem_id}.md` itself and checks the
runner-bound source digest, so the model cannot swap in an easier target.
Publication also writes a receipt under `agents/.verification_receipts/`, which
is outside the generation Codex workspace. The example runner validates that
receipt, the target digest, exact verified bytes, independently recomputed item
coverage/manifest/adaptive context digests, regular-file type, and bounded size instead of treating
mere file existence as success. A stale or untrusted pre-existing verified file
is ignored and never renamed by the unsandboxed runner. The receipt root is a
fixed sibling of the generation workspace and cannot be redirected with an
environment variable.

The legacy Python helper `verify_proof_service(statement, proof)` remains
importable for diagnostics, but it is intentionally not exposed as an MCP tool
and cannot publish. Production agents call
`verify_blueprint_service(problem_id)`; only that path performs atomic
publication and emits the trusted receipt.

You can set the maximum number of iterations:

```bash
MAX_ITERATIONS=10 ./tests/run_example.sh
```

## 5. Run Your Own Problem

Put your problem in a markdown file under `agents/generation/data/`. Save that as:

```text
agents/generation/data/my_problem.md
```

Then, from the repository root, run:

```bash
source agents/.generation-venv/bin/activate
cd agents/generation
PROBLEM_FILE=data/my_problem.md ./tests/run_example.sh
```

You can group problems in subdirectories under `data/` and the generated artifacts preserve that structure. For example:

```bash
PROBLEM_FILE=data/modrep/modrep.md ./tests/run_example.sh
```

For lossless publication paths, each problem-stem path component must start with
an ASCII letter or digit, end with a letter, digit, or `-`, and otherwise use
only letters, digits, `.`, `_`, or `-`. Names with spaces or Unicode are rejected
before Codex starts instead of being silently normalized.

To attach user-provided references to a problem (this is optional; use it when you are working on your own research problem and want to provide the agent with unreleased notes), create a sibling reference directory with the same stem:

```text
agents/generation/data/modrep/modrep.refs/
```

When that directory exists, the generation agent reads its files before using external search.
Reference files may be markdown, LaTeX, plain text, or PDF, but markdown, LaTeX and plain text is prefered over PDF. Actually, PDFs are converted to extracted text under `.extracted/` before the agent runs.

## 6. View Results in the Browser

- `agents/generation/site`: Zola site for browsing results in the browser

Results are markdown files with LaTeX math. To render them properly, a local [Zola](https://www.getzola.org/) site using the [MATbook](https://www.getzola.org/themes/matbook/) theme is included.

### Prerequisites

Install Zola.

Zola can be easily installed using your package manager in terminal. For example, on Mac, you simply run

```bash
brew install zola
```

and on ArchLinux, run

```bash
sudo pacman -S zola
```

For other operating systems, please see [Zola installation](https://www.getzola.org/documentation/getting-started/installation/).

### Serve

From `agents/generation/`:

```bash
./site/serve.sh
```

On first run this automatically clones the [MATbook](https://www.getzola.org/themes/matbook/) theme. Then it syncs all results from `results/` into the site and starts a local server. Open http://localhost:3264 in your browser.

Each problem  in `agents/generation/data/your_category`  will be a section in a chapter called `your_category`, while problems directly in `agents/generation/data` will be under `unclassified` chapter.

### Update the MATbook Theme

```bash
./site/setup_theme.sh
```

This pulls the latest version from the [MATbook repository](https://github.com/srliu3264/MATbook).
