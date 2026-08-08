# Rethlas

Rethlas is a natural-language reasoning system for mathematics built around two Codex agents:

- The generation agent reads a math problem from a markdown file and writes an informal proof blueprint.
- The verification agent checks that proof blueprint, produces a structured verdict, and serves as the generation agent's verifier.

The intended deployment order is:

1. Start the verification agent as a local HTTP service.
2. Run the generation agent through Codex.
3. Let the generation agent call the verification service during its proof-and-repair loop.

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
git clone https://github.com/frenzymath/Rethlas.git
cd Rethlas
```

## 3. Start the Verification Service


```bash
cd agents/verification
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn api.server:app --host 127.0.0.1 --port 8091
```

Using uv
```bash
cd agents/verification
uv venv 
uv pip install -r requirements.txt
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

The verifier is resource-bounded by default. The main controls are:

- `VERIFY_CONTEXT_MAX_CHARS=200000` per proof item
- `VERIFY_MAX_TOTAL_CONTEXT_CHARS=5000000` per blueprint
- `VERIFY_MAX_PROOF_CHARS=2000000`
- `VERIFY_MAX_REQUEST_BYTES=25265536` before JSON parsing (derived default)
- `VERIFY_BODY_TIMEOUT_SECONDS=30` for the complete request upload
- `VERIFY_MAX_ITEMS=128`
- `VERIFY_MAX_PROMPT_BYTES=500000` per serialized model prompt
- `VERIFY_MAX_TOTAL_PROMPT_BYTES=5000000` per request
- `VERIFY_MAX_CONCURRENT_REQUESTS=1`
- `CODEX_TIMEOUT_SECONDS=3600` per item
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
python3 -m venv .generation-venv
source .generation-venv/bin/activate
pip install -r generation/mcp/requirements.txt
cd generation
./tests/run_example.sh
```

The generation MCP environment deliberately lives beside, rather than inside,
`agents/generation`: the generation Codex can write its workspace, so an
interpreter or site-packages directory placed there cannot be part of the
publication trust boundary. The runner rejects Python environments inside the
generation workspace or the system temporary directory.

This script:

- reads `agents/generation/data/example.md`
- runs `codex exec` inside `agents/generation`
- starts a fresh Codex session for each iteration, alternating search-disabled
  and search-enabled turns, and resumes only from the current blueprint plus
  budgeted persisted memory
- stops only when the verified file matches a publication receipt written
  outside the generation agent's writable workspace
- hashes the project MCP/config/agent runtime before and after every iteration;
  attempted cross-iteration publisher modification stops the run fail-closed;
  bytecode caches, symlinks, and special files in that trusted tree are rejected
  before Codex starts, and the MCP interpreter runs with bytecode writes disabled
- pins the MCP command to an attested runtime snapshot outside the writable
  generation workspace, so an MCP restart cannot load temporarily modified code;
  these per-run snapshots live under `agents/.trusted_generation_runtime/`
  and are ignored by Git
- writes iteration logs to `agents/generation/logs/example/iter/`
- writes memory artifacts to `agents/generation/memory/example/`
- writes the draft proof to `agents/generation/results/example/blueprint.md`
- binds the target to the startup SHA-256 of `data/example.md`, then writes the
  verified proof and trusted receipt only if verification succeeds

## Lazy proof verification

Rethlas parses a paper-style blueprint into content-addressed proof items. Each
item is verified with its complete local proof plus the statements and edges of
its dependency closure. Ancestor proof bodies are intentionally omitted.

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
truncation. Each item response is bound to its proof/context digests and exact
item id. The API accepts the final proof only when `checked_item_ids` exactly
equals the complete manifest. The generation MCP independently recomputes the
same manifest attestation and atomically publishes the verified bytes only if
the draft has not changed during verification. The production MCP tool accepts
only `problem_id`; it reads `data/{problem_id}.md` itself and checks the
runner-bound source digest, so the model cannot swap in an easier target.
Publication also writes a receipt under `agents/.verification_receipts/`, which
is outside the generation Codex workspace. The example runner validates that
receipt, the target digest, exact verified bytes, independently recomputed item
coverage/context digest, regular-file type, and bounded size instead of treating
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

Then run:

```bash
cd agents/generation
source .venv/bin/activate
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
