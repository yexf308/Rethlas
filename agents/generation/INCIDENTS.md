# Generation Flow Incident Log

This file records operational failures observed in real generation runs. It is
intentionally separate from mathematical memory: an incident must not be
mistaken for a proof failure, and ignored run logs must not be the only record
of a flow defect.

For each incident, preserve the trigger, effect, classification, remediation,
and regression evidence. Never weaken a security boundary merely to suppress
an incident.

## 2026-08-08: write-capable MCP calls cancelled noninteractively

- **Classification:** flow defect; fixed.
- **Trigger:** the generator ran with outer approval policy `never`, while the
  trusted `reasoning_agent` MCP used the default approval behavior for
  write-capable tools such as `memory_init` and `memory_append`.
- **Observed effect:** `memory_init` was cancelled three times before any
  mathematical work. The failed session used 36,717 tokens, produced no
  blueprint, and never called the verifier.
- **Root cause:** the complete trusted MCP object did not declare
  `default_tools_approval_mode="approve"`.
- **Remediation:** inject one complete CLI MCP object containing command, args,
  cwd, environment, timeout, and approval mode. The executable MCP snapshot is
  outside the model-writable workspace and is content-attested.
- **Regression evidence:** runner mocks parse the exact TOML object and require
  the approval mode; full suite passed before publication. Fixed by commit
  `1b3cdfa`.

## 2026-08-08: trusted bytecode cache blocked startup

- **Classification:** expected fail-closed security behavior; operational
  hygiene gap.
- **Trigger:** test execution left `__pycache__` and `.pyc` files under the
  trusted generation MCP source tree.
- **Observed effect:** the runner refused to establish its trusted runtime
  manifest and exited before launching Codex. Token cost was zero.
- **Reason for retaining the check:** unchecked or stale Python bytecode is
  executable input and can override unchanged source. Silently excluding it
  from the manifest would reopen a previously demonstrated trust bypass.
- **Operational remediation:** run tests with `PYTHONDONTWRITEBYTECODE=1` and
  move generated caches out of trusted source trees before a live run. Do not
  make the runner delete or ignore bytecode automatically.

## 2026-08-08: numerical research packages unavailable to the generator

- **Classification:** capability-preflight defect; fixed in the pending
  math-research runtime change.
- **Trigger:** the external generation environment installed only MCP/API
  infrastructure packages, and the model shell correctly used
  `shell_environment_policy.inherit=none`.
- **Observed effect:** exploratory commands raised `ModuleNotFoundError` for
  NumPy and SymPy. The agent recovered with pure Python, but spent reasoning
  time discovering capabilities dynamically.
- **Root cause:** there was no declared math-research dependency profile, no
  real-import preflight, and no restricted PATH binding the model shell to the
  preflighted interpreter.
- **Remediation:** maintain a single math-research requirements
  profile; before any paid call, run `find_spec` and a real import for every
  required module; inject only the trusted environment's bin directory plus
  fixed system directories via a complete `shell_environment_policy` object.
  Keep host-environment inheritance disabled.
- **Regression evidence:** every missing module, a broken import, executable
  `.pth` hooks, and workspace-backed `.pth`/editable module origins fail before
  run-state allocation and with zero Codex invocations; the injected `python`
  and `python3` resolve to the preflighted interpreter. Safe external regular
  and namespace packages remain accepted. The focused runner suite passed 23
  tests and the full repository suite passed 210 tests.

## 2026-08-08: direct shell retrieval unavailable

- **Classification:** expected sandbox boundary; capability-disclosure gap.
- **Trigger:** a generation shell command attempted to download arXiv PDFs
  with `curl`.
- **Observed effect:** DNS resolution failed. Native web search and the
  theorem-search MCP remained available, and externally staged local paper
  text allowed the research to continue.
- **Reason for retaining the boundary:** arbitrary shell networking would let
  adversarial problem text exfiltrate readable data or fetch executable input.
- **Operational remediation:** tell the agent before reasoning that shell
  network access is unsupported. Use native web search for discovery and stage
  primary-source PDFs/text in the problem reference directory through a
  trusted outer workflow. Numerical acceptance must never depend on a fixed
  web sample.

## 2026-08-08: recursive sub-agent spawn protocol errors

- **Classification:** orchestration defect with automatic recovery; hardening
  pending.
- **Trigger:** a full-history fork was attempted with an explicit
  `agent_type`, followed by a transient lookup failure for the valid parent
  thread ID.
- **Observed effect:** the router reported both "omit agent_type" and "no
  thread with id" errors. Subsequent retries did create plan agents, but the
  flow recorded planned IDs before all successful tool returns were confirmed.
- **Required remediation:** omit agent/model overrides for full-history forks;
  store only canonical IDs returned by successful spawn calls; on failure,
  reconcile against the live-agent list before one bounded retry; never record
  a recursive round as `running` using invented or unconfirmed IDs. Wait and
  gather only confirmed live agents.
- **Required tests:** rejected spawn parameters, partial batch success,
  transient parent lookup failure, concurrency-slot exhaustion, and recovery
  must all preserve truthful round state.

## 2026-08-08: transient sampling and telemetry transport failures

- **Classification:** recoverable external transport events.
- **Observed effect:** one sampling stream disconnected before
  `response.completed` and recovered through the built-in bounded retry.
  Separate analytics calls returned 503/connection errors and missing-thread
  metadata warnings while mathematical work and memory writes continued.
- **Handling rule:** distinguish sampling failure from telemetry failure. A
  sampling call that exhausts retries is a failed generation attempt, not a
  mathematical verdict. Telemetry failure must not change proof state. Persist
  enough service-owned metadata to identify both without logging prompts,
  proofs, model streams, or secrets.

## 2026-08-09: recursive wait amplified a large cached context

- **Classification:** paid orchestration cost defect; mitigated by the
  `rethlas_recursive_wait_v1` contract and exact usage-growth audit.
- **Trigger:** while four recursive proof branches were active, the root agent
  used 34 short `wait_agent` calls, eight status-list calls, and seven separate
  follow-up calls. Twenty-three waits timed out without progress. The root
  context was already about 180k--194k input tokens, so each tool resumption
  sampled that context again.
- **Observed effect:** the 49 collaboration resumptions added 9,052,168 reported
  tokens, including 9,049,038 input tokens. Pure timeout resumptions accounted
  for 4,239,999 tokens. Four duplicate token-usage notifications were also
  observed, but their cumulative totals did not change and therefore did not
  inflate the app-server total.
- **Root cause:** the recursive-proving skill said only to wait for every
  sub-agent. It specified no long completion wait, backoff, status-query rule,
  fanout rule, or root orchestration budget. App-server usage notifications do
  not identify orchestration versus mathematical samples, so an adapter-side
  orchestration cutoff would be unsafe.
- **Remediation:** start at a 600-second early-waking wait, back off to the tool
  maximum, forbid status queries without mailbox changes, batch independent
  fanout where supported, and stop new orchestration calls at the contract
  budget. The runner integrity-binds `AGENTS.md` and `.agents/`. The hot-join
  adapter now separately audits notifications, cumulative-growth observations,
  and duplicates, and rejects nonmonotone or inconsistent cumulative deltas.
- **Regression evidence:** offline policy tests parse the machine-readable
  contract, execute timeout growth/reset and each exact stop gate, and forbid a
  60-second polling fallback. At this incident's historical average, the
  conservative 16-resumption proxy projects about 2.955 million input tokens;
  that is incident evidence, not a universal token ceiling for larger future
  contexts. Hot-join mocks cover early/duplicate/growth usage, malformed deltas,
  cross-thread spoofing, and terminal count honesty.

## 2026-08-10: prompt-timed review boundaries and implicit compaction were unsafe

- **Classification:** route-scheduling and context-lifecycle defects; corrected
  during the run and replaced by durable host policy.
- **Trigger:** a real FrontierMath generation run was instructed in prose to
  perform independent reviews at 30 and 60 minutes and hard-stop at 90 minutes.
  The model, rather than an external scheduler, was expected to interpret the
  review cycle and wall clock.
- **Observed effect:** after the first yellow review, the root misread the end
  of one bounded yellow period as the end of the whole 90-minute cycle and
  attempted to stop at roughly minute 35. It later launched the nominal
  minute-60 review at roughly minute 42. Owner messages corrected both events,
  but those messages were operational intervention, not reliable scheduling.
  No mathematical/proof content from the run is recorded here.
- **Context evidence:** immediately before automatic compaction, the root
  reported `last.inputTokens=179078` against
  `modelContextWindow=258400` (about 69.3% occupancy and 79,322 tokens of
  headroom). Compaction reset the next observed input to 24,978 tokens. The
  official minute-60 review had happened to be durably persisted just before
  compaction, but compaction itself neither proved persistence nor represented
  mathematical progress.
- **Control-plane evidence:** at 23:42:19 EDT the adapter exited 2 with only
  `rethlas hot-join error: disk I/O error`; app-server then recorded
  `turn_aborted`/`interrupted` after 4,350,715 ms, while the valid 138-event
  ledger remained `generation=2`/`active_turn=019feea7…` with
  `bootstrap:…:2` state `active` and zero terminal receipts (SQLite
  `integrity_check=ok`). A locally observed app-server terminal was therefore
  insufficient to close the durable active projection or authorize another
  paid turn.
- **Root cause:** prompts can influence behavior but cannot provide a trusted
  wall clock, atomic review boundary, hard interrupt, fresh-thread guarantee,
  or durable terminal projection. Automatic context compaction preserves some
  conversational utility but is not an authenticated research handoff. A
  wrapper-local elapsed timer also restarts with the wrapper and has no control
  authority. The exact failing SQLite operation is unknowable because the
  former `finally: release_lease()` path could mask the primary exception. A
  roughly 0.1-second FULL-sync lease-renewal loop—more than 43,000 writes over
  the 72.5-minute turn—is the strongest current candidate, not a proven cause.
- **Remediation:** hot-join now defaults to the fingerprint-bound
  `rethlas_route_review_90m_v1` and `rethlas_context_guard_v1` contracts. The
  adapter persists absolute `T0`, schedules T+30m/T+60m independent reviews,
  begins close at T+87m, and enforces a non-extendable T+90m stop. It records
  terminal-pending and operationally blocked states, and permits no new paid
  cycle without `continue_next_cycle`, a validated <=32 KiB handoff, and a
  strictly fresh thread epoch. Context thresholds use full input occupancy;
  cached tokens are not subtracted, and observed compaction forces handoff.
- **Regression evidence:** runner mocks require hot-join for cadence, reject a
  conflicting deep-work window before Codex, require one-shot authorization
  for a clean same-cycle continuation and a fresh epoch for
  `continue_next_cycle`, distinguish terminal finalization recovery from a new
  paid turn, preserve legal `generation_yield`, reject stale/hard-stopped
  restarts, and detect control-plane or reviewer-helper mutation before a
  reviewer/root spawn. Adapter tests exercise durable
  deadlines, hard-stop precedence, review idempotency, context thresholds,
  handoff/fresh-epoch binding, disk-failure markers, and restart recovery.

## 2026-08-11: restart and boundary audit found prompt-shaped capability leaks

- **Classification:** cross-layer scheduler, restart-capability, and executable
  trust-anchor defects found by static/mock audit; no mathematical content is
  recorded here.
- **Early-terminal finding:** an ordinary root turn can end cleanly while the
  durable 90-minute cycle is still active. Treating that transport terminal as
  the end of the cycle strands work; treating it as a new cycle resets the
  deadline. The only legal continuation is a one-shot host authorization in
  the same cycle (and normally the same thread epoch) with the original `T0`.
  Wrapper iteration limits must count owner-authorized cycles, not these short
  same-cycle transport turns.
- **Review-boundary finding:** a prompt labelled "review only" plus an MCP
  allowlist does not remove an ordinary root turn's built-in shell, web, or
  collaboration capabilities. Due reviews therefore require host-owned
  orchestration and must start zero ordinary full-capability root turns. A red
  route or any review/verification-only allowed action must also block normal
  continuation even when the preceding transport terminal was clean. The
  runner now consumes a terminalized boundary only through the authenticated
  owner-side `review-drive` operation. Its executable and exact dependency
  closure are content-attested; the request contains only the durable boundary
  id, and the host derives the critic/review identities. The same owner-host
  operation must finish a bounded handoff before status can expose
  `continue_reviewed_cycle_fresh_epoch`; otherwise
  `post_review_handoff_required` remains paid-disabled. The adapter consumes
  the exact handoff and substitutes canonical rehydration input before any
  fresh same-cycle root turn, preserving the original `T0` and deadlines.
- **Owner-yield finding:** `generation_yield` can be durably written just
  before a wrapper crash. If restart first writes a new `running` record, it
  destroys the exact wait receipt needed to close the cycle. Restart must
  project recovery-only `owner_yield_close_required`, close the already-bound
  handoff/wait receipt, and only then accept an explicit owner resume into a
  fresh epoch. Reviewer red alone never substitutes for this handshake.
- **Frozen-route terminal finding:** an official red review with no exact
  pre-due fallback is not an operational retry condition. The host closes it
  as `route_frozen`; the runner reports the frozen reason, starts zero further
  paid work, and exits `1`. Restart replays the same normal unsolved terminal
  and cannot synthesize an owner/advisor wait.
- **Generation-counter finding:** `runs.generation` advances for every paid
  root turn, while `cadence_cycles.generation` remains the generation of that
  cycle's first turn. Same-cycle clean-terminal, review, and context rollovers
  therefore make valid cycle generations non-consecutive. A next-cycle
  guardian must bind `expected_generation = runs.generation + 1` and require
  it to exceed the prior cycle generation; using `latest_cycle.generation +
  1` can pre-create a cycle that the real next turn can never consume. A
  same-cycle guardian resume instead binds the existing cycle generation and
  immutable clock. Fresh-thread binding may only replace that guardian
  cycle's pending thread/turn sentinels; it must not insert a second cycle or
  recompute `T0`.
- **Release-gate finding:** a collaboration convention saying that the paid
  path is on hold does not stop a standalone wrapper. Cadence-on execution now
  requires hash-bound host policy `guardian_enforcement_ready=true`; false,
  missing, non-boolean, or digest-tampered policy exits operationally before
  run initialization or any control/reviewer/root work. Wrapper environment
  cannot opt in, while legacy cadence-off operation remains explicitly
  outside this guarantee.
- **Executable finding:** making a trusted snapshot read-only with `chmod` is
  not a same-UID execution boundary; the owner can chmod, replace, execute on
  MCP restart, and restore bytes between wrapper hash checks. The runner now
  uses an isolated fixed loader that securely opens, hashes, captures, and
  executes the attested MCP/review module bytes without a pathname reopen.
  The Codex executable is likewise resolved and content-attested, and
  group/world-writable targets are rejected.
- **Deadline finding:** a wrapper that starts after absolute `T+90m` with no
  live adapter must perform only authenticated zero-model scheduler
  finalization; it must never start a replacement turn. A scheduler hosted in
  the same potentially hung adapter process is not an independent hard stop,
  so the enforcement design also requires a detached trusted guardian outside
  the paid process group. Before the paid command can leave a pipe-blocked exec
  gate, the guardian must durably register the run/generation instance, stable
  paid-group leader PID and start identity, owner UID, PGID, policy digest,
  boot identity, and inherited lifeline. The paid group uses a trusted
  session-leader shim which forks and waits for the runner; it never replaces
  itself with the runner or exits merely because the direct runner returns
  while trusted descendants remain. This prevents a missing leader or reused
  PGID from turning a later kill into an unrelated-process signal.
- **Hard-boundary design:** the guardian enforces the earlier authenticated
  wall-clock and same-boot monotonic deadlines persisted by the host. A boot
  mismatch, clock drift, missing authoritative `T0`, database/poll failure,
  lost exec-release byte, escaped descendant, or unverifiable leader identity
  fails closed rather than creating a new 90-minute clock. At `T+89m55s` it
  persists the internal interrupt intent and may use graceful cleanup only
  before the hard boundary. At exact `T+90m` it revalidates the leader
  PID/start identity/UID/PGID, sends process-group `SIGSTOP`, then directly
  sends `SIGKILL` and reaps; it never sends `SIGCONT` and grants no paid grace.
  A durable idempotent `watchdog_forced` result and zero-model reconciliation
  follow only after the group is dead. The detached guardian survives wrapper
  death and must also prove the paid group empty after a nominal `rc=0` before
  review orchestration or a fresh epoch begins.
- **OS boundary:** on macOS this design covers the trusted runtime and its
  ordinary process tree. In the absence of cgroups or `PDEATHSIG`, it cannot
  prove containment of malicious code which deliberately escapes with
  `setsid`, nor survive an external actor killing the guardian itself. The
  guardian continuously detects an ordinary descendant PGID escape and
  fail-stops; this limitation is recorded rather than overstated as a kernel
  guarantee.

## 2026-08-11: legacy non-fresh resume needed a zero-paid migration diagnosis

- **Situation:** the owner asked whether an existing run/thread could be
  resumed without forcing a fresh run while the immutable guardian release
  policy was still false. Starting the old turn to find out would itself have
  violated the release gate; opening the authoritative SQLite file with a
  migration-capable adapter would also have changed the evidence being
  diagnosed.
- **Dry-run contract:** the runner now accepts an explicit non-fresh diagnostic
  only on a distinct, owner-only, byte-identical SQLite copy with no
  pre-existing WAL/SHM sidecar. It securely reads and executes the attested
  adapter bytes in memory, obtains only `policy-contract`, `status`, and
  `cadence-control-state`, and exits before statement preparation, runtime
  snapshot, `init`, capability binding, recovery, reviewer, or Codex discovery.
  Its JSON always says `resume_admitted=false` and
  `paid_processes_started=false`; command exit `0` means only that the
  diagnostic completed.
- **Real copied-ledger result:** run
  `fm-chowla-encourage-20260810-213629` preserved existing thread
  `019fee7e-1549-75d0-a045-738e9d46ef9a` and active turn
  `019feea7-97b2-70c0-8c47-ae9e04c29839` at generation 2. With
  `guardian_enforcement_ready=false`, the host projected `stale_active`,
  `paid_turn_allowed=false`, and
  `legacy_stale_active_offline_reconciliation_required`. The next admissible
  step is the runtime's authenticated zero-model reconcile receipt after
  guardian release, not a model-authored continue message and not an inferred
  fresh-thread migration.
- **Integrity result:** the authoritative DB SHA-256 was
  `5abfc0a6506d106d17d3b20a9e3b1df7ef806ab418fc2b7c187b7d90b6fe0029`
  before and after. The source had a zero-byte WAL and a stale 32,768-byte SHM;
  source inspection therefore used immutable/secure reads and did not treat an
  idle SHM alone as a live writer. A nonempty WAL remains a hard rejection.
  Adapter schema/scheduler projection changed only the disposable copy. Mock
  regression also instruments the Codex executable and proves zero invocations,
  zero `init`, zero capability bind, zero review drive, and zero
  `run-generator` calls during diagnosis.
- **Authoritative reconcile result:** on the owner-only copy, schema 5 still
  projected the adapter-side stale active turn, while the single authenticated
  `thread/read(includeTurns=true)` observation reported that exact turn as
  `interrupted`. The copy therefore converged only to
  `operational_blocked` with an immutable quarantine; it did not gain resume,
  fresh-thread, or paid-turn authority. The terminal receipt committed at
  sequence 377 with five settled messages, `thread/read` response SHA-256
  `2fa248f2db11cd311a870ef1161dfcf2ab796ac0c159b847ab14b307ba80cf27`,
  and terminal SHA-256
  `2e3f5af84a2a0eb3635039e5b6c6a689237c9dbc8a4985952c3881d65ecf6ecc`.
  Only the disposable copy changed. Its final byte digest is intentionally
  invocation-specific because the scoped capability/token receipt is fresh;
  it is not a stable recovery identity. The original DB retained the exact
  SHA-256 above, its zero-byte WAL, and its 32,768-byte idle SHM.
- **Why a bounded handoff is necessary:** the legacy memory held only four
  `branch_states` records, and the lane/review record IDs referenced by the
  old control state were absent. Those rows cannot substantiate a restart.
  Any recovery artifact must instead be a bounded candidate derived by the
  trusted host from the reconciled `thread/read` receipt. It remains a lead,
  not mathematical evidence and not resume authority.
- **External retrieval candidate:** the separately staged Matlas UI result at
  `agents/generation/downloads/frontiermath-chowla-cosine/matlas-recovery-gap-20260811.json`
  has SHA-256
  `df36bc8fba691cbfc6248499145a2725a77c73791af37dac89af2b0b40e39770`.
  It is explicitly `external_leads_only`: non-proof, no
  paid authority, and usable by a later handoff consumer only after the host
  re-hashes the complete candidate. A model or shell cannot promote it to
  evidence.

## 2026-08-12: clean worker return left a residual process-group member

- **Classification:** terminal process-cleanup defect found by the fifth fresh
  guardian soak; fixed by exact durable-topology cleanup semantics.
- **Trigger:** run `guardian-soak-20260812-fresh-05` completed a real paid model
  turn and its independent verifier accepted the resulting artifact. The
  direct worker then returned `rc=0`, but the guardian's retirement check still
  observed a member of the root paid process group.
- **Observed effect:** the successful model and verifier work was preserved,
  but a worker return code did not prove that its process group was empty. The
  residual check raised `ResidualDescendants`; the guardian recorded
  `execution_unknown`, the wrapper exited `70`, and cadence remained
  `operational_blocked`. Later postmortem checks found all production process
  identities absent, but the exact transient residual PID was not durably
  recorded and is therefore not inferred.
- **Remediation:** after a natural `rc=0`, an already-empty registered topology
  still completes as `paid_group_empty` without signals. If any registered
  durable group remains, the guardian immediately applies `SIGSTOP` and then
  `SIGKILL` to the exact durable topology, discovers and revalidates identities
  while frozen, and proves every represented group empty at a fixed point. It
  never grants a passive grace period or sends `SIGCONT`. Only that complete,
  exact receipt may close as
  `paid_worker_returned_group_cleanup`; the worker itself still returned
  naturally, so this terminal cleanup is not reported as a watchdog-forced
  worker return.
- **Fail-closed cases:** a newly observed candidate that lacks prior durable
  attestation may be stopped and killed for safety, but it cannot be laundered
  into successful coverage. A new or ambiguous identity, incomplete signal or
  reap receipt, coverage mismatch, host revalidation failure, or arrival at
  the hard deadline remains `execution_unknown` or `watchdog_forced` as the
  deadline semantics require.
- **Regression evidence:** tests cover same-group residual cleanup with a real
  forked helper, exact stopped/killed/empty coverage, rejection of unattested
  candidates, deadline crossing, idempotent terminal replay without duplicate
  signaling, and host-side rejection of arbitrary cleanup reasons or partial
  process coverage.
- **OS threat boundary:** this guarantee is for the trusted ordinary runtime
  tree on macOS. Without cgroups or an equivalent kernel containment primitive,
  it does not claim to contain deliberately malicious code that escapes with
  `setsid`; such an escape remains outside the release threat model and must
  not be disguised as proof of exact process containment.

## 2026-08-12: checkpoint MCP wait crossed the first review boundary

- **Classification:** checkpoint transport/recovery and publication-boundary
  defects exposed by the sixth fresh guardian soak; corrected by the
  three-role MCP and host-registry change described below.
- **Trigger:** run `guardian-soak-20260812-fresh-06` used a real model on the
  Chowla cosine problem. At roughly T+10m the model called
  `memory_append_batch` from the required single outer `functions.exec` cell.
  The former single `reasoning_agent` server exposed that write tool with
  `tool_timeout_sec=3600`. The outer call yielded a running cell ID and remained
  pending through repeated observations until the T+30m boundary.
- **Observed effect:** no checkpoint memory directory, durable batch, or tool
  receipt appeared. At T+30m the guardian correctly interrupted the run,
  failed closed, and proved all registered paid process groups empty. The
  wrapper exited operationally hard-stopped and no official T+30m review was
  published. Mathematical work from the turn is not treated as a checkpoint
  or review result.
- **Attribution limit:** the available rollout and filesystem evidence cannot
  distinguish a stall in nested tool-call delivery, authenticated MCP
  preflight or handler execution, or result transport back through the outer
  cell. The old 3,600-second timeout explains why there was no bounded return
  before T+30m; it does not identify the internal stall location.
- **MCP role split:** one content-attested base definition now derives three
  required MCP processes. `reasoning_agent` retains the long 3,600-second
  budget needed by reasoning and verification tools but explicitly disables
  `memory_append_batch`. `reasoning_checkpoint_primary` and
  `reasoning_checkpoint_recovery` each expose only `memory_append_batch` and
  each has a 60-second tool timeout. The independent recovery process is a
  separate fault domain from the primary; it is not a general load-balancing
  path.
- **Recovery boundary:** recovery is currently selected by the model prompt's
  exact classifier inside the same outer `functions.exec` program. It accepts
  only either of the two version-pinned primitive timeout strings for the
  primary's exact server/tool pair, compared with `===`, and makes at most one
  recovery call with the same frozen arguments. Semantic errors, `isError`
  results, objects, generic transport errors, and every unclassified failure
  fail closed. This is not yet a host-side automatic circuit breaker, and a
  second MCP call is never evidence of success without its durable receipt.
- **Timeout semantics:** an MCP tool timeout bounds how long Codex awaits the
  response; it does not guarantee cancellation of the server handler. The
  primary may therefore commit durably and continue running after the caller
  observes a timeout. The recovery call is an exact content-addressed replay
  that must return the original receipt rather than publish a second logical
  batch.
- **Publication authority:** new batch writes first durably prepare a v3
  content-addressed data file and a separately validated commit marker. Those
  files are immutable candidates, not publication authority. In a released
  run, the MCP must then register their exact hashes through the authenticated
  adapter. That registration samples fresh wall and same-boot monotonic clocks
  inside the same SQLite `BEGIN IMMEDIATE` writer fence used by cadence
  transitions, and stores one immutable accepted-or-rejected receipt. If the
  boundary transaction wins first, registration is rejected; if registration
  wins first with both artifacts already durable and both clocks pre-due, the
  later boundary observes the accepted row. A marker left by timeout, crash,
  or post-due registration is invisible to trusted readers. Exact replay
  returns the original row and receipt after response loss.
- **Legacy boundary:** markerless v2 checkpoints and legacy JSONL remain
  readable only in offline development where no released registry is
  configured. A released run trusts only v3 artifacts whose hashes match its
  accepted registry manifest; same-UID creation of a syntactically valid v2
  file, a v3 marker without a row, or a JSONL record cannot enter the official
  review projection. The legacy `memory_append` and `branch_update` tools fail
  before any filesystem write in released runs instead of returning a success
  receipt for evidence that official readers would ignore. Fresh soak runs do
  not import legacy evidence implicitly.
- **Regression evidence:** a zero-model integration test drives the installed
  Codex app-server directly through `thread/start`, MCP status, and MCP tool
  calls, and never invokes `turn/start`. With shortened test timeouts it proves
  three distinct MCP process IDs, long-lane batch disablement, a primary
  handler that continues after Codex times out, recovery with the exact same
  frozen arguments in the separate process, one durable publication, and
  successful completion of a long-lane probe. Focused persistence tests cover
  dual-clock and second-auth denial, the SQLite publication/boundary ordering,
  invisible rejected or crash-orphaned v3 candidates, exact response-loss
  replay, released-run rejection of unregistered v2 files and JSONL writes,
  and offline-only legacy v2/JSONL reads.
- **OS threat boundary:** this incident and its regression evidence cover the
  trusted ordinary runtime tree. They do not expand the macOS guardian threat
  model and do not claim containment of deliberately malicious code that
  escapes with `setsid`.

## 2026-08-12: checkpoint timeout envelope and daemon event tail were mishandled

- **Classification:** two independent flow defects exposed by the eighth fresh
  guardian soak: an exact checkpoint-recovery classifier mismatch and an owner
  launcher terminal-event drain gap. The guardian's offline safety cleanup
  still succeeded. No mathematical content from this run is recorded here.
- **Trigger:** fresh single-wrapper run
  `guardian-soak-20260812-fresh-08` used the three-role MCP topology on the
  Chowla cosine problem with one wrapper iteration. The checkpoint primary
  actually timed out, but Codex returned the failure as an MCP
  `{content, isError}` envelope. The then-current recovery classifier accepted
  only either of two primitive timeout strings, so it rejected the real
  envelope and did not call the independent recovery role.
- **Checkpoint and turn outcome:** the protected turn propagated that failure
  and stopped at the pre-critic safety gate. It returned a clean app-server
  `completed` terminal with no trusted checkpoint receipt and therefore no
  official checkpoint, review, verification, or solution publication. The
  clean turn terminal does not imply that the guardian terminal protocol also
  completed.
- **Guardian terminal outcome:** after the final durable poll reported all
  dynamic paid groups terminal and only the stable root remained, the owner
  launcher's `select` readiness check was followed by `daemon_process.poll()`.
  The old loop broke as soon as `poll()` observed daemon exit and did not drain
  bytes queued between those two observations through the event pipe's true
  EOF. No durable guardian terminal report existed, so the launcher took the
  registered offline-stop path. The iter artifact consequently recorded
  `report=null`, `offline_finalize.state=watchdog_forced`, and wrapper exit
  `70`.
- **Attribution limit:** the missing tail could have contained a `final` frame,
  a `daemon_error` frame, or malformed/partial terminal bytes. Neither that
  frame nor the daemon wait status was retained in the run artifacts, so the
  concrete daemon-side error is unknown and must not be inferred. The evidence
  proves only the post-final-poll/pre-offline-stop failure window and the
  launcher's failure to consume and validate the complete pipe tail.
- **Safety result:** authoritative offline cleanup captured the durable
  registration and exact paid topology, proved complete coverage and every
  group empty, killed no process, and left no wrapper, verifier, guardian,
  app-server, MCP group, escaped descendant, or verifier listener behind. Exit
  `70` was therefore an operational fail-closed result, not a cleanup escape.
- **Checkpoint remediation:** classify only either exact version-pinned MCP
  timeout envelope: exactly the top-level keys `content` and `isError`,
  `isError: true`, and exactly one `{type: "text", text: ...}` block whose text
  is one of the two complete 60-second timeout spellings for
  `reasoning_checkpoint_primary/memory_append_batch`. Permit at most one call
  to the recovery role with the same frozen arguments. Primitive strings,
  extra or missing fields, `_meta`, `structuredContent`, near matches, semantic
  failures, and every other error remain non-retryable; a durable returned
  receipt is still required before claiming checkpoint success.
- **Launcher remediation:** parse the guardian event stream incrementally and,
  after observing daemon exit, boundedly drain all available bytes through a
  clean EOF before interpreting terminal state. Distinguish `EAGAIN` from EOF;
  reject partial, oversized, replayed, out-of-order, or unknown frames. For
  offline cleanup, the host's durable registration is authoritative even if
  the local registration event was absent from the observed pipe; lack of a
  local frame must not suppress cleanup of an already registered topology.
- **Regression evidence:** checkpoint tests cover both exact timeout-envelope
  spellings, one byte-identical recovery replay, rejection of the former
  primitive strings and all inexact envelopes, no third call, and receipt-only
  success. Launcher tests queue registration/release/final or `daemon_error`
  frames after the last readiness observation, require clean EOF consumption,
  distinguish bounded `EAGAIN`, reject a partial terminal frame and event
  replay/order violations, and require durable host registration to authorize
  offline cleanup when local event observation is incomplete.
- **Outer observation cadence:** a read-only outer soak observer may sample as
  infrequently as every five minutes to reduce observation noise. That cadence
  has no control authority and does not replace, delay, or weaken the
  guardian's internal safety polling, durable deadlines, or terminal cleanup.

## 2026-08-18: official MCP migration exposed two package-boundary mismatches

- **Classification:** MCP SDK compatibility and verifier preflight defects;
  fixed. No mathematical rejection occurred.
- **Trigger:** a real one-iteration smoke used Codex CLI 0.148.0-alpha.9 and a
  clean runtime with the official MCP SDK 2.0.0 on the included prime-order
  group example.
- **Observed effect:** generation produced a complete draft and durably wrote
  its local phase checkpoint, but the exact checkpoint receipt validator
  rejected the returned envelope. The verifier endpoint then returned HTTP 500
  twice before allocating a run or starting a verifier Codex process. No
  `blueprint_verified.md` was published during that turn, and the wrapper
  correctly exited `1`. The generation turn reported 94,767 tokens; the two
  failed verifier requests used zero verifier-model tokens.
- **Root causes:** the official SDK inferred structured output for
  `Dict[str, Any]` as `structuredContent={"result": receipt}`, while the
  security contract requires the text body and structured content to equal the
  exact receipt. Separately, starting uvicorn from `agents/verification` put
  that directory's local `mcp/` package ahead of the installed official SDK
  during API preflight.
- **Remediation:** the checkpoint tool now returns an explicit standard
  `CallToolResult` whose only app-server fields are `content`, `isError`, and
  `structuredContent`, with the decoded text exactly equal to the structured
  receipt. Verifier preflight removes only the exact local MCP package parent
  from its temporary import search path, resolves the official SDK by
  capability across its 1.x and 2.x server locations, and restores `sys.path`
  before returning. A preloaded local shadow still fails closed.
- **Regression evidence:** installed-Codex zero-model tests enforce the exact
  checkpoint envelope and idempotent replay under both MCP 1.29 and 2.0. A
  subprocess regression starts from the documented verifier working directory
  and proves that the local package cannot shadow the SDK. The clean-runtime
  smoke subsequently verified both proof items, atomically published the
  blueprint and external receipt, and the top-level runner recognized them
  with exit code `0`. The final repository suite passed 1,121 tests and 53
  subtests, with one expected skip.

## 2026-08-18: critic-first hardening had removed original three-route fanout

- **Classification:** proof-search policy regression relative to original
  Rethlas semantics; replaced with a bounded safe three-route contract.
- **Trigger:** repository-history review showed that commit `bf8e6f1` replaced
  the old one-agent-per-decomposition-plan fanout with one protected root route
  followed by one adversarial critic. Tests explicitly forbade the original
  parallel-plan clauses, so the change was semantic rather than an incidental
  runtime limit.
- **Observed effect:** the first real post-upgrade multi-agent smoke ran only a
  root plus one critic. It proved native collaboration worked, but did not test
  the original three-direction behavior. A separate legacy smoke also exposed
  that `generation_yield` was still attempted without a hot-join review-adapter
  binding, producing an invalid owner-wait transition after otherwise valid
  mathematical failure persistence.
- **Remediation:** `rethlas_safe_three_route_v1` now requires exactly three
  materially different, scope-disjoint plans and one context-free solver per
  plan in a single fanout. Children cannot recursively spawn, switch assigned
  routes, write shared memory, verify, publish, yield, or open an advisor path.
  The root is the canonical merger and cannot run a fourth proof route. Any
  complete candidate preempts the remaining waits; a later fanout requires all
  three prior reports plus a durable shared failure synthesis. Guardian permits
  three live proof children and fail-stops the fourth. Owner-wait states remain
  hot-join-only; cadence-disabled legacy runs return unverified without calling
  `generation_yield`.
- **Real-run evidence:** one Codex CLI 0.148.0-alpha.9 smoke on the Chowla
  cosine-set problem admitted all three solvers in one fanout. The durable
  round bound unique child ids to the cyclic-deperiodization,
  multivariate-positive, and one-sided-discrepancy plans, recorded
  `fanout_complete=true`, six orchestration resumptions, zero timeouts, zero
  status queries, and no follow-up fanout. All three reports were merged in one
  checkpoint; no proof or verified blueprint was claimed. The turn used
  129,559 tokens.
- **Runtime finding:** the framework Python 3.13.7 venv reproducibly caused
  multi-process SQLite `disk I/O error` or `database disk image is malformed`
  failures in the guarded review path. Rebuilding both local venvs with copied
  Anaconda Python 3.13.9 binaries made the real review-drive tests pass. The
  failed hot-join smoke attempts started zero model turns; the successful
  three-solver smoke used legacy transport.
- **Regression evidence:** policy tests bind the exact three plans, three
  context-free roles, no child recursion/shared writes, candidate preemption,
  and hot-join-only yields. Host tests admit three live proof lanes and
  operationally block four. The final repository suite passed 1,121 tests and
  53 subtests, with one expected skip.

## 2026-08-20: guardian rejected valid Codex descendants and short-lived groups

- **Classification:** fail-closed guardian liveness incompatibility; fixed. No
  mathematical result or untrusted descendant was accepted.
- **Trigger:** a real protected run on `arxivmath/am-2606-047` used Codex CLI
  0.148 and the required simultaneous three-route fanout.
- **Observed effect:** the original run stopped at the review boundary with
  `descendant closure crossed app-server sessions`. Later repair smokes exposed
  `paid descendants escaped into ambiguous process groups`. No candidate,
  verifier receipt, or verified blueprint was published by the failed runs, so
  the safety boundary remained closed.
- **Root causes:** Codex 0.148 reports each persisted child thread's own thread
  id as its raw `sessionId`, although rollout metadata binds the descendants to
  the root session. The guardian had treated the raw field as one shared durable
  identity. Separately, millisecond-lived Codex helper processes could be exact
  process-group leaders at the descendant snapshot and exit before the later
  identity recheck, leaving a valid but already-empty group. Codex
  0.148.0-alpha.15 also left short-lived same-UID members alive after their
  exact process-group leader exited. Finally, Darwin's `kern.boottime.tv_usec`
  changed repeatedly within the same boot, so hashing it caused a false reboot
  classification.
- **Remediation:** descendant review now validates the full parent chain,
  collaboration source, declared depth, and absence of cycles. It accepts only
  one shared raw session id or root-and-child-local raw ids, then normalizes the
  durable projection to the root thread id. Only unexplained mixed ids receive
  three bounded authoritative rescans before a fail-closed stop. Process-group
  capture may stage a vanished exact leader only when its group is provably
  empty or contains only same-UID leaderless members of the still-live original
  POSIX group. The host independently rechecks that membership before durable
  attestation. PID reuse, identity change, a vanished nonleader without an
  already-bound leader, foreign members, or ambiguous inspection still fail
  closed. Darwin boot identity now uses the stable native
  `kern.bootsessionuuid` instead of the drifting boot-time microseconds, with
  the guardian and adapter deriving the same content digest.
- **Regression evidence:** three session-projection tests cover child-local ids,
  transient convergence, and persistent unexplained mixing. The guardian TOCTOU
  tests cover just-exited empty and same-UID leaderless groups. Darwin tests
  bind the exact boot-session UUID format and guardian/adapter agreement. Final
  suites passed 412 hot-join tests, 76 guardian tests with one expected skip,
  and 137 launcher and runner tests.
- **Real-run evidence:** run
  `arxivhard-am2606047-guardian-sessionfix-e2e-20260820-04` admitted exactly
  three live proof children whose raw `sessionId` values equalled their own
  child ids. It recorded zero control failures, 23 paid-group discoveries, and
  17 safely classified already-empty groups while the guardian remained active.
  The smoke was then intentionally interrupted rather than allowed to solve the
  full problem. Its terminal state was therefore `execution_unknown`; exact
  process-group cleanup left no generation or guardian process behind.

## 2026-08-20: production T+30 review lost the guardian terminal report

- **Classification:** unresolved post-boundary lifecycle failure; fail-closed.
  The review snapshot and interruption completed, but the independent critic
  was not launched and no mathematical success was claimed.
- **Trigger:** three fresh, retrieval-disabled same-statement runs under Codex
  0.148.0-alpha.15 reached the first production review boundary after the
  session, process-group, and Darwin boot fixes. The last run was
  `arxivhard-am2606047-guardian-live-20260820-r9`.
- **Observed effect:** the root and exactly three active proof children were
  observed, all four interrupt requests were accepted, all turns became
  terminal, and `review_boundary_no_live_descendants_confirmed` was committed.
  About 2.5 seconds after the T+30 due instant, the guardian daemon disappeared
  without a final or `daemon_error` frame. Owner offline cleanup proved the
  complete topology empty and committed `watchdog_forced`; the wrapper exited
  `70` before `review_drive` or a critic could start.
- **Evidence:** the final run's 211-event ledger chain verifies successfully and
  contains zero control failures, one accepted reasoning checkpoint, 34
  paid-group discovery events, three descendant observations, three accepted
  descendant interrupts, one accepted root interrupt, one root-terminal
  confirmation, and one no-live-descendants receipt. Discovery dispositions
  comprised 24 live registrations and 20 already-empty groups.
- **Isolation attempts:** the same boundary outcome occurred under the ordinary
  foreground launcher, a reparented independent process session, and detached
  GNU Screen. A `launchd` job would have provided a separate process coalition,
  but macOS TCC denied that service access to the Documents checkout before any
  model call. Host process-lifetime interference is therefore plausible but is
  not proven; the early offline-result branch also omits the daemon wait status,
  preventing exact signal attribution.
- **Next validation:** add a separate default-off accelerated guardian smoke
  policy with its own policy id and digest, plus exact daemon wait-status
  evidence. Compress only the test clock and label every receipt non-production;
  never change or claim equivalence with the production 30/60/90-minute policy.

## 2026-08-20: accelerated guardian review chain completed after Codex 0.148 fixes

- **Classification:** guardian/reviewer runtime compatibility failures; fixed.
  The validating clock was an isolated, default-off non-production smoke and
  does not establish production 30/60/90 timing equivalence.
- **Root causes found by same-statement testing:** transient groups observed
  after a completed host poll were omitted from durable terminal coverage;
  PID/PGID reuse and Darwin group visibility were treated as live old groups;
  forward NTP correction was rejected even though it only shortened the
  earliest-clock deadline; and normal Codex teardown could create one final
  helper group after root return. The independent critic then exposed stale
  Codex configuration (`tools.view_image`), auth-status output on stderr, an
  undersized process-wide file limit that killed the SQLite WAL, an implicit
  shell-snapshot setsid process, unsupported JSON Schema `oneOf`, bounded
  transport-error items outside the turn, and a runner fence that expired at
  official close before disposition/handoff.
- **Remediation:** exact locally observed groups remain staged until a
  successful host poll records either their identity or already-empty state;
  retirement requires the old group empty or exact PID+PGID leader reuse.
  Descendant capture has no per-group sleep and rechecks the hard deadline
  across large scans. Forward wall corrections are accepted while backward
  drift remains fail-closed. Reviewer auth is copied before preflight and
  accepts login status on either bounded stream. The critic disables shell
  snapshot and all tool surfaces, carries its report in bounded JSONL, uses a
  separate internal-state file cap, and validates one ordered attempt only
  after every pipe reaches EOF. Review admission and completion use the
  earliest same-boot wall/monotonic deadline, including a transaction-bound
  check immediately before `Popen`. The post-close runner fence remains valid
  only for the same live Guardian, cycle, terminal boundary, official review,
  and unexpired dual-clock review window.
- **Regression evidence:** final local suites passed 82 guardian tests with one
  expected skip, 422 hot-join tests, 83 review client/contract tests, and 137
  launcher/runner tests. Dedicated tests cover 256-group deadline pressure,
  unsubmitted already-empty publication, PID reuse with leaderless residuals,
  pre-dispatch deadline expiry, inherited pipe writers, ordered event streams,
  and post-close fence revocation.
- **Real-run evidence:** non-production run
  `arxivhard-am2606047-guardian-fast-high-final-20260820-16` used the same
  `am-2606-047` statement, `gpt-5.6-sol` at `high`, and a T+6m first review. It
  accepted one reasoning checkpoint, observed and interrupted exactly three
  active child turns plus the root, recorded zero control failures, completed
  both root and review Guardians, launched one fresh tool-free critic, and
  officially published a yellow review. The drive reached
  `disposition_ready`; context handoff
  `handoff_95ae1304f6d1e3f26c50bcc7554eb9afafcbd89b2af62c1b91a600fafadf5c59`
  was validated from epoch 1 to pending epoch 2. The smoke stopped immediately
  after that handoff, exited zero in 7m16s, and did not claim a proof or start a
  second paid segment.

## 2026-08-20: production fresh-run owner yield failed before the first review

- **Classification:** production-shaped pre-review owner-yield integration
  failures; fixed. The mathematical search remained honestly unsolved and no
  verifier receipt or answer was claimed.
- **Trigger:** production run
  `arxivhard-am2606-048-production-max-20260820-01` used the T0 problem asking
  for the exact number of labeled 19-element posets, `gpt-5.6-sol` at `max`,
  and the unmodified 30/60/87/90-minute cadence. Exactly three logical routes
  completed without a candidate. The root then requested an owner-advisor
  yield before the first scheduled review.
- **Observed effect:** all three `context-handoff-prepare` attempts failed with
  `context handoff assertions differ from durable host state`. After the root
  returned cleanly, the wrapper attempted `continue_active_cycle` and failed
  with `owner control capability cannot authenticate runner authority`; the
  run exited `70` at T+22m35s. Guardian finalized normally with
  `forced=false`, no killed or stopped groups, and no remaining generation
  process.
- **Root causes:** a fresh production cycle keeps the host route placeholder
  `route:unspecified` until its first official review, while the valid
  pre-fanout checkpoint had already committed the concrete root route. The
  handoff control required strict equality and therefore made a pre-review
  owner yield impossible. A missing draft correctly produced
  `blueprint_sha256=null`, but the adapter coerced that assertion to the string
  `"None"` before comparing it. Released `cadence-admit` always selected a
  runner fence even when the command arrived through the documented one-shot
  owner FD after Guardian return. Its outer projection validator then rejected
  the real post-admission `continue_active_cycle_authorized` action because the
  mock had continued to report `free_construction`. Finally, the trusted MCP
  derived one pending advisor record, but the review-client to adapter proposal
  omitted that host-derived field and the adapter hard-coded both pending ids
  to null. This last mismatch created a validated host handoff while returning
  an MCP error, so generation correctly refused to call `generation_yield`.
- **Remediation:** a validated pre-review `owner_yield` or `context_guard`
  handoff may bind the single initial concrete route atomically when, and only
  when, the durable cycle still contains `route:unspecified` and has no prior
  official review. Replays are idempotent and a different later route remains
  rejected. Released continuation admission now selects the fence from the
  actual one-shot FD domain: runner FD operations retain `GuardianRunnerFence`,
  while the outer owner wrapper uses the current `ReviewControlFence`. The
  adapter preserves a genuinely nullable pre-review blueprint digest. The
  validator recognizes only the exact authorized continuation action. The
  trusted server now derives `pending`, passes it through the private review
  client proposal, and the adapter binds it into the content-addressed handoff;
  the model-facing tool still has no pending-state parameter. The following
  paid root still requires a fresh Guardian admission, so none of these changes
  grants paid work directly.
- **Regression evidence:** tests reproduce a reasoning-epoch handoff against a
  production-like unspecified route, reject invalid content without binding,
  accept one binding, replay it once, reject route drift, exercise the real
  owner-FD subprocess after Guardian return, preserve the runner-only fence
  path, use a null blueprint digest, expose the actual authorized action in the
  runner mock, and carry one server-derived advisor id through the private
  handoff protocol. The complete generation test directory passes 973 tests
  with one expected skip; the isolated worktree needed the repository's
  intentionally ignored quarantined-seed fixture before its final test could
  pass.
- **Follow-up production evidence:** runs `-02` and `-03` exposed the nullable
  blueprint, authorized-action, and pending-state mismatches without starting a
  second paid turn. Final run
  `arxivhard-am2606-048-production-max-20260820-04` used the same statement,
  model, effort, and unmodified production cadence. It completed one exact
  three-route fanout, atomically bound `route_source_attestation`, validated
  handoff
  `handoff_7b5dcbcbeb77b700bd7208b60abd60e2cf4e06529b08c991abc060a4665528bf`
  with the exact pending advisor record, prepared and closed one owner-yield
  admission, completed Guardian with `forced=false`, and exited zero in
  22m57s. Its 231-event chain verifies, with zero control failures, zero
  continuation authorizations, zero verifier attempts, and final disposition
  `owner_wait_advisor` with `paid_turn_allowed=false`. The benchmark answer was
  not produced, so this closes the control incident but is not a mathematical
  solve.
