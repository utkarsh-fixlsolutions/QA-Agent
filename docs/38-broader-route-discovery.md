# Step 38 - broader route discovery (Pages Router, Express)

## What this adds

Two new, additive discovery strategies in `qa_agent/api_qa/discovery.py`,
combined into `discover_api_endpoints` alongside the existing App Router
and FastAPI strategies - none replaces any other.

### Next.js Pages Router (`_discover_nextjs_pages_endpoints`)

Real `.ts`/`.js` files under a `pages/api/`-named directory with a real
`export default` handler.

- **Gated on a real, already-detected "Next.js" framework fact**
  (`project.frameworks`) - unlike the App Router strategy (whose
  `route.ts`/`route.js` filenames are distinctive enough on their own), a
  bare directory named `pages` is common far outside Next.js, so directory
  name alone would be too weak a signal.
- **Path derivation**: `pages/api/companions/[id].ts` ->
  `/api/companions/[id]`; `pages/api/companions/index.ts` ->
  `/api/companions` (Pages Router's own real `index` convention - the file
  itself *is* the route, there is no route-group-folder syntax to strip
  the way App Router has).
- **Method resolution**: Pages Router hands every HTTP method to the same
  one default-exported function, so which methods it actually answers is a
  real fact about the handler's own body, not the filename. A real
  `req.method === 'GET'` (or `case 'GET':`) check contributes that method,
  same as App Router's exported-function-name evidence. When no such check
  exists at all, the handler genuinely answers every method the same
  way - reported as `GET` with an explicit warning that this is an
  assumption, never silently treated as fact (the same "document the
  assumption" convention `runner.py`'s own `_resolve_base_url` already
  established for the default Next.js port).

### Express (`_discover_express_endpoints`)

Real `app.<method>('/path', ...)` / `router.<method>('/path', ...)` calls
in any `.js`/`.ts` file in the project.

- **Gated on a real, already-detected "Express" framework fact** - the
  same evidence-gating discipline the FastAPI strategy already
  established, applied to Express's own dependency.
- Unlike the two Next.js strategies (each scoped to one named directory),
  this scans the whole project - Express has no fixed directory
  convention, a route can live anywhere.
- Only a real call with a real, literal path string as its first argument
  counts (`app.get(someMiddleware, ...)` is never matched at all - the
  regex itself requires a quote character there); `:param`-shaped segments
  are marked `dynamic`, the same way FastAPI's `{param}` already is.

## Scope boundaries (named, not silently gapped)

- **A router mounted under a path prefix** (`app.use('/api', router)`) is
  not resolved - the exact same, deliberately-drawn boundary already named
  for FastAPI's `APIRouter(prefix=...)`. A route discovered this way is
  reported with whatever literal path string appears at its own call site,
  which may be missing the real mount prefix.
- **A differently-named Express app/router variable** (not `app`/`router`)
  is not matched - these are Express's own overwhelmingly standard names;
  a project using something else is a real gap, named here rather than
  attempted unreliably.
- **`APIRouter(prefix=...)` composition (FastAPI)** remains out of scope,
  unchanged from docs/32/33 - deferred again today given time constraints,
  the most complex of the three originally-planned strategies and the
  first one cut when time ran short (explicitly agreed with the user).

## Verified

`test_api_qa.py`: 11 new discovery tests (Pages Router method resolution,
`index` mapping, dynamic segments, the "no method check -> assumed GET,
warned" path, no-default-export warning, Next.js-gating; Express simple
route, router-call + dynamic segment, Express-gating, non-literal-argument
rejection) - 134/134 total, all pre-existing checks unchanged. Related
suites unaffected: `test_api_qa_fastapi.py` 49/49, `test_api_qa_resolution.py`
60/60, `test_api_ai_bridge.py` 71/71.

Re-run against the real `D:\Major projects\lms-ai` project (Next.js App
Router only, no Pages Router or Express) after this change: still exactly
the same 2 endpoints discovered - proof neither new strategy fires when its
own gating evidence is genuinely absent.
