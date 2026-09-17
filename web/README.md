# QA Agent - Web

A thin web layer over the existing `qa_agent` engine: pick a project folder
in the browser, it gets uploaded, and the *same* discovery/static-analysis/
API-QA functions the CLI already calls run against it, with results shown
in a color-coded page.

## Run it

```
pip install -r web/requirements.txt
python -m uvicorn web.server:app --reload
```

Open http://127.0.0.1:8000, click "Select a project folder", pick a real
project directory (Chrome/Edge - needs `webkitdirectory` support), then
"Run QA".

## What it does

- Uploads every file in the chosen folder (skipping `node_modules`, `.git`,
  build output, etc. - both client-side, to keep the upload small, and
  server-side as a backstop) to a per-request temp directory on the server.
- Runs `discover_project` + `runner.run` (static checks - read-only,
  always safe) against it.
- If "Also run live API tests" is checked (off by default), also runs
  `run_api_qa` - **this executes the uploaded project's own dev/start
  script on this machine.** Only enable it for projects you trust; this is
  the same trade-off the CLI's own `--api-test` flag already has, just now
  reachable from a browser upload instead of a local path.
- The temp directory is deleted after the request finishes either way.

## What's NOT done yet (time-boxed for a same-day deadline)

- No automated test suite for `web/server.py` itself (manually verified
  end-to-end: static findings with real relative paths, and a real
  `run_api_test=true` pass against a real server). `qa_agent/`'s own test
  suite is untouched and unaffected.
- No auth, no per-user isolation beyond the per-request temp directory, no
  rate limiting - fine for a local demo/internal tool, not hardened for a
  public multi-tenant deployment. Running arbitrary uploaded start scripts
  server-side is real code execution; treat this as a trusted-user tool.
- No upload progress bar (uploads happen in one `fetch`, no chunking).
