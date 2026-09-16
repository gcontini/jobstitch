# jobstitch server

The compute half of jobstitch: it turns a job description into tailored CV
data, compiles LaTeX into a PDF, analyses postings and writes cover letters.

It is **stateless**. It holds your provider API keys and a set of default
prompts, and nothing else: your profile, your contact details, your
preferences and your signature arrive with each request and are gone when it
ends. There is no database, no job queue and no uploaded-file store.

Every response — success or failure — carries what the server did and what it
cost, so the client can write a full log next to each CV without the server
remembering anything.

---

## Run it

```bash
docker build -f server/Dockerfile -t jobstitch-server .
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
there.

`docker compose up --build` from this directory does the same with the
settings below already wired.

---

## Configure it

### API keys

The server calls three models. Which, and where, is declared in
`src/jobstitch_server/resources/models.toml`; the keys themselves come from
the environment:

```bash
DASHSCOPE_API_KEY=sk-...
DASHSCOPE_BASE_URL=https://dashscope-intl.aliyuncs.com/compatible-mode/v1
```

Any OpenAI-compatible `/chat/completions` endpoint works — OpenAI, DeepSeek, a
local Ollama. `models.toml` ships with those as commented blocks to paste in.
A model whose key is not set borrows the endpoint of one that is, so a single
key runs the whole pipeline.

| Role | What it does | Shipped as |
|---|---|---|
| `summary` | JD detection, JD analysis, cover letters | `qwen-plus`, thinking on, low effort |
| `cv` | Writing the CV and reviewing it | `qwen-max`, thinking on, 6000-token budget |
| `highlight` | The `**bold**` keyword pass | `qwen-plus`, thinking **off** |

Thinking is off for the highlighter deliberately: letting a reasoning model
think about inserting markers burns the output budget and returns truncated
JSON.

### Environment

| Variable | Default | What it does |
|---|---|---|
| `PORT` / `HOST` | `8080` / `0.0.0.0` | Where uvicorn listens |
| `WEB_CONCURRENCY` | `1` | uvicorn worker processes |
| `JOBSTITCH_API_TOKEN` | unset | Bearer token clients must send. **Unset means no auth.** |
| `JOBSTITCH_RESOURCES` | baked in | Directory of replacement prompts/template/`models.toml` |
| `JOBSTITCH_WORK_DIR` | system temp | Parent of the per-request scratch directories |
| `JOBSTITCH_MAX_CONCURRENT_JOBS` | `10` | Requests in flight; the rest get `429` |
| `JOBSTITCH_LATEX_TIMEOUT` | `120` | Seconds before a compile is killed |
| `JOBSTITCH_REQUEST_BUDGET_SECONDS` | `1200` | Wall clock for one CV run before `504` |
| `JOBSTITCH_MAX_ATTEMPTS` | `4` | Generate → review → render rounds |
| `JOBSTITCH_MAX_PART_BYTES` | `2000000` | Cap on any one uploaded part |
| `JOBSTITCH_JD_MIN_CHARS` / `_MAX_CHARS` | `1000` / `10000` | Length band for the free JD check |
| `JOBSTITCH_LOG_HISTORY` | `200` | Requests whose log stays fetchable from `/logs/{id}` |
| `JOBSTITCH_LOG_LEVEL` | `INFO` | Logging level |

### Prompts and the template

The four prompts and `resume3.tex.jinja` ship inside the image. To change
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

A failed request returns the same body with `ok: false` and a populated
`error`. Nothing else rides on a reply: what the server did is behind
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

The last `JOBSTITCH_LOG_HISTORY` requests are kept, in memory, per instance —
this is the only state the server holds. An id that has aged out, or that was
served by a different instance behind a load balancer, returns `404`
`unknown_request`. Ask for the log soon after the request, which is what the
client does.

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
     -F pers_preferences=@pers_preferences.md \
     localhost:8080/v1/jd/analysis
```

Required: `jd`, `candidate_profile`, `pers_preferences`. Optional:
`temperature`. Returns a `JDAnalysis` — match percentage and rationale, title,
location, work mode, salary, seniority, hard and soft skills, company, whether
it is a direct or agency posting, the gaps against your profile and a score
against your stated preferences.

### `POST /v1/cv`

The expensive one: minutes of model calls. Writes the CV, reviews it against
your profile, renders it, and condenses it until it fits two pages.

```bash
curl -F jd=@JD.txt \
     -F candidate_profile=@candidate_profile.json \
     -F candidate_data=@candidate_data.json \
     localhost:8080/v1/cv > document.json
```

| Part | | |
|---|---|---|
| `jd` | required | The posting |
| `candidate_profile` | required | Everything you have done — what the model tailors from |
| `candidate_data` | required | Any JSON object — name, email, phone, whatever else your template reads. Copied to the CV verbatim, never sent to a model |
| `sys_prompt_cv`, `sys_prompt_highlight`, `sys_review_prompt` | optional | Replace a prompt for this request |
| `template` | optional | Replace `resume3.tex.jinja` |
| `signature` | optional | Your signature PNG |
| `temperature`, `max_attempts` | optional | Tuning |

Returns a **CV document**: the model's output plus your candidate data, which
is what `POST /v1/cv/render` takes. It does not return a PDF — rendering is a
separate, cheap call, so you can edit the document and re-render without
paying to write it again.

Why it needs `candidate_data` and the template even though it returns JSON:
the two-page limit is enforced by actually compiling the CV and counting the
pages, so the instruction fed back to the model ("remove one bullet point") is
grounded in a real overflow. A page count taken against a different template
than you will render with would mean nothing.

### `POST /v1/cv/render`

```bash
curl -F document=@document.json localhost:8080/v1/cv/render
curl -F tex=@cv_edited.tex       localhost:8080/v1/cv/render
```

Send a document **or** a hand-edited `.tex`, not both. Optional `template` and
`signature`. Returns `{tex, pdf_base64, pages, advice}` — the LaTeX that was
compiled, the PDF, and whether it fits.

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

`POST /v1/cv` runs for minutes. That is the one thing to plan around.

- **Request timeout.** Cloud Run defaults to 5 minutes and allows up to 60 —
  raise it. An AWS ALB idles out at 60 seconds by default. Whatever sits in
  front must outlast `JOBSTITCH_REQUEST_BUDGET_SECONDS`, or the client will
  see a proxy error instead of the server's own `504`.
- **Concurrency.** Ten jobs in flight is the default; each is mostly idle
  waiting on the provider, but each also compiles LaTeX several times. Size
  memory and CPU for the compiles, not the waiting. Beyond the limit the
  server answers `429` with `Retry-After` immediately rather than queueing a
  caller for minutes.
- **Scaling.** Nothing is shared between requests, so any number of instances
  behind a load balancer works. Do not set a request-affinity policy; there is
  no session to be sticky about.
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
| `422` with `"stage": "compile"` | The template or the `.tex` does not compile. `GET /logs/{request_id}` has the TeX log tail, which says where |
| `429` with `Retry-After` | All job slots are busy — retry, or raise `JOBSTITCH_MAX_CONCURRENT_JOBS` |
| `502` with `"stage": "cv.generate"` | The model never produced valid output. `GET /logs/{request_id}` shows each failed attempt |
| `404` `unknown_request` from `/logs` | The id aged out of the ring buffer, or another instance served that request |
| `504` | The compile, or the whole run, hit its timeout |
| `"status": "degraded"` on `/healthz` | No `pdflatex` on PATH — CV and render calls will fail |
| Model name in `/healthz` is not what you configured | A role with no API key borrowed a configured endpoint; the startup log says which |

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
