# jobstitch server

The compute half of jobstitch: it turns a job description into tailored CV
data, compiles LaTeX into a PDF, analyses postings and writes cover letters.

It holds your provider API keys and a set of default prompts. Everything
else — your profile, your contact details, your preferences, your images —
arrives with each request and is thrown away when it ends. There is no
database and no user accounts.

It does keep one thing, on the filesystem: a directory per request under
`JOBSTITCH_WORK_DIR`, holding that request's log and, for a CV job, its state
and its finished PDF. Writing a CV takes minutes, so `POST /v1/cv` answers
immediately and the client polls for the rest; that directory is where the
answer waits. The newest 200 are kept and the rest are dropped.

---

## Run it

```bash
docker build -t jobstitch-server .
docker run --rm -p 8080:8080 --env-file .env jobstitch-server
curl localhost:8080/healthz
```

Or, from a checkout:

```bash
uv sync
uv run jobstitch-api
```

The image is ~780 MB, almost all of it the LaTeX toolchain. It runs as any
uid, needs no volume, and works with a read-only root filesystem as long as
`/tmp` is writable (`docker run --read-only --tmpfs /tmp:exec`). `exec` on
that tmpfs is required: `pdflatex` writes and then reads back its font cache
there. A tmpfs also means finished CVs do not survive a restart — mount a
volume at `JOBSTITCH_WORK_DIR` if you want them to.

`docker compose up --build` from this directory does the same with the
settings below already wired.

---

## Configure it

### API keys

The server calls three models, all through one provider (jobstitch assumes a
single API key). The endpoint is declared once, in the `[provider]` table of
`resources/models.toml`; the key itself comes from the
environment:

```bash
MODEL_API_KEY=sk-...
MODEL_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
```

Any OpenAI-compatible `/chat/completions` endpoint works — OpenAI, DeepSeek, a
local Ollama. Switching providers is just replacing those two values in
`.env`; `models.toml`'s `[provider]` table names the same two variables and
doesn't need to change.

| Role | What it does | Shipped as |
|---|---|---|
| `summary` | JD detection, JD analysis, cover letters | `qwen-plus`, thinking on, low effort |
| `cv` | Writing the CV and reviewing it | `qwen3.8-max`, thinking on, 6000-token budget |
| `highlight` | The `**bold**` keyword pass | `qwen-plus`, thinking **off** |

Thinking is off for the highlighter deliberately: letting a reasoning model
think about inserting markers burns the output budget and returns truncated
JSON.

Each role's `model`, `temperature`, `thinking` and `structured_output` (JSON
output mode) can also be overridden per role with an env var, without editing
`models.toml` — handy for a Docker deployment:

```bash
JOBSTITCH_CV_MODEL=qwen-max
JOBSTITCH_CV_TEMPERATURE=0.2
JOBSTITCH_CV_THINKING=off
JOBSTITCH_CV_STRUCTURED_OUTPUT=json_object
```

The pattern is `JOBSTITCH_<ROLE>_<FIELD>` for `SUMMARY`, `CV` and
`HIGHLIGHT`; see `.env.example` for the full list.

### Environment

| Variable | Default | What it does |
|---|---|---|
| `PORT` / `HOST` | `8080` / `0.0.0.0` | Where uvicorn listens |
| `WEB_CONCURRENCY` | `1` | uvicorn worker processes |
| `JOBSTITCH_API_TOKEN` | unset | Bearer token clients must send. **Unset means no auth.** |
| `JOBSTITCH_RESOURCES` | baked in | Directory of replacement prompts/template/`models.toml` |
| `JOBSTITCH_WORK_DIR` | system temp | One directory per request: its scratch space, its log, and a CV job's state and result |
| `JOBSTITCH_MAX_CONCURRENT_JOBS` | `10` | Requests in flight; the rest get `429` |
| `JOBSTITCH_LATEX_TIMEOUT` | `120` | Seconds before a compile is killed |
| `JOBSTITCH_REQUEST_BUDGET_SECONDS` | `1200` | Wall clock for one CV run before `504` |
| `JOBSTITCH_MAX_ATTEMPTS` | `4` | Generate → review → page check rounds |
| `JOBSTITCH_MAX_PART_BYTES` | `2000000` | Cap on any one uploaded part |
| `JOBSTITCH_JD_MIN_CHARS` / `_MAX_CHARS` | `1000` / `10000` | Length band for the free JD check |
| `JOBSTITCH_LOG_LEVEL` | `INFO` | Logging level |

### Prompts and the template

The four prompts and `resume.tex.jinja` ship inside the image. To change
them permanently, mount a directory with your versions and set
`JOBSTITCH_RESOURCES`; anything missing there falls back to the built-in copy.
To change them for one request, upload them as parts — that is what the client
does when it finds them next to its executable.

### Auth

Set `JOBSTITCH_API_TOKEN` and every `/v1/...` call must send
`Authorization: Bearer <token>`. `/healthz` stays open so a platform probe
works. With no token set the server is open to anyone who can reach it, which
is only reasonable on a private network.

---

## The API

Everything is `multipart/form-data` in and JSON out. Every text part can be
sent as a file (`-F jd=@JD.txt`) or as a plain field (`-F jd_text=...`); the
file wins if you send both.

Every response has the same shape:

```jsonc
{
  "request_id": "b510f8dff047",   // also in the X-Request-Id header
  "ok": true,
  "data": { },                    // the payload, when ok
  "error": null                   // set when ok is false
}
```

A failed request returns the same body with `ok: false` and `error` set to
one line saying what went wrong, its kind and its stage:

```json
{"request_id": "b510f8dff047", "ok": false,
 "error": "model_output [cv.generate]: No CV passed review in 4 attempts."}
```

Nothing else rides on a reply: what the server did is behind
`GET /logs/{request_id}`, so the common case pays nothing for it.

### `GET /healthz`

No auth. Reports the version, whether `pdflatex` is present, the model name
per role and whether a token is required.

### `GET /logs/{request_id}`

What the server did during one request, including a line per model call with
its duration and token counts:

```bash
curl localhost:8080/logs/b510f8dff047
```

```jsonc
{"request_id": "b510f8dff047", "ok": true, "data": {"request_id": "b510f8dff047",
 "entries": [{"ts": "…", "level": "INFO", "stage": "cv.generate",
              "message": "  [cv] qwen-max 12.4s | prompt=7100, completion=1850"}]}}
```

The log lives in the request's own directory under `JOBSTITCH_WORK_DIR`, so a
CV job still running answers with what it has said so far, and a restart does
not lose it. The newest 200 directories are kept. An id that has been pruned,
that never did any work (a `400`, `401`, `413` or `429` fails before the
pipeline starts — its `error` already carries the whole cause), or that was
served by a different instance, returns `404` `unknown_request`.

### `POST /v1/jd/detect`

Is this text a job posting? Structural checks first — length band, no binary —
and only if they pass does it cost one small model call. The answer is
`{"is_job_description": true|false}`; why it was rejected is in the request's
log, not in the reply.

```bash
curl -F jd=@JD.txt localhost:8080/v1/jd/detect
```

### `POST /v1/jd/analysis`

Scores a posting against your profile and extracts its facts.

```bash
curl -F jd=@JD.txt \
     -F candidate_profile=@candidate_profile.json \
     -F pers_preferences=@candidate_preferences.md \
     localhost:8080/v1/jd/analysis
```

Required: `jd`, `candidate_profile`, `pers_preferences`. Optional:
`temperature`. Returns a `JDAnalysis` — match percentage and rationale, title,
location, work mode, salary, seniority, hard and soft skills, company, whether
it is a direct or agency posting, the gaps against your profile and a score
against your stated preferences.

### `POST /v1/cv` → `GET /v1/cv/{id}/status` → `GET /v1/cv/{id}`

The expensive one: minutes of model calls. It writes the CV, reviews it
against your profile, renders it, condenses it until it fits the page limit
(2 by default, or `pages` in the request), highlights the keywords and
renders it for good.

It does not wait. The `POST` answers **`202`** with the job's id, you poll for
the status, and you collect everything at the end:

```bash
ID=$(curl -s -F jd=@JD.txt \
          -F candidate_profile=@candidate_profile.json \
          -F candidate_data=@candidate_data.json \
          localhost:8080/v1/cv | jq -r .request_id)

curl -s localhost:8080/v1/cv/$ID/status    # every few seconds
curl -s localhost:8080/v1/cv/$ID > cv.json # once it says END
```

| Part | | |
|---|---|---|
| `jd` | required | The posting |
| `candidate_profile` | required | Everything you have done — what the model tailors from |
| `candidate_data` | required | Your name, email and the rest — what the template prints |
| `sys_prompt_cv`, `sys_prompt_highlight`, `sys_prompt_review` | optional | Replace a prompt for this request |
| `template` | optional | Replace `resume.tex.jinja` |
| `images` | optional | Repeatable. Each part's **file name** is the name the template includes it under. Up to 10. |
| `temperature` | optional | Tuning |
| `pages` | optional | Page limit the CV must fit. Default: 2. |

**Why it needs `candidate_data`, and the template.** The page limit is
enforced by actually compiling the CV and counting the pages, so the
instruction fed back to the model ("remove one bullet point") is grounded in a
real overflow. A page count taken with a different template, or with the
contact block missing, is not the page count of the CV you will send. No model
is shown `candidate_data`: it goes to the renderer and nowhere else.

#### `GET /v1/cv/{id}/status`

Where the job is now, and one line about the step that just finished. There is
no history — poll it, print the status when it changes, and print `detail`
when you want to know what it cost.

```json
{"request_id": "b510f8dff047", "ok": true,
 "data": {"status": "review",
          "detail": "generation finished, tokens used=5341, thinking=610, elapsed=26.6s"}}
```

`status` is one of `generate`, `review`, `re-generate`, `page_check`,
`highlight`, `END`. `detail` is written for a person to read and may span
several lines: a rejected review quotes the reviewer's complaints verbatim,
and a failed page check quotes the condense instruction verbatim, because
those are the words the model is about to be given.

A job that failed answers here with the status and cause the work produced —
`502`, `504`, `422` — so a poll is the only call a client has to handle
failure on.

#### `GET /v1/cv/{id}`

The finished CV: `{document, tex, pdf_base64}`. `409` while the job is still
running; `404` once its directory has been pruned.

`document` is one flat object — what the model wrote with your
`candidate_data` merged over the top, so a field the model invents can never
replace a real contact detail. It has no schema: what the template reads from
it is between you and the template. Store it, edit it, and post it back to
`/v1/cv/render`.

### `POST /v1/cv/render`

```bash
curl -F document=@cv.json     localhost:8080/v1/cv/render
curl -F tex=@cv_edited.tex    localhost:8080/v1/cv/render
```

Send a `document` — the flat object `GET /v1/cv/{id}` returned, or anything
else your template can read — **or** a hand-edited `.tex`, not both. Optional
`template` and `images`. Returns `{tex, pdf_base64}`; `document` is not
echoed back, because whoever asked for the render already has it. Cheap and
deterministic: editing the content and re-rendering never costs a model call.

### `POST /v1/letter`

```bash
curl -F jd=@JD.txt -F candidate_profile=@candidate_profile.json \
     -F analysis=@analysis.json localhost:8080/v1/letter
```

Required: `jd`, `candidate_profile`. Optional: `analysis` (makes the letter
more targeted), `sys_prompt_letter`, `temperature`. When the analysis says the
posting is direct from a named employer and the endpoint supports server-side
web search, the model is told to research the company; an endpoint that
rejects the flag falls back to writing without it.

---

## Deploying

A CV job runs for minutes after the request that started it has been
answered. That is the one thing to plan around.

- **`JOBSTITCH_WORK_DIR` is state now.** A job's status, its result and every
  request's log live there. The default is the system temp directory, which on
  most container platforms is RAM-backed and empty again after a redeploy —
  fine, because the client collects its CV within the minute. Mount a volume
  only if you want results to outlive a restart.
- **One process per work root.** `WEB_CONCURRENCY=1`. The job slots and the
  worker threads are per-process, and a starting process marks every job still
  marked `running` as failed — which is right for its own orphans and wrong for
  a sibling's live jobs.
- **Request timeouts no longer matter much.** Every call now returns in
  seconds; only `/v1/cv/render` waits on a compile. What must still outlast
  `JOBSTITCH_REQUEST_BUDGET_SECONDS` is the client's willingness to keep
  polling.
- **Concurrency.** Ten jobs in flight is the default; each is mostly idle
  waiting on the provider, but each also compiles LaTeX several times. Size
  memory and CPU for the compiles, not the waiting. Beyond the limit the
  server answers `429` with `Retry-After` immediately rather than queueing a
  caller for minutes.
- **Scaling.** A job id only means something to the instance that has its
  directory, so either run one instance, or give them a shared work root and
  route by request id. This is the one thing the old stateless server did not
  ask of you.
- **Cold starts** build three HTTP clients and read six files. It is fast, but
  the first request also has to warm the LaTeX font cache in `/tmp`.

### Sandboxing

A request may supply the LaTeX template, and `/v1/cv/render` accepts a whole
`.tex`. That is arbitrary LaTeX, so every compile runs with shell escape
disabled (`-no-shell-escape`, `shell_escape=f`), reads and writes confined to
the scratch directory (`openin_any=p`, `openout_any=p`), no stdin to block on,
a timeout, and its own `HOME`/`TEXMFVAR`. The scratch directory is deleted
when the request ends. Error responses have the scratch path scrubbed out of
the LaTeX log before it goes back.

---

## Troubleshooting

| What you see | What it means |
|---|---|
| `401` with `WWW-Authenticate: Bearer` | `JOBSTITCH_API_TOKEN` is set on the server and the request had no matching token |
| `413` | A part exceeded `JOBSTITCH_MAX_PART_BYTES` |
| `422` `latex_compile [compile]` | The template or the `.tex` does not compile. `GET /logs/{request_id}` has the TeX log tail, which says where |
| `429` with `Retry-After` | All job slots are busy — retry, or raise `JOBSTITCH_MAX_CONCURRENT_JOBS` |
| `502` `model_output [cv.generate]` from a status poll | The job failed: the model never produced valid output. `GET /logs/{request_id}` shows each failed attempt |
| `404` `unknown_request` | The id was pruned, never did any work, or another instance served it |
| `409` `job_not_ready` from `GET /v1/cv/{id}` | The job is still running — poll `/status` until it says `END` |
| `504` | The compile, or the whole run, hit its timeout |
| `"status": "degraded"` on `/healthz` | No `pdflatex` on PATH — CV and render calls will fail |

---

## Development

```bash
uv sync
uv run pytest server/tests           # no API key, no network
uv run pytest                        # every package, plus the integration test
```

The LaTeX-dependent tests skip themselves when `pdflatex` is absent. Nothing
in the suite calls a model: `server/tests/server_helpers.py` wires a real
`ModelSelector` to a fake HTTP client, so request assembly, structured-output
negotiation and usage logging are all the production code paths.

The layout is described in `src/jobstitch_server/__init__.py`; the rules the
code follows are in `AGENTS.md` at the repository root.
