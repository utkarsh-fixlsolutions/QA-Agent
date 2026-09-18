# Step 56 — Mongoose Model/Schema Discovery

**Status:** IMPLEMENTED (2026-09-18).
**Goal:** close the real gap the Phase 4 real-project validation (idurar-erp-crm) exposed — required request-body fields declared in a project's own Mongoose models were never read, so a mutating endpoint whose controller doesn't directly destructure `req.body` (a very common "thin controller, fat model" or shared-factory pattern) fell through to a meaningless single-placeholder body (`{"qa-agent-test": true}`) or, worse, was skipped as `CONTRACT UNKNOWN` even when the real answer was sitting in the project's own source. General for any Express+Mongoose project — no file name, directory, or model name is ever special-cased.

## Audit finding

idurar-erp-crm's real `POST /api/login` proved this concretely: its controller (`adminAuth/index.js`) is two lines, delegating to a shared `createAuthMiddleware('Admin')` factory with no `req.body` reference anywhere in the file. The real required fields (`email`, `name` in `Admin.js`; `password` in the related `AdminPassword.js`) exist, declared with `required: true`, two files away. No existing evidence tier (OpenAPI, Zod, test examples, source-hint field names) could ever see this — Zod and source-hints both only look at text in or near the route's own handler; Mongoose models were never read by any part of the pipeline at all.

## Design

**`qa_agent/api_qa/model_schema.py`** (new): mirrors `zod_schema.py`'s exact architecture and scope discipline — a bounded, regex-based parser (reuses `zod_schema`'s own `_find_matching_brace`/`_split_top_level` helpers rather than duplicating them), never a full JS parse.

- `build_mongoose_schema_registry(root)` — one whole-project pass finding every real `new (mongoose.)Schema({...})` + `mongoose.model('Name', schemaVar)` pair **defined in the same file** (idurar's own `Admin.js` does both; a schema exported from one file and bound to a model in a different one is a real, named, accepted gap). Returns `(by_model_name, file_to_model_name)`.
- A field counts as required only for a real, explicit `required: true` or Mongoose's own `required: [true, 'message']` form — a shorthand type (`title: String`) or a validator-function form is never guessed either way.
- `find_referenced_model_fields(file_abs, file_text, by_model_name, file_to_model_name)` — resolves which model a **handler-defining file** (not necessarily the route file) actually uses, checked in order: (1) a locally imported model whose identifier is used again anywhere else in the file (import-statement text itself is stripped before counting, so the import line's own repetition of the name is never mistaken for real usage), (2) a string literal naming a real, registered model exactly — the factory-by-name convention idurar's own login controller uses, with no import at all.

**`qa_agent/api_qa/discovery.py`**: `_discover_express_endpoints` gained a new `mongoose_registry` parameter and a new mechanism, `_resolve_cross_file_handler_text` — a route's own handler reference (`.post(adminAuth.login)`, `.post(catchErrors(adminAuth.login))`) is resolved, via the route file's own real import map, to the file that actually defines it, and *that* file's text is what body-field-hints/Zod-fields/model-fields all get checked against whenever the same-file check found nothing — never overwriting real same-file evidence that already exists. This closes the same "handler lives in another file" gap for the two existing evidence tiers too, not only the new one.

**`qa_agent/api_qa/models.py`**: additive `ApiEndpoint.model_fields`, feeding the same `EVIDENCE_SCHEMA` tier `zod_fields` already established in `resolution.py`'s fallback chain (checked immediately after Zod, same tier — never a guess at which one "wins" when a project genuinely has both).

**`qa_agent/api_qa/synthesis.py`**: a new `"objectid"` format, synthesizing MongoDB's own canonical documentation example id (`507f1f77bcf86cd799439011`) — a real, valid 24-hex-character shape a Mongoose `required: true` `ObjectId` field's own cast validation actually accepts, instead of a generic string.

**Two general bugs found and fixed via real-project contact, both pre-existing (from Phase 4's own `route_composition.py`), not introduced by this feature but exposed by it:**
1. `_resolve_js_module_path` returned an *unresolved* `Path` (still carrying a literal `..` segment) for an upward-traversal import (`require('../models/X')` — an extremely common `controllers/` + `models/` sibling-directory layout). It matched on disk but silently failed every dict lookup against the `.resolve()`d keys used everywhere else. Fixed by resolving the returned candidate. This also retroactively fixes Phase 4's own router-mount composition for the same import shape.
2. idurar's real imports (`require('@/controllers/...')`) use the `module-alias` npm package's own standard `_moduleAliases` convention in `package.json` (`{"@": "src"}`) — a common, general Node.js pattern, not specific to this project. New `_find_module_alias_map`/`_local_import_map_any`/`_resolve_js_module_path_any` (additive, alongside the existing relative-only functions, which composition continues to use unchanged) read that real, declared mapping rather than guessing one.

## Explicitly not done

- No cross-model `ref` tracing — idurar's login needs `password` from a *second*, related model (`AdminPassword`, linked via `user: ObjectId ref: 'Admin'`); this v1 correctly finds `email`/`name` from the one model actually named, and does not attempt to follow a `ref` relationship to a second schema. A real, disclosed limitation, not a silent gap.
- No Joi / express-validator readers (a separate, later capability, per the standing audit's own scoping).
- No schema-variable-in-one-file, `mongoose.model()`-call-in-another-file resolution.
- Module-alias resolution added only where this feature needed it (handler cross-file resolution); the existing router-mount composition pass is untouched and still relative-import-only.

## Verified

New suite `tests/regression/test_api_qa_model_schema.py`: 31/31 (pure parser unit tests, registry building, both real usage-detection signals, `build_request_body` integration, and two full end-to-end discovery fixtures reproducing idurar's exact three-layer shape). All pre-existing api_qa suites remain green after the two shared-infrastructure fixes above.

Real-project validation against idurar-erp-crm (not a fixture): `POST /api/login` now resolves real `email`/`name` fields (previously `CONTRACT UNKNOWN`, then a placeholder). `POST /api/setting/create` / `PATCH /api/setting/update/:id` independently resolved real `settingCategory`/`settingKey` fields from a completely unrelated model file (`Setting.js`) — confirmed directly against that file's own source, proving this generalizes across the project rather than only working for the one case that motivated it.
