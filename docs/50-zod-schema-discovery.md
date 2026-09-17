# Step 50 — Zod Schema Discovery

**Status:** IMPLEMENTED (2026-09-16).
**Goal:** discovery only ever found endpoints (route + method); it never read the project's own schema/validation-definition files at all. The user asked for real schema files (not just route text) to feed request-body construction - Zod prioritized first, since most test targets are MERN/full-stack (JS/TS) projects, not Python/FastAPI.

## Why this beats what already existed (docs/45/47/49)

Every prior fallback (`body_field_hints`, the FastAPI body-read-evidence, the probe) can tell you a field's *name*, never its real *type* - `age` synthesizes to a generic string just like any other unrecognized field name would, which a real `z.number()` check on the target correctly rejects. A real Zod schema declares both, for real - this closes that gap by reading the schema itself rather than guessing from a name or an error message.

## Design

**`qa_agent/api_qa/zod_schema.py` (new)** - a small, bounded parser, deliberately not a JS/TS parser (the same "narrow, recognizable convention" scope every other discovery strategy in this project already accepts):

- `_find_matching_brace`/`_split_top_level`: real depth-tracked scanners (string-literal-aware) that find a `z.object({...})`'s own real closing brace and split its body into real top-level `key: value` entries - needed because a real chain (`z.array(z.string())`, `z.enum(["a","b"])`) has its own real nested commas/parens/brackets a naive split would break on.
- `parse_zod_object_body`: each entry's real base type from its own first `z.<type>(...)` call (`string`/`number`/`boolean`/`array`/`object`/`enum`/`date`), `.email()` recognized as a more specific format, and - critically - a field with `.optional()`/`.nullable()`/`.default(...)` anywhere in its chain is **excluded entirely**, the same "only ever report a real required field" discipline `build_request_body` already has for OpenAPI schemas.
- `build_zod_schema_registry(root)`: one whole-project pass (gated behind real, already-detected Next.js/Express framework evidence - never attempted for a pure Python project) finding every real `const X = z.object({...})` and its real fields, keyed by its own real variable name - a schema is commonly defined in a different file than the route that uses it.
- `find_referenced_schema_fields(handler_text, registry)`: a route's own handler text is checked for a real `X.parse(...)`/`X.safeParse(...)` call or a `validate(X)`-shaped middleware reference; if `X` is a real name the registry actually has, its real fields are returned - never a guess at an unrelated schema.
- `zod_field_to_prop`: bridges this module's own small type-token vocabulary to the `{"type": ..., "format": ...}` shape `synthesis.synthesize_value` already understands - reuses that one canonical function rather than a second, competing one.

**`ApiEndpoint.zod_fields: Tuple[Tuple[str, str], ...]`** (new, additive) - populated by all three JS/TS discovery strategies (Next.js App Router, Pages Router, Express), each scoped to its own already-established per-route text window (so two different routes in one file never blend schema references, the same guarantee `body_field_hints` already has).

**`resolution.py`'s `_fallback_from_source_hints`** checks `endpoint.zod_fields` **first**, before `body_field_hints` - real type evidence outranks a name-only guess.

## Verified

`tests/regression/test_api_qa_zod_schema.py` (new, 35/35): the parser against basic types, email-format precedence, optional/nullable/default exclusion, enum values (proving nested commas never split a field early), quoted keys (including hyphenated ones only valid when quoted), and unrecognized shapes; the registry finding a schema defined in a different file than its route, `.parse()`/`.safeParse()`/`validate()` reference matching, per-route isolation; `build_request_body` producing real type-correct values (a real integer for a number field, a real boolean for a boolean field) and `zod_fields` outranking `body_field_hints` when both exist; real discovery attaching real cross-file schema fields; one real, guarded, end-to-end proof against a genuine running Node server that only accepts a request when `age` is a real JS number (not a numeric string) - the real, type-aware synthetic body genuinely passes that real check.

All previously-passing suites re-run clean (a full-suite run was in progress at the time of writing).

## Explicitly not done

- Joi (lower priority, per the user's own MERN/full-stack framing - same shape as Zod, not built today).
- Nested `z.object({...})` sub-schemas are recorded as their parent field's type (`"object"`), never descended into.
- `.refine()`/`.transform()`/`.superRefine()` runtime validation logic is invisible to this module - only the schema's own declared shape is ever read.
- ORM/DB models (Prisma/SQLAlchemy/Mongoose) and static OpenAPI/GraphQL spec files - named as real, separate next steps, not attempted here.
