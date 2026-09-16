# Copilot Instructions — jobstitch

**jobstitch** turns a job description into a tailored, two-page LaTeX CV and
an optional cover letter. It is a `uv` workspace of three packages, split so
each can be deployed and tested independently:

```
contracts/   jobstitch_contracts  — pydantic models + envelope + the free JD guess
server/      jobstitch_server     — stateless FastAPI service: LLM calls + pdflatex
client/      jobstitch_client     — the thing you run: clipboard/watch/submit/render
```

Dependency direction is one-way — `client → contracts ← server`, never
`client ↔ server` — and enforced by `tests/test_architecture.py` (AST-based:
it fails the build if either side imports the other, or if `contracts` pulls
in anything heavier than pydantic). Read [AGENTS.md](../AGENTS.md) first for
the principles this design follows (SOLID, one job per class, no hidden
global state); this file is the file map and the day-to-day commands.

## Components (file map)

**`contracts/src/jobstitch_contracts/`** — the wire format, nothing else
- `cv.py` — `TailoredCVData` (LLM output; every `Field(description=...)` is
  restated in a prompt's JSON schema — editing one changes model behaviour),
  `CVDocument` (`cv` + a raw `candidate: dict` that is never sent to a model,
  copied through verbatim; `render_context()` flattens both for Jinja and
  raises on a key collision between them).
- `jd.py` — `JDAnalysis` (the analysis endpoint's output), `JDDetection`
  (`is_job_description: bool`, nothing else — the reason lives in the log,
  not the reply).
- `envelope.py` — `Envelope[T]` (`request_id`, `ok`, `data`, `error` — every
  endpoint returns this shape, success or failure), `LogEntry`, `RequestLog`.
  No usage/token fields here: each model call logs its own spend as one line.
- `guess.py` — `static_jd_guess(text) -> bool`, the free structural check
  (length band, no binary) both sides can run before paying for anything.
- `payloads.py` — `RenderedCV`, `CoverLetter`, `ServerStatus`.

**`server/src/jobstitch_server/`** — stateless; personal data arrives per
request and is dropped when it ends
- `api/app.py` — `create_app()`, the ASGI middleware that propagates the
  request id / log collector through `run_in_threadpool` via contextvars.
- `api/deps.py` — `AppState`, `envelope_of()`, `execute()` (owns the
  concurrency semaphore + threadpool hop + per-request scratch dir).
- `api/routers/{health,jd,cv,letter,logs}.py` — one router per resource.
  `logs.py` is `GET /logs/{request_id}` against `logstore.py`'s bounded
  in-memory ring (`JOBSTITCH_LOG_HISTORY`, default 200) — the one piece of
  state the server holds, and it is disposable.
- `api/errors.py` — maps `PipelineError`/`ValidationError`/`openai.APIError`
  to a status and an `ok:false` envelope; never echoes the provider body.
- `pipeline/cv_generator.py` — `CVGenerator`: write → review → render →
  condense-to-two-pages (up to `max_attempts` rounds) → keyword highlight.
  Pure library code — inputs in memory, outputs returned, nothing read from a
  configured path.
- `pipeline/cv_renderer.py` — `CVRenderer`: Jinja (`\VAR{}`/`\BLOCK{}`
  delimiters, `DictLoader` built fresh per request since the template can be
  client-supplied) → `pdflatex`, sandboxed (`-no-shell-escape`,
  `openin_any=p`/`openout_any=p`, no stdin, a timeout, its own scratch
  `mkdtemp` removed in a `finally`) → page count.
- `pipeline/jd_validator.py`, `pipeline/letter_generator.py` — the other two
  LLM pipelines; same shape as `cv_generator.py`.
- `model_selector.py` — `ModelSelector`, one OpenAI-compatible endpoint per
  role (`summary`/`cv`/`highlight`) from `resources/models.toml`. Every LLM
  call ends with one `logger.info` line carrying model, duration and token
  counts — this is the entire token-accounting story; there is no per-request
  total anywhere.
- `bundle.py` — `ResourceBundle` (impersonal: prompts, template, signature —
  built once at startup, `with_overrides()` per request) vs.
  `CandidateInputs` (personal: profile, candidate data, preferences — always
  per-request, never cached, never written to disk).
- `observability.py` — `Run`/`NullRun`, `current_run` contextvar, `stage()`
  context manager, the logging handler that turns log records into
  `LogEntry`s for the run.
- `resources/` — the impersonal defaults baked into the image: four prompts,
  `resume3.tex.jinja`, `models.toml`, a placeholder signature PNG.

**`client/src/jobstitch_client/`** — your data, a bearer token, no Python
required to run it (PyInstaller `--onefile`)
- `api.py` — `JobstitchApi` (Protocol) / `HttpApi` (httpx) — the only module
  that knows the server exists. `JobstitchError` carries `request_id` so a
  caller can fetch what the server did.
- `config.py` / `discovery.py` — settings resolution, highest precedence
  first: CLI flag → env var → `jobstitch.toml` → file found next to the
  executable → server default. `CONFIG_KEYS` maps short TOML keys to the
  filenames `discovery.py` looks for.
- `runner.py` — `JobRunner`: the one place the step order is written
  (detect → analyze → confirm → cv → render → letter → deliver). Every API
  call goes through `_call()`, which fetches the request's server log when
  `--debug` is set, or unconditionally on failure.
- `joblog.py` — `JobLog` (per-job `log.log`, client steps + folded-in server
  log), `format_entries()` (same rendering, for `jobstitch logs <id>`).
- `workspace.py` — `Workspace`: owns the `working/error/discarded/cv` folder
  tree, atomic moves, the daily subfolders. `take_in(path, move=...)` — moved
  for a watched inbox, copied for a named argument (`submit` must not consume
  the file you pointed it at).
- `sources/{clipboard,folder,single}.py` — the only difference between the
  four modes; each yields `JDCandidate`s to the same `JobRunner`.
- `modes/{clipboard,watch,submit,render,logs}.py` — thin: build a source,
  hand it to `Session`/`JobRunner`, or (for `render`/`logs`) call the API
  directly. `modes/__init__.py`'s `Session` is the shared wiring.
- `tracking.py` — `XlsxTracker` (openpyxl, `applications.xlsx`) / `NullTracker`.
- `ui.py` — `Confirmer` protocol (`PromptConfirmer` / `AutoConfirmer` for
  `--yes`), the analysis table, the recovery prompt.

**Shared**
- `examples/candidate/` — a fictional profile/data/preferences/signature set,
  shipped so a fresh download has something to run against immediately.
- `tests/test_architecture.py` — the one-way dependency rule, enforced.
- `tests/test_integration.py` — client and server together in one process
  (httpx against the real ASGI app via `starlette.testclient.TestClient`,
  only the model call faked) — the one test that catches the two sides
  disagreeing about the wire format.

## Key contracts & invariants

- **Every response is the same envelope**, success or failure:
  `{request_id, ok, data, error}`. Nothing else rides on it — no logs, no
  usage — so the common case pays nothing for what it does not ask for.
- **`GET /logs/{request_id}`** is the only way to see what a request did.
  Backed by a bounded in-memory ring (`logstore.py`); an id that aged out, or
  landed on a different instance behind a load balancer, is a `404`. The
  client fetches it after every call with `--debug`, and always on failure.
- **Token spend is per model call, not per request.** `model_selector.py`
  logs one line per call (model, duration, prompt/completion tokens); there
  is no aggregate anywhere in the envelope or the contracts.
- **`candidate` on a `CVDocument` is a raw dict**, not a schema — whatever
  your LaTeX template reads, put it in `candidate_data.json` and it flows
  through untouched. The only rule is `render_context()` refusing a key that
  both the model's output and your data define.
- **The LaTeX subprocess is always sandboxed**: shell escape off
  (`-no-shell-escape`, `shell_escape=f`), reads/writes confined to the
  per-request scratch dir (`openin_any=p`/`openout_any=p`), no stdin, a
  timeout. A client can supply the template *and* a whole `.tex` — treat both
  as hostile input. On a compile failure the TeX log tail goes to the
  server's own log, not into the error body (it is kilobytes; ask for it via
  `/logs` if you need it).
- **`submit` copies its input; `watch` moves it.** An inbox gets emptied; an
  argument you named does not disappear.
- **Concurrency**: one `BoundedSemaphore(JOBSTITCH_MAX_CONCURRENT_JOBS)`
  (default 10) gates job handlers → `429` + `Retry-After` when full. No
  second gate for the LaTeX compile — it is fast enough not to need one.
- **Every model reply is re-validated.** The schema is shown in the prompt
  *and* requested via `response_format`, and the reply is parsed by pydantic;
  a `ValidationError` is fed back to the model rather than trusted.

## Conventions

- Python 3.12, `uv` workspace (`pyproject.toml` at the root plus one per
  package); `uv sync` installs all three in editable mode. Console scripts:
  `jobstitch-api` (server), `jobstitch` (client).
- LLM access via any OpenAI-compatible endpoint — `DASHSCOPE_API_KEY`,
  `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, or a local Ollama; see
  `.env.example`. Roles and models are declared in
  `server/src/jobstitch_server/resources/models.toml`
  (`summary`/`cv`/`highlight`); a role with no key set borrows the endpoint
  of one that has it.
- `%`-style lazy logging args, never f-strings inside `logger.*` — sanitized
  LaTeX can reach a log line and a literal `%` would break the formatter.
- LaTeX templates use Jinja delimiters `\VAR{}`/`\BLOCK{}`; escaping is done
  by `_sanitize_latex`/`_sanitize_obj` in `cv_renderer.py` — keep `{}` guard
  groups intact.
- Console prints in the client use `flush=True` (long-lived watch/clipboard
  processes).
- Docker: **classic builder only** — no BuildKit features, no heredocs in a
  Dockerfile (`server/Dockerfile`, built with `DOCKER_BUILDKIT=0`).
- PyInstaller (`client/packaging/jobstitch.spec`), not Cython — Cython still
  needs an interpreter and does not produce a standalone exe. Build on the
  target OS; it does not cross-compile.
- Keep the code simple; this is a personal tool wearing production-shaped
  seams (SOLID, tests, sandboxing) because it is a portfolio piece — do not
  add speculative options beyond what is asked.

## Commands

```bash
# setup (once, after cloning or when deps change)
uv sync

# run from source
uv run jobstitch-api                       # the server
uv run jobstitch clipboard --out ~/applications   # the client

# tests — all four suites, no API key, no network
uv run pytest
uv run pytest server/tests                 # one package only
uv run pytest -k architecture              # the dependency-direction rule

# the server image (classic builder only)
DOCKER_BUILDKIT=0 docker build -f server/Dockerfile -t jobstitch-server .
docker run --rm -p 8080:8080 --env-file .env jobstitch-server

# the client executable (build on the OS you are targeting)
uv run --with pyinstaller pyinstaller client/packaging/jobstitch.spec
```

`pdflatex`-dependent tests skip themselves when it is not on `PATH`.
`server/tests/server_helpers.py` wires a real `ModelSelector` to a fake HTTP
client, so request assembly and structured-output negotiation stay under
test with no network call.

## Testing guidance

- No test may need an API key, a network socket, or a terminal.
- Fake at the transport boundary, not above it — inside `ModelSelector` for
  the server, behind the `JobstitchApi` protocol (`FakeApi`) for the client —
  so the code that assembles a request is what actually runs under test.
- `server/tests/golden/cv_golden.tex` pins the whole render byte-for-byte; an
  escaping regression is otherwise invisible until someone reads a bad PDF.
- `tests/test_integration.py` is the one place both packages run together;
  keep it passing before touching either side's contract usage.

## Interaction with the user

- The user is skilled: be concise.
- If a design decision was not addressed or clarified in the prompt and
  implementing it one way forecloses another, stop and ask.

## Code style

- Clean up local variables that are only assigned or incremented with no
  effect on the logic.
- Keep decisions as simple as possible — this is not over-engineered
  production code; it borrows production patterns (typed contracts,
  sandboxing, tests) only where the split into a server/client actually
  requires them.
- If a decision introduces a lot of complexity for a small improvement, stop
  and ask before implementing it.
- Prefer clear contracts with mandatory parameters over defaults scattered
  across a method signature.
- Never commit or push — `git commit`/`git push` are strictly forbidden; the
  user reviews and commits every change.
