"""JD sources — programs that publish job folders for the jobstitch watcher.

A JD source is anything that writes the folder :mod:`jobstitch.job_watcher`
picks up::

    $JOBSTITCH_HOME/incoming/<any folder name>/
        JD.txt          the raw job description text — the whole contract
        ...             anything else the source wants to keep with the job

A non-empty ``JD.txt`` is all the watcher requires and the only file it reads.
The folder name is never parsed (``<Company>_<JobTitle>`` is a convention for
the reader), and every other file in the folder is carried through to
``resume/<today>/`` with the finished CV — an ``analysis.json``, as
:mod:`~jd_sources.clipboard_import` writes, being the usual extra.

That folder is the whole contract, so a source is a *consumer* of jobstitch,
not a part of it: everything here imports :mod:`jobstitch`, and nothing in
:mod:`jobstitch` imports anything here. Sources live outside the package for
exactly that reason — add a scraper, a job-board client or a shell script
next to :mod:`~jd_sources.clipboard_import` without touching the pipeline.

Ships one source: :mod:`~jd_sources.clipboard_import` (console script
``clipboard-import``).
"""
