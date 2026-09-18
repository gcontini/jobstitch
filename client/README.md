# jobstitch

Copy a job posting. Get back a tailored two-page CV as a PDF, filed in a
dated folder with the posting, the analysis, the LaTeX source and a log of
what it cost.

`jobstitch` is a single executable. It keeps your CV data on your machine and
sends it, per request, to a [jobstitch server](../server/README.md) that holds
the model API keys and the LaTeX toolchain. You need no Python, no API key of
your own and no LaTeX install (for the client).

---

## Set it up

1. **Download** the binary for your platform and put it wherever you like —
   `~/bin/jobstitch`, `C:\Tools\jobstitch.exe`. On Linux: `chmod +x jobstitch`.

2. **Put your files next to it.** They are picked up by name:

   | File | What it is |
   |---|---|
   | `candidate_profile.json` | Everything you have ever done. The model tailors the CV from this — the more complete, the better. **Required.** |
   | `candidate_data.json` | Name, email, phone, LinkedIn, languages, location, education. Merged into the CV here and printed as-is; **Required.** |
   | `candidate_preferences.md` | What you want from a job, in prose. Scored against each posting. **Required** for analysis. |
   | `resume.tex.jinja` | Your own LaTeX template. Optional — the server's is used otherwise. |
   | `sys_prompt_cv.txt`, `sys_prompt_highlight.txt`, `sys_prompt_review.txt`, `sys_prompt_letter.txt` | Your own prompts. Optional, same. |

   Every `.png`, `.jpg` and `.jpeg` in the folder is sent along too, under its
   own file name — that is the name your template includes it under, so
   `\includegraphics{photo.jpg}` is satisfied by a `photo.jpg` sitting next to
   you. `candidate_signature.png` is just one such image: the stock template
   includes it, and a blank one is used if you have none.

   Start from the fictional set in [`examples/candidate/`](../examples/candidate)
   — copy them into your current folder and edit.

3. **Point it at a server.** Either a `jobstitch.toml` in your current folder:

   ```toml
   server_url = "https://jobstitch.example.run.app"
   token      = "the-bearer-token"      # only if the server requires one
   cover_letter = "no"                  # no | yes | letter_only
   # temperature = 0.4                  # optional
   # debug   = true                     # same as -d on every run
   # verbose = true                     # same as -v on every run
   ```

   …or `--server`/`--token` on the command line, or `JOBSTITCH_API_URL` and
   `JOBSTITCH_API_TOKEN` in the environment.

4. **Check it works:**

   ```bash
   cd examples
   jobstitch submit-raw posting.txt --server ... [--token ...]
   ```

Everything resolves in this order: **command line → environment →
`jobstitch.toml` → file in the current folder → the server's default.** The
`[files]` section of `jobstitch.toml` can point anywhere:

---

## The modes

### `clipboard` — copy a posting, get a CV

```bash
jobstitch clipboard --out ~/applications
```

Watches the clipboard. When you copy something that looks like a posting it
checks with the server, analyses it, prints the analysis and asks:

```
Paste the posting URL to submit, y = submit without link, s = skip, q = quit
> https://jobs.example.com/12345
```

Then it writes the CV, renders the PDF, and files everything. Ctrl+C to stop.

On Linux the clipboard needs a helper: `apt install xclip` (X11) or
`wl-clipboard` (Wayland). On Windows and macOS it works as-is.

### `watch` — drop files into a folder

```bash
jobstitch watch --in ~/Downloads/postings --out ~/applications
```

Every file dropped into `--in` is claimed immediately, checked, analysed and
put to you for confirmation. Files that are not job postings go to `error/`
with a timestamp in the name. If a previous run left anything behind, you are
asked at startup whether to resume it or clean it out.

### `submit` — one file, once

```bash
jobstitch submit posting.txt --out ~/applications --yes
```

The same pipeline for a single file, then it exits. No recovery prompt. The
file you name is **copied**, not consumed — unlike `watch`, which empties the
folder it is watching.

### `submit-raw` — just the CV, right here

```bash
jobstitch submit-raw posting.txt
jobstitch submit-raw posting.txt -o cv.pdf -o cv.json -o cv.tex
```

The CV and nothing else: no detection, no analysis, no question before
spending, no cover letter, no spreadsheet, no folders. Files land in the
directory you are standing in.

`-o` is repeatable and the suffix is the whole instruction — `.pdf` writes the
PDF, `.json` the document, `.tex` the LaTeX. A name you chose is overwritten
without asking. With no `-o` you get one PDF named after the posting
(`posting.txt` → `posting.pdf`), and a second run counts up to `posting_1.pdf`
rather than replacing it.

### `render` — compile an edited CV

```bash
jobstitch render ~/applications/cv/26-01-15/Acme_Head_of_IT/cv_Jordan_Rivera.json
jobstitch render cv_Jordan_Rivera.tex
```

Give it the `.json` document to re-render from the content, or the `.tex` to
compile a hand-edit. The PDF lands next to the input. Needs no profile and no
posting — just the server.

The `.json` is one flat object: what the model wrote with your own candidate
data already merged in. Edit any of it and re-render; it costs no model call.

### `logs` — what the server did

```bash
jobstitch logs b510f8dff047
```

Every failure names a request id. This prints that request's server-side log
— each model call with its duration and token counts, and the error that
ended it. The server keeps the last couple of hundred requests, so ask
reasonably soon; an id that has been dropped gives `404`.

### Switches

| Flag | Applies to | What it does |
|---|---|---|
| `--out DIR` | clipboard, watch, submit | The output folder. Default: the current folder. |
| `-o FILE` | submit-raw | Where to write one output: `.pdf`, `.json` or `.tex`. Repeatable. |
| `--in DIR` | watch | The folder to watch. Default: `./incoming` (created if missing). |
| `--cover-letter no\|yes\|letter_only` | clipboard, watch, submit | Also write a cover letter, or write *only* one. Default `no`. |
| `--yes` | clipboard, watch, submit | Submit every valid posting without asking. Unattended runs spend tokens on their own. |
| `--no-xlsx` | clipboard, watch, submit | Do not record delivered CVs in the spreadsheet. |
| `--server`, `--token`, `--temperature` | all | Override the config for one run. |
| `--pages N` | clipboard, watch, submit, submit-raw | Page limit the CV must fit. Default: 2. |
| `--config FILE`, `--data-dir DIR` | all | Use a specific config, or look for your files somewhere else. |
| `-d`, `--debug` | all | Fetch the server's log after **every** call and fold it into `log.log`. Without it only failures are fetched. |
| `-v`, `--verbose` | all | While a CV is being written, print what the server reports about each step it finishes — tokens, thinking tokens, elapsed, and the reviewer's or the page check's own words. |

Both work on either side of the mode name: `jobstitch -v submit posting.txt
--out ~/applications` and `jobstitch submit -v …` do the same thing.

---

## What you get

```
~/applications/
├── cv/
│   └── 26-01-15/
│       └── Acme_Corp_Head_of_IT/
│           ├── cv_Jordan_Rivera.pdf     the CV
│           ├── cv_Jordan_Rivera.tex     the LaTeX it was compiled from
│           ├── cv_Jordan_Rivera.json    the content, for re-rendering
│           ├── analysis.json            match score, salary, skills, gaps
│           ├── jd.txt                   the posting (or its original filename)
│           ├── cover_letter.txt         with --cover-letter
│           └── log.log                  every step, plus the server's own on failure
├── discarded/26-01-15/…                 postings you said no to, analysis kept
├── error/26-01-15-09-30-00_notes.txt    rejected or failed, with a .log beside it
├── working/                             in flight; empty when nothing is running
└── applications.xlsx                    one row per delivered CV
```

To revise a CV: edit `cv_Jordan_Rivera.json` and run `jobstitch render` on it.
That costs one LaTeX compile and no model calls. Editing the `.tex` and
rendering that works too, if you would rather work in LaTeX.

The spreadsheet's own header row is the schema — rename, reorder or add
columns in your copy and the rows follow.

`log.log` holds the client's own steps. It also holds the server's account of
a request — always when one fails, and for every call when you pass `--debug`:

```
2026-01-15T09:30:12 ✍ writing the CV (this takes minutes)...
--- server request b510f8dff047 ---
  2026-01-15T09:30:53 INFO    [cv.generate]   [cv] qwen-max 41.2s | prompt=7104, completion=1832, thinking=903
  2026-01-15T09:31:03 INFO    [cv.review]     [cv] qwen-max 9.8s | prompt=5210, completion=96
```

One line per model call is what it cost. To read it for a run that has already
finished, `jobstitch logs <request id>`.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `cannot reach the jobstitch server at …` | The server is down or the URL is wrong. `curl <url>/healthz` to check. |
| `401` / `a valid bearer token is required` | The server wants a token: set `token` in `jobstitch.toml` or pass `--token`. |
| `candidate_profile.json is required but was not found` | Put it in the current folder, or name it under `[files]` in the config. |
| `no clipboard here: neither DISPLAY nor WAYLAND_DISPLAY…` | Install `xclip` or `wl-clipboard`, or use `watch` instead — it needs no clipboard. |
| `429` / `all job slots are busy` | The server is at capacity. Try again shortly. |
| `clipboard: not a posting (412 chars, nothing sent)` | What you copied is a fragment, not a posting. Nothing was sent anywhere. |
| A posting you wanted lands in `error/` | Read the `.log` beside it: it holds the server's own log for the call that failed. A wrong "not a job description" verdict usually means the copy grabbed only part of the page. |
| You need more than the error line | Re-run with `-d`, or `jobstitch logs <request id>` while the server still has it. |
| `working/` is not empty | A previous run stopped mid-job. `clipboard` and `watch` offer to resume or clean at startup. |

---

## What leaves your machine

Per request: the posting text, your `candidate_profile.json`, your
`candidate_preferences.md` (for the analysis), your `candidate_data.json` and
every image in the folder. Whether *the model provider* keeps what it is
shown is between you and whoever runs the server — the same question as with
any hosted model.

**No model is ever shown your `candidate_data.json`.** It is sent because the
server compiles the CV to count its pages, and a page count taken with the
contact block missing is not the page count of the CV you will send; it goes
to the LaTeX template and nowhere else. The server holds a finished CV in a
directory of its own until a couple of hundred newer requests have pushed it
out.

Nothing is sent for text that fails the local length and binary checks, so a
copied password or a screenful of code never leaves the machine.
