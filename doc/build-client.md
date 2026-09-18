# Building the client

The client ships as a single executable — no Python on the user's machine, no
virtualenv, no install step.

```bash
uv sync
uv run --with pyinstaller pyinstaller --noconfirm client/packaging/jobstitch.spec
./dist/jobstitch --help
```

That produces `dist/jobstitch` (or `dist/jobstitch.exe` on Windows).

## What the spec does

`client/packaging/jobstitch.spec`:

- **Entry point** is `client/packaging/entrypoint.py`, not
  `jobstitch_client/__main__.py`. PyInstaller runs its entry script as a
  top-level module, where `__main__.py`'s relative import has no package to
  resolve against.
- **Bundled data**: `applications.xlsx`, the empty spreadsheet copied on first
  use. Everything else the client needs — your profile, your template, your
  images — it reads from beside the binary at runtime, which is the whole
  point.
- **Excludes**: `fastapi`, `uvicorn`, `starlette`, `openai`, `jinja2`,
  `pypdf`, `jobstitch_server`, `tkinter`, `pytest`. None of them belong in a
  client; excluding them keeps the binary small if one is ever pulled in
  transitively. `tests/test_architecture.py` enforces the same rule at the
  source level, so a bad import fails the test suite before it reaches the
  build.

**PyInstaller does not cross-compile.** Build each binary on the OS it is for.

Why PyInstaller and not Cython: Cython compiles modules to C extensions but
still needs an interpreter and a launcher around them, so it does not produce
the single file this is for. Nuitka would, and is the fallback if start-up
time or source opacity ever matters more than build simplicity.

## The release pipeline

```mermaid
flowchart TB
    subgraph matrix["client job — one runner per OS"]
        direction TB
        U["uv sync --frozen"] --> CACHE["restore the PyInstaller<br/>analysis cache<br/><i>key: uv.lock + the spec</i>"]
        CACHE --> BUILD["pyinstaller jobstitch.spec"]
        BUILD --> HELP["./dist/jobstitch --help"]
        HELP --> SMOKE["Linux only:<br/>start jobstitch-api from source,<br/>render smoke.tex through it,<br/>assert the file starts with %PDF"]
        SMOKE --> PKG["assemble package/"]
        PKG --> ART["upload-artifact"]
    end
```

The smoke test is the part worth keeping: `--help` proves the binary starts,
but the real question is whether it *talks to a server*. `render` is the one
call that needs no model, so CI starts the server from the same checkout with
an invalid provider endpoint, renders a two-line `.tex` through the real
multipart → envelope → `pdflatex` → base64 path, and checks the bytes on disk.

The analysis cache is keyed on `uv.lock` **and** the spec, because either one
invalidates it; without it PyInstaller re-analyses every dependency from
scratch on each run.

## What a user downloads

CI assembles more than the binary, because a binary alone cannot run:

```text
package/
├── jobstitch[.exe]           the executable
├── README.md                 the client README, links rewritten for this layout
├── jobstitch.toml.example    rename it and point it at your server
└── examples/
    ├── candidate/            a fictional profile, data, preferences, signature
    └── posting.txt           something to try it on
```

The README's links are rewritten during assembly: in the repository they point
at `../examples/candidate` and `../server/README.md`, and in the download
`examples/` sits beside the README and there is no `server/`.

Releases are published from those artifacts as `jobstitch-linux-x86_64` and
`jobstitch-windows-x86_64`.

## Running from source instead

```bash
uv sync
uv run jobstitch --help
uv run jobstitch --server http://localhost:8080 clipboard --out ~/applications
uv run pytest client/tests        # no server, no terminal, no network
```

The client's tests fake at the boundary: `JobstitchApi` is a protocol, so the
modes run against a stub with no HTTP at all, and `tests/test_integration.py`
drives the real client against the real app in-process through an
`httpx.Client` — both halves, no socket.

## Adding a new way to feed it postings

A source of job descriptions is a `JDSource`: one method, yielding
`JDCandidate`s. `clipboard`, `watch` and `submit` are three sources over one
pipeline, so a fourth — an IMAP folder, a browser extension, a queue — is a
new file under `client/src/jobstitch_client/sources/`, a thin mode that wires
it up, and no change to `JobRunner` at all.
