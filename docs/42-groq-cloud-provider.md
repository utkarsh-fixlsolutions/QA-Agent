# Step 42 - Groq cloud provider

## What this adds

`GroqProvider` (`qa_agent/ai/groq.py`) - a second, independent cloud
`AIProvider`, alongside the existing `OpenRouterProvider` (docs/27). Groq's
own API is OpenAI-compatible `/chat/completions`, the exact same wire shape
OpenRouter already speaks - so this module is close to a direct copy of
`openrouter.py`, with the endpoint, default model, and API-key environment
variable swapped, and OpenRouter's own optional attribution headers
(`HTTP-Referer`/`X-Title`, meaningless to Groq) dropped.

Stdlib only (`urllib.request`) - no new dependency. Never imported unless
`--ai-provider groq` (or `ai.provider: "groq"` in `.qa-agent.json`) is
explicitly chosen; every deterministic QA feature is completely unaffected
either way.

## Wiring

Same three places every provider is wired (`_construct_ai_provider` is the
one dispatch point both the one-shot/watch CLI and `discover`/`agent`
subcommands already share):

- `qa_agent/ai/__init__.py` - exports `GroqProvider`.
- `qa_agent/__main__.py` - `_AI_PROVIDER_CHOICES` gained `"groq"`;
  `_construct_ai_provider` gained one branch; the `--ai-provider` help text
  in the `discover` and `agent` subcommand parsers now name it (the
  top-level one-shot/watch parser's own `--ai-provider` help stays generic,
  unchanged - it never named `cloud` by name either).
- `qa_agent/config.py` - `_KNOWN_AI_PROVIDERS` gained `"groq"`, so
  `ai.provider: "groq"` in `.qa-agent.json` is accepted, and any other
  unknown name is still rejected the same way a typo'd analyzer name
  already is.

## The API key

Read only from the `GROQ_API_KEY` environment variable - never hardcoded,
never read from a project config file or a `.env` file (this project
deliberately never auto-loads `.env` anywhere - see `.env.example`'s own
header), never logged, and never present in any error message this
provider produces. Verified directly: a real `HTTP 401` response is
reported with the server's own error message and a pointer to check
`GROQ_API_KEY`, with the configured secret itself confirmed absent from
that message (`test_ai_groq.py`).

## Two real issues found and fixed via direct end-to-end verification against the real API (not just mocked tests)

- **A real HTTP 403 (`error code: 1010`) on every request** - Groq's own
  Cloudflare edge blocks `urllib`'s default `Python-urllib/x.y` User-Agent
  as a bot signature. Easy to mistake for an authentication failure (it
  isn't - the key was valid the whole time); fixed by sending a real,
  explicit `User-Agent: qa-agent/<version>` header on every request
  (`generate` and `test_connection` both, via the shared `_headers()`).
  OpenRouter has no equivalent requirement, which is why `openrouter.py`
  never needed this.
- **The initial default model, `llama-3.3-70b-versatile`, no longer
  exists** - a real `HTTP 404` from the real API
  (`"The model llama-3.3-70b-versatile does not exist or you do not have
  access to it"`). Groq's model catalog changes over time. Replaced with
  `openai/gpt-oss-120b`, confirmed via a real `GET /models` call against
  the real API to be currently active, then confirmed via a real
  generation call to actually work end-to-end.

## Verified

`test_ai_groq.py` (new, mirrors `test_ai_openrouter.py`'s own structure) -
40/40 checks: successful generation (real endpoint, real headers, real
non-streaming body), API key read from the environment, every real HTTP
failure mode (401/429/5xx), timeout, connection-refused, malformed JSON,
missing `choices`/`message`/`content`, and `test_connection`'s own
lightweight `/models` reachability check (never invokes generation).
`test_ai_provider.py` (55/55) and `test_config.py` (50/50) both unaffected.

Real end-to-end sanity check via the CLI (`discover . --diagnose
--ai-provider groq`, no key set): reports the exact same honest
"GROQ_API_KEY is not set" failure `_construct_ai_provider`'s own docstring
promises, never a crash.

## Scope

This is provider plumbing only - it does not change what G3 diagnosis, G4
repair, or any other AI-consuming module does with a provider's output;
every caller in this project already only depends on the `AIProvider`
shape (`generate`/`test_connection`), never on which concrete provider
produced it, so nothing downstream needed to change at all.
