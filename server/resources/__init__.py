"""Impersonal defaults shipped inside the server package.

The system prompts (CV, highlight, review, letter, JD analysis, JD
detection), the LaTeX template, ``models.toml`` and a blank signature
placeholder. Nothing here identifies anyone: every personal file arrives with
the request. Resolved through :mod:`jobstitch_server.defaults`, which lets
``$JOBSTITCH_RESOURCES`` point at an edited copy instead.
"""
