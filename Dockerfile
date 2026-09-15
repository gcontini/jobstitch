# syntax=docker/dockerfile:1
#
# jobstitch in one image: the LaTeX toolchain, the Python environment and both
# processes (job-watcher + clipboard-import).
#
#     docker build -t jobstitch .
#     docker run -it --rm --env-file .env -v /path/to/jobstitch-data:/data jobstitch
#
# Everything that is yours lives in that one mounted folder:
#
#     /data/applications_cv/   the workspace: incoming, working, error, resume, xlsx
#     /data/resources/         templates, prompts, models.toml, your CV data
#
# The image carries a pristine copy of resources/ and seeds the mounted one
# from it at every launch, never overwriting a file you have edited — see
# docker/entrypoint.sh.


# ---------------------------------------------------------------------------
# 1. Build the virtualenv with uv, from the lock file.
# ---------------------------------------------------------------------------
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS build

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies first, so this layer is rebuilt only when the lock file changes.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

# Then the code, which changes on every commit. --no-editable copies the
# packages into the venv instead of linking them back to /app/src, so the
# runtime stage needs nothing but the venv itself.
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable


# ---------------------------------------------------------------------------
# 2. Runtime: the same base the venv was built against, plus pdflatex.
# ---------------------------------------------------------------------------
FROM python:3.12-slim-bookworm

# resume3.tex.jinja needs pdflatex with hyperref, graphicx, array, hhline,
# amsmath and babel-english (all in texlive-latex-base) and Type 1 CM fonts
# (cm-super — without them T1 encoding falls back to bitmap fonts and the PDF
# looks scanned; cm-super-minimal is not enough, it leaves the design sizes
# the CV uses for headings and small print as Type 3 bitmaps).
#
# xclip / wl-clipboard are what pyperclip drives for clipboard-import; they
# talk to the host's X11 or Wayland socket (see compose.yaml).
RUN apt-get update && apt-get install -y --no-install-recommends \
        texlive-latex-base \
        cm-super \
        xclip \
        wl-clipboard \
    && rm -rf /var/lib/apt/lists/*

# fontawesome5 is vendored (docker/vendor/, see the README there) rather than
# pulled from the 1.7 GB texlive-fonts-extra or fetched from CTAN at build
# time — no network access needed for this step. The shell lives in a script
# file rather than a Dockerfile heredoc because heredocs need BuildKit, and
# this has to build on the classic builder too.
COPY docker/vendor/fontawesome5.tar.xz docker/install-fontawesome5.sh /tmp/
RUN /tmp/install-fontawesome5.sh /tmp/fontawesome5.tar.xz \
    && rm /tmp/fontawesome5.tar.xz /tmp/install-fontawesome5.sh

# HOME, the cache and TEXMFVAR are written to at runtime, so they are kept off
# a real home directory: the image is meant to run as any uid
# (--user "$(id -u):$(id -g)") so files in the mounted folder belong to you.
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    JOBSTITCH_DATA=/data \
    HOME=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    TEXMFVAR=/tmp/texmf-var

COPY --from=build /app/.venv /app/.venv
# The pristine resources the entrypoint seeds $JOBSTITCH_DATA/resources from.
COPY resources /opt/jobstitch/resources
COPY docker/entrypoint.sh docker/start-all.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/entrypoint.sh /usr/local/bin/start-all.sh \
    && mkdir -p /data

# The mounted folder is the working directory, so a .env dropped next to your
# data is picked up too (variables set at launch still win over it).
WORKDIR /data

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/usr/local/bin/start-all.sh"]
