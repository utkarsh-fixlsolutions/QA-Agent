# Step 46 — Ready-Signal False-Positive Fix

**Status:** IMPLEMENTED (2026-09-16).
**Goal:** a real project tested through the web UI came back with every single endpoint failing "connection refused," against a guessed port (`3000`) the tool never actually observed. Traced to a real defect in the server-startup detection itself, not the HTTP/testing layer - fixed at the source. See docs/39 (connect-probe.md, the related earlier fix this one completes) for the sibling race this closes the last gap of.

## Root cause

`server.py`'s `start_and_wait_ready` watches the target process's live console output and stops watching the instant **any** line contains one of five generic words (`ready`, `listening`, `running on`, `compiled successfully`, `watching for file changes`), case-insensitive, anywhere in the line. It does not check that the line actually came from the real application - a wrapper tool (`nodemon`, `ts-node-dev`, a compiler's own watch-mode banner, ...) commonly prints a line matching one of these words well before the real child process it spawns has finished binding a port.

Once the loop breaks on that early, unrelated line, it never reads any further output - so the real application's own port-announcing line, printed moments later, is never seen at all. `_resolve_base_url` then has nothing real to work with and falls back to assuming the single most common default port (`3000`), honestly labeled as a guess. The existing real-TCP connect probe (docs/39) correctly detects that nothing is listening on that guessed port and adds a warning - but by design (a deliberate, already-tested choice from docs/39: never fabricate a pass) it still proceeds to make every real call, each one failing with an honest, real "connection refused." The result looks exactly like "every endpoint is broken," when the actual defect is upstream: the tool never even tried the right port.

A second, smaller defect found alongside it: the warning printed when the connect probe fails always said *"server matched a ready signal..."*, verbatim, regardless of whether a ready signal was actually matched (`STATUS_READY`) or the process merely stayed alive without one (`STATUS_ALIVE_NO_READY_SIGNAL`) - a real, if minor, violation of this project's own "never state something as fact that wasn't actually observed" discipline.

## Fix

**`start_and_wait_ready` (`server.py`):** the loop no longer stops the instant a bare keyword match occurs. A keyword match is still remembered (for the eventual `reason` text), but the loop only breaks *early* once a real URL or `port NNNN` has actually been observed in the accumulated output (checked via the same `_observed_base_url` the end of the function already used) - the strongest evidence available, and the only thing worth stopping early for. A keyword-only line now just means "keep reading, right up to the same overall timeout" - so when the real application's own port line does appear shortly after, it is now actually seen and used, instead of being missed entirely. When no URL/port is ever observed within the timeout at all (a real app that genuinely never logs its port), behavior is unchanged - still an honest, correctly-labeled fallback guess.

**`runner.py`'s connect-probe-failure warning:** now built from the real, already-known facts instead of one fixed sentence - whether the server actually matched a ready signal or just stayed alive (`handle.status`), and whether the URL being probed was real evidence or an unconfirmed guess (`_resolve_base_url` now returns `(base_url, was_guessed)` instead of just `base_url`).

## Explicitly not changed

The deliberate docs/39 design - proceed and let a real call honestly fail, rather than abort or fabricate a pass, when the connect probe never succeeds - is untouched. That discipline is correct on its own terms (a real, honest failure is still real evidence); the actual defect fixed here is what caused the probe to be checking the *wrong port* in the first place.

## Tests

`tests/regression/test_api_qa.py`: `test_start_and_wait_ready_does_not_stop_on_a_keyword_only_line` (unit-level, real subprocess: a keyword-only line, a real delay, then the real URL line - proves the real URL is now captured, not left empty by an early exit) and `test_run_api_qa_observes_the_real_port_past_a_wrapper_false_ready_line` (full `run_api_qa` end-to-end, real Node server: a fake wrapper-style line first, then the real app's own port line and listener - proves the real call reaches the real, later-observed port, with no "assuming the common default" warning and no connection-refused wall). Every pre-existing connect-probe test (docs/39) still passes unmodified, including the one that specifically locks in "still honestly fails, never fabricates a pass" when the probe genuinely never succeeds.

**Verified:** `test_api_qa.py` - 157/157 (151 pre-existing unchanged + 6 new). Full suite - 41/43 (the same two pre-existing, unrelated flakes documented in every prior step's log).
