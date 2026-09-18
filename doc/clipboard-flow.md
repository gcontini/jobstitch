# A run, end to end

One posting, from the moment you press Ctrl+C on a job ad to the folder that
holds the PDF. This is `jobstitch clipboard`; `watch` and `submit` differ only
in what hands the runner the next posting.

## The call sequence

```mermaid
sequenceDiagram
    autonumber
    actor You
    participant Src as ClipboardSource
    participant Run as JobRunner
    participant Api as HttpApi
    participant Srv as jobstitch-server
    participant Ws as Workspace

    Note over Src: polls every 2 s

    You->>Src: copy a posting
    Src->>Src: static_jd_guess(text)
    Note right of Src: too short, too long or binary<br/>→ printed and dropped, nothing sent

    Src->>Run: JDCandidate(text)

    Run->>Api: detect(text)
    Api->>Srv: GET /healthz
    Note right of Api: once per client, 3 attempts, 2 s apart
    Srv-->>Api: 200 ServerStatus
    Api->>Srv: POST /v1/jd/detect
    Srv-->>Api: 200 {is_job_description}
    Note right of Run: false → the run stops here

    Run->>Api: analyze(text, profile, preferences)
    Api->>Srv: POST /v1/jd/analysis
    Srv-->>Api: 200 JDAnalysis
    Run->>Ws: open_job(company, title)
    Ws-->>Run: working/Acme_Corp_Head_of_IT/
    Run->>Ws: write jd.txt + analysis.json

    Run->>You: print the analysis, ask
    You-->>Run: URL · y · s · q
    Note right of Run: s → filed under discarded/<br/>q → filed under discarded/, then stop

    Run->>Api: create_cv(jd, profile, candidate_data, template, images, pages)
    Api->>Srv: POST /v1/cv
    Srv-->>Api: 202 {request_id}

    loop every 4 s until END
        Api->>Srv: GET /v1/cv/{id}/status
        Srv-->>Api: 200 {status, detail}
        Api-->>You: prints the step (and detail with -v)
    end

    Api->>Srv: GET /v1/cv/{id}
    Srv-->>Api: 200 {document, tex, pdf_base64}
    Run->>Ws: cv_You.json · cv_You.tex · cv_You.pdf

    opt --cover-letter yes
        Run->>Api: letter(jd, profile, analysis)
        Api->>Srv: POST /v1/letter
        Srv-->>Api: 200 {text, words}
        Run->>Ws: cover_letter.txt
    end

    Run->>Ws: log.log, then deliver → cv/day/Company_Title/
    Run->>Ws: one row in applications.xlsx
```

## The same run, as state on disk

```mermaid
flowchart LR
    CLIP["clipboard"] --> WORK["working/Acme_Corp_Head_of_IT/<br/>jd.txt · analysis.json"]
    WORK -->|"delivered"| CV["cv/26-01-15/Acme_Corp_Head_of_IT/"]
    WORK -->|"s or q"| DISC["discarded/26-01-15/…<br/><i>analysis kept</i>"]
    WORK -->|"a call failed"| ERR["error/26-01-15-09-30-00_…<br/><i>with its .log</i>"]
```

A job is assembled under `working/` and moved into place in one step when it
is finished, so a folder under `cv/` is never half-written. `Workspace` owns
every path and every move; nothing else in the client builds a path by hand.

## Step by step

**1 — The free check runs on your machine.** `static_jd_guess` lives in
`contracts/` precisely so both sides run the same one: the client uses it
before spending a network call, the server before spending a model call.
Copying a password or a line of code never reaches the network, and the
terminal says so: `clipboard: not a posting (412 chars, nothing sent)`.

**2 — `/healthz` before the first real call.** A freshly started server (a
cold container, a scale-to-zero platform) can take a moment to answer, so the
client knocks up to three times, two seconds apart, once per process — not
once per call. With `-v` it narrates the attempts.

**3 — Detection costs one small model call.** `POST /v1/jd/detect` answers
`{"is_job_description": true|false}`. *Why* something was rejected is in the
server's log for that request, not in the answer — a caller only ever branches
on the verdict. A false verdict on a posting you wanted usually means the copy
grabbed only part of the page.

**4 — Analysis creates the folder.** The company name and job title in the
`JDAnalysis` are what the folder is named after, so the folder cannot exist
before the analysis does. `jd.txt` and `analysis.json` are written
immediately: from here on, a crash is recoverable — `working/` is scanned at
the next startup and you are offered *resume* or *clean*.

**5 — The question is the only thing standing between you and the bill.**
Everything before it costs one or two small model calls; everything after it
costs minutes of the large one. Answer with the posting URL and it is stored
in `analysis.json` (and in the spreadsheet); `y` submits without one; `s`
skips; `q` stops the whole run. `--yes` answers for you, with the URL the
analysis found if it found one.

**6 — The CV job is polled, not waited on.** `POST /v1/cv` returns `202` and a
request id at once. The client polls `/status` every four seconds, printing
each new step; with `-v` it also prints `detail` — the token spend of the step
that just finished, and the reviewer's or the page check's own words. When the
status reads `END` it collects the document, the LaTeX and the PDF in one
call.

If the client dies mid-job, the job does not: the server keeps writing. Pick
it back up with `jobstitch submit posting.txt --resume <request_id>`, which
skips submitting and starts from `/status`.

**7 — A folder that already holds its document is re-rendered, not rewritten.**
If `cv_You.json` is already in the job folder, `_cv` posts it to
`/v1/cv/render` instead of starting a job: one LaTeX compile, no model call.
That is the whole recovery story — edit the JSON, drop the folder back, pay
nothing to a model.

**8 — Failure is filed, not printed and lost.** Every failure carries a
`request_id`; the client always fetches `GET /logs/{request_id}` for it and
folds the server's account into the job's `log.log`, then moves the job to
`error/`. With `--debug` it fetches that log after every call, successful or
not.

**9 — The spreadsheet is bookkeeping, never a reason to fail.** A locked or
broken `applications.xlsx` becomes a warning line in `log.log`; the CV was
paid for and stays delivered.

## Where each step lives in the code

| Step | Module |
|---|---|
| Polling the clipboard, the free check | `client/src/jobstitch_client/sources/clipboard.py` |
| The order of the steps | `client/src/jobstitch_client/runner.py` |
| Every HTTP call, and nothing else | `client/src/jobstitch_client/api.py` |
| Submitting and following a CV job | `client/src/jobstitch_client/cvjob.py` |
| Paths, moves, recovery | `client/src/jobstitch_client/workspace.py` |
| The analysis table and the question | `client/src/jobstitch_client/ui.py` |
| `log.log` | `client/src/jobstitch_client/joblog.py` |
| `applications.xlsx` | `client/src/jobstitch_client/tracking.py` |
