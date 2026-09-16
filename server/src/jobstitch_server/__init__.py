"""jobstitch server: the HTTP API that writes and renders CVs.

Layers, outermost first:

- ``api`` — FastAPI routers, multipart handling, auth, error mapping. Knows
  about HTTP and nothing about LaTeX or prompts.
- ``pipeline`` — :class:`CVGenerator`, :class:`CVRenderer`,
  :class:`JDValidator`, :class:`LetterGenerator`. Pure library code: every
  input arrives in memory, every output is returned, nothing is read from a
  configured path.
- ``model_selector`` — one OpenAI-compatible endpoint per role, from
  ``resources/models.toml``.
- ``bundle`` / ``defaults`` — where the impersonal defaults come from, and the
  per-request split between them and the personal inputs.
- ``observability`` / ``logstore`` — the per-request log, including the token
  spend of every model call, fetchable afterwards via ``GET /logs/{id}``.

The wire format lives in :mod:`jobstitch_contracts`, which both this and the
client depend on; neither imports the other.
"""

__all__ = ["api", "bundle", "defaults", "model_selector", "observability", "pipeline"]
