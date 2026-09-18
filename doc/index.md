# jobstitch

Turn a job description and your unstructured experiences into a tailored,
two-page LaTeX CV and a cover letter, using any OpenAI-compatible model.

jobstitch is two halves that never share a machine by necessity:

- a **server** that holds the model API keys, the prompts and the LaTeX
  toolchain, shipped as a container image;
- a **client** that holds your CV data and your files, shipped as a single
  executable with no Python, no API key and no LaTeX of its own.

They speak HTTP, `multipart/form-data` in and JSON out, over a wire format
both sides import and neither owns.

## Where to start

| If you want to… | Read |
|---|---|
| Understand how the pieces fit | [Architecture](architecture.md) |
| Know how a CV is actually written | [The CV pipeline](cv-pipeline.md) |
| Follow one real run call by call | [A run, end to end](clipboard-flow.md) |
| Use the client | [The client](client.md) |
| Call the server yourself | [The HTTP API](protocol.md) |
| Build or deploy the server | [Building the server](build-server.md) |
| Build the client executable | [Building the client](build-client.md) |
| Decide where each half runs | [Running the two halves apart](deployment.md) |

## The shape of a run

```mermaid
flowchart LR
    JD["a job posting<br/>(clipboard, folder or file)"] --> C["client<br/><i>your data, your files</i>"]
    C -->|HTTP| S["server<br/><i>models + pdflatex</i>"]
    S -->|"document · LaTeX · PDF · request ids"| C
    C --> OUT["cv/26-01-15/Acme_Corp_Head_of_IT/<br/>cv_you.pdf · cv_you.tex · cv_you.json<br/>analysis.json · jd.txt · log.log"]
```

Your profile, your contact details and your images travel with each request
and are thrown away when it ends. The server keeps no database and no
accounts — only a directory per request, holding that request's log and, for
a CV job, its state and its result.

## The three packages

| | |
|---|---|
| `client/` | The CLI. Watches your clipboard or a folder, keeps your CV data local, files every result in a dated folder. |
| `server/` | The HTTP API. Writes the CV, reviews it, compiles the PDF, analyses postings, writes letters. |
| `contracts/` | The shapes both sides agree on — the one thing each side imports, so neither imports the other. |

The short quickstarts live in the repository's `README.md`, `client/README.md`
and `server/README.md`. These pages are the long form: what the system does,
why it is built this way, and every parameter of both halves.

## Building these pages

The site is `doc/` rendered by [MkDocs](https://www.mkdocs.org/) with the
[Material](https://squidfunk.github.io/mkdocs-material/) theme, configured in
`mkdocs.yml` at the repository root. Both are pinned in the `docs` dependency
group of `pyproject.toml`, so they come with the workspace and not from your
system Python:

```bash
uv run --group docs mkdocs serve    # live preview on http://localhost:8000
uv run --group docs mkdocs build    # static HTML into site/ (gitignored)
```

| | |
|---|---|
| Supported | MkDocs **1.6+** with mkdocs-material **9.5+** |
| Not supported | MkDocs 2.0, which removes the plugin system Material is built on. Both are capped in `pyproject.toml` |
| Diagrams | Mermaid, as ```` ```mermaid ```` fences, rendered by Material through `pymdownx.superfences`. A different theme will print them as code |
| Strictness | `strict: true` — a link to a page or an anchor that does not exist fails the build |

To add a page: put the Markdown in `doc/` and add it to the `nav:` in
`mkdocs.yml`. A page that is not in the nav still builds, but nothing links to
it.
