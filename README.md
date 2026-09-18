# Job Stitch

Turn a job description and your unstructured experiences into a tailored, two-page LaTeX CV and a cover letter, using any OpenAI-compatible model.

[**See what it produces →** `examples/sample_cv.pdf`](examples/sample_cv.pdf)

- **Analyses** the Job Description you select against your profile — match score, salary, seniority, skills, gaps (optional).
- **Writes** a CV tailored to the posting, reviewed against your profile for
  invented claims, condensed until it fits two pages, with matching keywords
  highlighted.
- **Renders** real LaTeX to a PDF, and optionally a cover letter.
- Watches your **clipboard** or a **folder**, or runs **once** on a single file.

You need to provide:
- A list of your work experiences, skills, the more you put the best it is (as long as you can defend it in an interview). You don't have to worry about writing them in a perfect style: it will fix it for you.
- Some customization data for the cv (your phone...)
   - Any image your template uses — a signature scan, a photo, a logo. Drop it
     in the folder and the template includes it by file name.
- The job description
- You api key and model configuration

## Will it work for me?

It started as my own personal lab to learn AI assisted coding. It's not tested across
different cv formats and sizes. It's sized for a well established professional with 
lots of experiences. I tried to make it "tunable", so you can fit your experiences.

## Download and run

jobstitch is two pieces: a server that holds the model keys and the LaTeX
toolchain, and a client executable you run on your machine. Neither needs the
other installed locally — the client talks to the server over HTTP.

**1. Run the server** — a container image, published on Docker Hub:

```bash
docker run -d -p 8080:8080 \
  -e MODEL_API_KEY=... \
  gcontini/jobstitch-server:latest
```

Any OpenAI-compatible provider works — see
[`server/README.md`](server/README.md) for the other providers and the full
environment table. `curl localhost:8080/healthz` should report `pdflatex: true`.

**2. Download the client** — a single executable, no Python required, from the
[releases page](https://github.com/gcontini/jobstitch/releases): pick
`jobstitch-linux-x86_64` or `jobstitch-windows-x86_64`, unpack it, and put your
own `candidate_profile.json` and `candidate_data.json` beside it (start from
the fictional set in the download's `examples/candidate/`).

```bash
./jobstitch --server http://localhost:8080 clipboard --out ~/applications
```

Copy a job posting to your clipboard; jobstitch checks it, shows you the
analysis, and on confirmation writes the CV into
`~/applications/cv/<day>/<Company>_<Title>/`. `watch` and `submit` work the
same way against files instead of the clipboard —
[`client/README.md`](client/README.md) covers all three modes, configuration,
and the output layout.

## Architecture

```
  you copy a posting
          │
          ▼
   ┌────────────┐   is this a job description? ──▶┌──────────────────────┐
   │   client   │   analyse it ──────────────────▶│        server        │
   │            │   write the CV ────────────────▶│  models + pdflatex   │
   │ your data  │   render the PDF ──────────────▶│   your data: never   │
   │ your files │◀── document, PDF, request ids ──│   kept after the     │
   └────────────┘                                 │   request ends       │
          │                                       └──────────────────────┘
          ▼
   cv/26-01-15/Acme_Corp_Head_of_IT/
       cv_you.pdf  cv_you.tex  cv_you.json  analysis.json  jd.txt  log.log
```

| | |
|---|---|
| **[`client/`](client/README.md)** | The executable. Watches your clipboard or a folder, keeps your CV data local, files every result in a dated folder. No Python, no API key, no LaTeX. |
| **[`server/`](server/README.md)** | The container. Holds the model API keys and the LaTeX toolchain; writes the CV, compiles the PDF, analyses postings. Keeps a directory per request — its log, and a CV job's state and result — and nothing else. |
| [`contracts/`](contracts/src/jobstitch_contracts/) | The shapes both sides agree on — the one thing each side imports, so neither imports the other. |

Writing a CV takes minutes, so `POST /v1/cv` answers at once with a job id;
the client polls `/v1/cv/{id}/status` every four seconds, prints each new step
as it happens, and collects the JSON, the LaTeX and the PDF in one call at the
end. `GET /logs/{request_id}` fetches what any request did, including the
token spend of every model call; the client folds that into `log.log` next to
each CV.

## Development

```bash
uv sync
uv run pytest                   # all four suites; no API key, no network
uv run jobstitch-api            # the server, from source
uv run jobstitch --help         # the client, from source
```

Tests that need `pdflatex` skip themselves without it. The engineering rules
the code follows are in [AGENTS.md](AGENTS.md).

## Known limitations

- The certifications table in `resume.tex.jinja` is hardcoded LaTeX rather
  than templated from your data — edit that block to put your own in.
- A CV job cannot be cancelled: an abandoned one holds a slot until its own
  budget runs out. See [PLANNED-FEATURES.md](PLANNED-FEATURES.md).
- A job id only means something to the instance holding its directory, so run
  one instance per `JOBSTITCH_WORK_DIR`.
- One template, one page limit, one language. Bring your own `.tex.jinja` if
  you want a different shape.

## License

MIT — see [LICENSE](LICENSE).
