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
