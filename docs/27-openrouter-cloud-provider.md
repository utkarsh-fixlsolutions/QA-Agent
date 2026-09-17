# Step 27 — OpenRouter Cloud Provider

**Status:** IMPLEMENTED (2026-09-11).
**Phase:** none — an additive provider, not a phase. Slots into the existing `AIProvider` abstraction exactly like `OllamaProvider`/`MockProvider`; no other module's behavior changes because it exists.
**Scope:** `qa_agent/ai/openrouter.py` (new), plus the minimum wiring needed for a third provider name to be selectable: `qa_agent/ai/__init__.py` (+export), `qa_agent/config.py` (+`"cloud"` to the known-provider set), `qa_agent/__main__.py` (+`"cloud"` to the CLI choice list, one new provider-construction branch).

## Why this exists

`OllamaProvider`/`MockProvider` cover local and test use. This adds a third option — an optional, opt-in cloud backend — for anyone who wants to try a stronger hosted model against QA-Agent's existing prompts/schemas/validators without needing local hardware for it. It changes nothing about what "local" already does; it's a parallel choice, not a replacement.

## Provider architecture

```
AIProvider  (qa_agent/ai/provider.py - unmodified)
   |
   +-- OllamaProvider   (qa_agent/ai/ollama.py - unmodified)
   |
   +-- MockProvider     (qa_agent/ai/mock.py - unmodified)
   |
   +-- OpenRouterProvider  (qa_agent/ai/openrouter.py - new)
```

`OpenRouterProvider` implements the exact same two-method contract every other provider does — `generate(prompt: str) -> LLMResponse` and `test_connection() -> ConnectionResult` — and is used identically everywhere a provider is already used: `explain_finding`, `summarize_run`, `suggest_fix`, `propose_repair`, `diagnose_runtime_failure(s)`, `repair_runtime_failure(s)`. None of those functions know or care which provider they were given.

## Configuration

| What | How |
|---|---|
| API key | `OPENROUTER_API_KEY` environment variable — read once, at provider construction, via `os.environ.get(...)`. Never read from `.env` (no dotenv loading anywhere in this project), never read from `.qa-agent.json`, never hardcoded. |
| Default model | `poolside/laguna-s-2.1:free` |
| Default endpoint | `https://openrouter.ai/api/v1/chat/completions` |
| Default timeout | 30 seconds (the same value `OllamaProvider` already uses — this project's own established convention, not a new number invented for this provider) |

The model remains fully configurable through the existing surface — `--ai-model` on the CLI, `ai.model` in `.qa-agent.json` — exactly as it already was for Ollama. Nothing about G3/G4 hardcodes a model name; `poolside/laguna-s-2.1:free` only ever appears as `OpenRouterProvider`'s own constructor default, the same role `"llama3"` already plays for `OllamaProvider`.

## CLI

```
python -m qa_agent discover . --diagnose --ai-provider cloud
python -m qa_agent discover . --diagnose --ai-provider cloud --ai-model poolside/laguna-s-2.1:free
python -m qa_agent discover . --diagnose --repair-runtime --ai-provider cloud
```

`--ai-provider cloud` is now a third accepted value alongside `ollama`/`mock`, in both places `--ai-provider` already existed (the one-shot/watch CLI's `--ai` flags, and `discover`'s own `--diagnose`/`--repair-runtime` flags). Omitting `--ai-model` with `--ai-provider cloud` uses the default model above. `.qa-agent.json`'s `ai.provider: "cloud"` works the same way.

## HTTP implementation

Stdlib `urllib.request` only, mirroring `ollama.py`'s own implementation shape line for line (same `_TIMEOUT_EXCEPTIONS` pair, same "catch everything, return a structured failure, never raise" contract) — no new dependency was added. One real difference from Ollama's endpoint, both because OpenRouter's own API is shaped this way: the request body follows OpenAI's chat-completions schema (`{"model": ..., "messages": [...], "stream": false}`), and the prompt is sent as a single `user`-role message — `AIProvider.generate(prompt: str)` only ever receives one already-joined string (every existing caller already merges a `Prompt`'s `system`/`user` text before calling a provider), so a single user message is the one faithful way to forward it, not a shortcut.

## Response handling

Every one of the following is caught and returned as a structured `LLMResponse(error=...)`, never raised into G3/G4: missing/empty API key (no network call made at all in either case), DNS/network failure, connection failure, timeout (both a direct `socket.timeout` and a `TimeoutError` wrapped inside `URLError`, the same real gotcha `ollama.py`'s own tests already caught), HTTP 400/401/403/429/5xx (each labeled clearly, with OpenRouter's own JSON error message included when present), invalid JSON, missing `choices`, an empty `choices` list, missing `message`, missing or empty `content`, and any genuinely unexpected exception.

## Optional headers

`HTTP-Referer`/`X-Title` are supported via `http_referer=`/`x_title=` constructor parameters - omitted from the request entirely unless explicitly given, never hardcoded to any specific project/personal value.

## Security

The API key is read only from `OPENROUTER_API_KEY`, only once, at construction. Every error message this provider produces is built only from the HTTP status code and OpenRouter's own server-returned response body - never from the request we sent or its headers, so the key cannot appear even if OpenRouter's own error response happened to echo something request-related back (it doesn't, but the message-construction code path never touches `self.api_key` or the headers dict regardless). Proven directly, not just asserted: a dedicated test constructs the provider with a distinctive fake secret, triggers five different failure modes (401, network failure, timeout, malformed response, an unexpected exception) plus a successful response, and asserts the secret string never appears in any resulting `LLMResponse`/`ConnectionResult` field.

`.env.example` documents the one variable name to set (`OPENROUTER_API_KEY=`, no real value) - `.env` itself is git-ignored, and no code anywhere in this project reads a `.env` file automatically. A repository-wide search after implementation (`git status`, `git diff`, a text search across every changed file) found no real key value anywhere - none was ever available to this session in the first place, since it lives only in the user's own terminal environment.

## G3/G4 compatibility - unchanged, by construction

Nothing about G3 (`diagnose_runtime_failure(s)`) or G4 (`repair_runtime_failure(s)`) changed - both already treat their `provider` parameter as an opaque `AIProvider`-shaped object, never inspecting which concrete class it is. The deterministic runtime executor (Phase G Part 2) remains the sole authority on `PASS`/`FAIL`/`TIMEOUT`/`ERROR`/`SKIPPED`/`NOT_IMPLEMENTED` regardless of provider. G4's eligibility gate, target localization, `decide_repair()`, `TemporaryWorkspace`, and the mandatory real post-apply re-verification are all provider-agnostic and were not touched.

## Testing

`tests/regression/test_ai_openrouter.py` (mirroring `test_ai_provider.py`'s own `OllamaProvider` suite exactly - `urllib.request.urlopen` replaced with a fake, no live network call anywhere) covers: a successful response, both custom and default model, missing/empty API key (no network call made), every HTTP failure code, network/DNS failure, timeout (both forms), an unexpected exception, every malformed-response shape, `test_connection()`'s own success/offline/no-key/never-invokes-generation cases, the secret-never-leaked guarantee (both on failure and on success), and one real CLI subprocess test proving `discover --ai-provider cloud` completes cleanly with no key configured at all. `tests/integration/test_ai_cli_integration.py` gained three focused tests proving `_build_ai_provider` constructs a real `OpenRouterProvider` for `"cloud"`, with correct defaults, and that `"mock"`/`"ollama"` construction is unaffected by the refactor that added the third branch.

## What changed vs. what didn't

**Changed (additive only):** a new provider module; `"cloud"` added to `_KNOWN_AI_PROVIDERS` (config.py) and `_AI_PROVIDER_CHOICES` (`__main__.py`); `_build_ai_provider`/`_discover_main`'s own two-provider ternary both now delegate to one new shared `_construct_ai_provider` helper (so a third provider needed one new branch, not two near-duplicate ones) - every existing `"mock"`/`"ollama"` construction path produces byte-identical objects to before, proven by the existing, unmodified tests for those two cases still passing.

**Not changed:** `AIProvider`'s own interface; `OllamaProvider`/`MockProvider` themselves; every prompt builder, schema, and validator; G1-G4's own logic; Phase E's repair pipeline; the production default provider/model (still whatever `.qa-agent.json`/CLI flags already resolved to before this step - nothing defaults to `cloud`).

**Status: OpenRouter Cloud Provider CLOSED.**
