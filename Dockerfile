# Builds the server as a docker. it is here because some cloud build don't 
# understand the build in a subfolder

# The jobstitch API server: the LaTeX toolchain, the Python environment and
# uvicorn. Stateless — it mounts nothing and stores nothing.
#
#     docker build -t jobstitch-server .
#     docker run --rm -p 8080:8080 --env-file .env jobstitch-server
#
# Built from the repository root's contracts/ and server/ only; the client is
# a separate artifact and shares nothing but the contracts package.
#
# Classic-builder safe: no BuildKit features and no heredocs.


# ---------------------------------------------------------------------------
# 1. Build the virtualenv with uv, from the lock file.
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so this layer is rebuilt only when the lock file changes.
# The workspace members must be present for uv to resolve against the lock.
COPY pyproject.toml uv.lock README.md ./
COPY contracts/pyproject.toml ./contracts/
COPY server/pyproject.toml ./server/
COPY client/pyproject.toml ./client/
RUN uv sync --frozen --no-dev --package jobstitch-server --no-install-workspace

# Then the code, which changes on every commit. --no-editable copies the
# packages into the venv instead of linking them back to /app/src, so the
# runtime stage needs nothing but the venv itself.
COPY contracts/src ./contracts/src
COPY server/src ./server/src
COPY server/resources ./server/resources
RUN uv sync --frozen --no-dev --package jobstitch-server --no-editable


# ---------------------------------------------------------------------------
# 2. Runtime: the same base the venv was built against, plus pdflatex.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# resume.tex.jinja needs pdflatex with hyperref, graphicx, array, hhline,
# amsmath and babel-english (all in texlive-latex-base) and Type 1 CM fonts
# (cm-super — without them T1 encoding falls back to bitmap fonts and the PDF
# looks scanned; cm-super-minimal is not enough, it leaves the design sizes
# the CV uses for headings and small print as Type 3 bitmaps).
#
# No clipboard helpers here: reading the clipboard is the client's job, and
# the client runs on your machine.
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        cm-super \
    && rm -rf /var/lib/apt/lists/*

# fontawesome5 is vendored (docker/vendor/, see the README there) rather than
# pulled from the 1.7 GB texlive-fonts-extra or fetched from CTAN at build
# time — no network access needed for this step. The shell lives in a script
# file rather than a Dockerfile heredoc because heredocs need BuildKit, and
# this has to build on the classic builder too.
COPY server/docker/vendor/fontawesome5.tar.xz server/docker/install-fontawesome5.sh /tmp/
RUN /tmp/install-fontawesome5.sh /tmp/fontawesome5.tar.xz \
    && rm /tmp/fontawesome5.tar.xz /tmp/install-fontawesome5.sh

# HOME, the cache and TEXMFVAR are written to at runtime, so they are kept off
# a real home directory: the image is meant to run as any uid
# (--user "$(id -u):$(id -g)") so files in the mounted folder belong to you.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    JOBSTITCH_WORK_DIR=/tmp/jobstitch \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    TEXMFVAR=/tmp/texmf-var

COPY --from=build /app/.venv /app/.venv
COPY server/docker/entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh && mkdir -p /tmp/jobstitch

EXPOSE 8080

# The platform probe has no token, so /healthz is the unauthenticated route.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD \
    python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/healthz')"

WORKDIR /tmp

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["jobstitch-api"]
