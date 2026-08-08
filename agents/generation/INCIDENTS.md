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
