# Working in this repository

Rules the code follows. They are not style preferences; each one is here
because breaking it has a cost we have already paid once.

## Shape

- **One job per class.** `HttpApi` speaks HTTP and nothing else. `Workspace`
  owns paths and moves and nothing else. `JobRunner` knows the order of steps
  and delegates every one of them. If you cannot say what a class does in one
  sentence without "and", split it.
- **Depend on protocols, not implementations.** `JobstitchApi`, `JDSource`,
  `Confirmer`, `Tracker` exist so the modes can be tested with no server, no
  terminal and no spreadsheet. A new source of job descriptions should be a
  new `JDSource` and no change anywhere else.
- **One-way dependencies:** `client → contracts ← server`. Neither side
  imports the other; `tests/test_architecture.py` enforces it. The contracts
  package imports nothing but pydantic — it is loaded by a web server and by a
  frozen executable, and has to stay cheap in both.
- **The pipeline is a library.** Everything under `jobstitch_server/pipeline/`
  takes its inputs in memory and returns its outputs. It reads no
  configuration, resolves no paths and writes nothing outside the scratch
  directory it is handed. That is what makes it safe to run per request.
- **No hidden global state.** Configuration is passed in, not imported.
  Anything cached at module level must be immutable and identical for every
  request; anything personal must not be cached at all.

## Restraint

- **Build what was asked.** No speculative options, no "while we are here".
  A knob nobody asked for is a knob that must be documented, tested and kept
  working.
- **Prefer deleting.** The three-model rework removed a whole second CV
  pipeline; the client/server split removed a thread pool and a folder
  protocol. Less surface has consistently been the better answer here.
- **Simple over clever.** The watcher processes one posting at a time because
  it asks you a question about each one; concurrency would buy nothing and
  cost a queue.

## Correctness

- **Every model reply is re-validated.** The schema goes into the prompt *and*
  into `response_format`, and the reply is parsed by pydantic. Providers
  differ in how much structure they enforce, so a weak `response_format`
  costs a retry, never correctness.
- **Prompt text is behaviour.** The `Field(description=...)` strings end up in
  the JSON schema shown to the model. Editing one changes what the model
  produces — treat those classes as prompts, not just as types.
- **Failures carry a cause and a stage.** Every response, successful or not,
  carries a `request_id`; `GET /logs/{id}` turns it into what ran and what
  each model call cost. A user who paid for four attempts is entitled to see
  all four — but a reply nobody reads should not carry them.
- **The LaTeX subprocess is always sandboxed.** Shell escape off, reads and
  writes confined to the scratch directory, no stdin, a timeout. The template
  and the `.tex` can both come from a client: treat them as hostile input.
- **Pure where it can be.** `render_tex`, `render_context`, `static_jd_guess`
  take values and return values. They are the easiest things to test and the
  hardest things to break.

## Tests

- No test may need an API key, a network or a terminal.
- Fake at the boundary, not above it: the test double replaces the HTTP client
  *inside* a real `ModelSelector`, so request assembly, structured-output
  negotiation and usage logging stay under test.
- `server/tests/golden/cv_golden.tex` pins the whole render. An escaping
  regression is otherwise invisible until someone reads a bad PDF.
- Tests that need `pdflatex` skip themselves; everything else always runs.

## House rules

- `uv` for everything: `uv sync`, `uv run pytest`, `uv lock`.
- Docker: **classic builder only**. No BuildKit features, no heredocs in a
  Dockerfile.
- Do not commit or push. Changes are left in the working tree for review.
