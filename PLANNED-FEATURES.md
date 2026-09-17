# Planned features

Things this design has made obvious and cheap, deliberately left out of the
change that made them obvious.

## `DELETE /v1/cv/{id}` — cancel a job

`POST /v1/cv` now returns before its work is done, so a caller that stops
polling — Ctrl-C, a closed laptop, a crashed client — leaves a worker thread
writing a CV nobody will collect. It holds one of the
`JOBSTITCH_MAX_CONCURRENT_JOBS` slots until `JOBSTITCH_REQUEST_BUDGET_SECONDS`
runs out, 20 minutes by default. Ten abandoned jobs lock the server out for
that long, with no way back but a restart.

The blocking version had the same property, but abandonment was rare; polling
makes it ordinary.

What it needs: a flag on the job directory the worker checks where it already
checks its deadline (`CVGenerator._check_deadline`), so the job stops between
rounds rather than mid-model-call, and the client raising it on `KeyboardInterrupt`.
