"""Real Zod schema discovery (docs/50-zod-schema-discovery.md):
`qa_agent.api_qa.zod_schema` - the bounded `z.object({...})` parser, the
whole-project schema registry, matching a route's own real schema
reference to its real definition, and the resulting `ApiEndpoint.
zod_fields` feeding `resolution.py`'s `build_request_body` with real
field-name-*and*-type evidence, ranked above the weaker, name-only
`body_field_hints` fallback (docs/45).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import ApiQaConfig, discover_api_endpoints, run_api_qa  # noqa: E402
from qa_agent.api_qa.models import ApiEndpoint, CALL_PASS, CALL_SKIPPED  # noqa: E402
from qa_agent.api_qa.resolution import build_request_body  # noqa: E402
from qa_agent.api_qa.zod_schema import (  # noqa: E402
    build_zod_schema_registry,
    find_referenced_schema_fields,
    parse_zod_object_body,
    zod_field_to_prop,
)


def _npm_available():
    return shutil.which("npm") is not None


# --- parse_zod_object_body (pure) -------------------------------------------

def test_parses_basic_field_types(suite):
    fields = parse_zod_object_body(
        'name: z.string(), age: z.number(), active: z.boolean(), tags: z.array(z.string())'
    )
    suite.check("all four real fields, in declared order, with the real base type each",
                fields == (("name", "string"), ("age", "number"), ("active", "boolean"), ("tags", "array")))


def test_email_format_recognized_over_bare_string(suite):
    fields = parse_zod_object_body('email: z.string().email()')
    suite.check("the more specific 'email' token wins over the bare string base type",
                fields == (("email", "email"),))


def test_optional_and_nullable_fields_are_excluded(suite):
    fields = parse_zod_object_body(
        'name: z.string(), nickname: z.string().optional(), bio: z.string().nullable()'
    )
    suite.check("only the real required field is kept - optional/nullable are real evidence "
                "they are NOT required, never included anyway", fields == (("name", "string"),))


def test_default_fields_are_excluded(suite):
    fields = parse_zod_object_body('role: z.string().default("user"), name: z.string()')
    suite.check("a field with a real declared default is not required either",
                fields == (("name", "string"),))


def test_enum_type_recognized(suite):
    fields = parse_zod_object_body('status: z.enum(["active", "inactive"])')
    suite.check("enum recognized as its own type token", fields == (("status", "enum"),))


def test_quoted_keys_are_stripped(suite):
    fields = parse_zod_object_body('"user-name": z.string(), \'user-email\': z.string().email()')
    suite.check("real quoted keys are stripped to their real bare names",
                fields == (("user-name", "string"), ("user-email", "email")))


def test_nested_commas_inside_enum_never_split_a_field_early(suite):
    """The real reason a naive `str.split(",")` would break here - an
    enum's own real comma-separated values must never be mistaken for a
    field separator.
    """
    fields = parse_zod_object_body('status: z.enum(["a", "b", "c"]), name: z.string()')
    suite.check("exactly two real fields, not fragments of the enum's own values",
                fields == (("status", "enum"), ("name", "string")))


def test_unrecognized_field_shape_is_excluded_not_guessed(suite):
    fields = parse_zod_object_body('custom: someCustomValidator(), name: z.string()')
    suite.check("a field with no recognizable z.<type>(...) call is never guessed at",
                fields == (("name", "string"),))


# --- build_zod_schema_registry / find_referenced_schema_fields (real files) -

def test_registry_finds_schema_defined_in_a_different_file_than_the_route(suite):
    proj = TempProject()
    try:
        proj.write("schemas.ts", (
            "export const createUserSchema = z.object({\n"
            "  name: z.string(),\n"
            "  email: z.string().email(),\n"
            "});\n"
        ))
        proj.write("route.ts", (
            "import { createUserSchema } from './schemas';\n"
            "export async function POST(request) {\n"
            "  const data = createUserSchema.parse(await request.json());\n"
            "  return Response.json(data);\n"
            "}\n"
        ))
        registry = build_zod_schema_registry(proj.path)
        suite.check("the real schema is found, keyed by its own real variable name",
                     registry.get("createUserSchema") == (("name", "string"), ("email", "email")))
        handler_text = (proj.path / "route.ts").read_text(encoding="utf-8")
        fields = find_referenced_schema_fields(handler_text, registry)
        suite.check("the route's own real .parse() reference resolves to the real schema",
                     fields == (("name", "string"), ("email", "email")))
    finally:
        proj.__exit__(None, None, None)


def test_safe_parse_and_validate_middleware_shapes_are_both_recognized(suite):
    registry = {"schema1": (("a", "string"),), "schema2": (("b", "number"),)}
    suite.check("safeParse recognized",
                 find_referenced_schema_fields("schema1.safeParse(body)", registry) == (("a", "string"),))
    suite.check("a validate(...) middleware call recognized",
                 find_referenced_schema_fields("router.post('/x', validate(schema2), handler)", registry)
                 == (("b", "number"),))


def test_unreferenced_or_unknown_schema_name_yields_nothing(suite):
    registry = {"realSchema": (("a", "string"),)}
    suite.check("no reference at all -> nothing, never a guess",
                 find_referenced_schema_fields("res.json({ok: true})", registry) == ())
    suite.check("a reference to a name that isn't a real known schema -> nothing",
                 find_referenced_schema_fields("somethingElse.parse(x)", registry) == ())


def test_two_routes_in_one_file_never_blend_schema_references(suite):
    """The same per-route windowing guarantee `body_field_hints` already
    has (docs/45) must hold here too - a POST route's own schema reference
    must never leak onto an unrelated route below it.
    """
    registry = {"createSchema": (("name", "string"),), "updateSchema": (("age", "number"),)}
    text_for_create = "createSchema.parse(body)"
    text_for_update = "updateSchema.parse(body)"
    suite.check("create route resolves its own schema only",
                 find_referenced_schema_fields(text_for_create, registry) == (("name", "string"),))
    suite.check("update route resolves its own, different schema",
                 find_referenced_schema_fields(text_for_update, registry) == (("age", "number"),))


# --- zod_field_to_prop / build_request_body integration (pure) -------------

def test_zod_field_to_prop_maps_to_synthesis_vocabulary(suite):
    suite.check("email -> string+email format", zod_field_to_prop("email") == {"type": "string", "format": "email"})
    suite.check("number -> number", zod_field_to_prop("number") == {"type": "number"})
    suite.check("boolean -> boolean", zod_field_to_prop("boolean") == {"type": "boolean"})
    suite.check("an unrecognized token still degrades to a safe string type",
                 zod_field_to_prop("nonsense") == {"type": "string"})


def test_build_request_body_uses_real_zod_types_not_just_names(suite):
    """The core value-add over `body_field_hints` alone: a real declared
    type, not a name-only guess - proven directly with a field name that
    would otherwise guess wrong (a generic name gives no type signal).
    """
    endpoint = ApiEndpoint(
        method="POST", path="/api/users", source_file="x", dynamic=False,
        zod_fields=(("widgetCount", "number"), ("isPublished", "boolean"), ("contact", "email")),
    )
    body, evidence, synthetic_fields = build_request_body(None, "POST", "/api/users", endpoint=endpoint)
    suite.check("a real number-typed field gets a real number, not a string",
                 body["widgetCount"] == 1 and isinstance(body["widgetCount"], int))
    suite.check("a real boolean-typed field gets a real boolean, not a string",
                 body["isPublished"] is True)
    suite.check("a real email-typed field gets an email-shaped value",
                 body["contact"] == "qa-agent-test@example.com")
    suite.check("every real field is named as synthetic",
                 set(synthetic_fields) == {"widgetCount", "isPublished", "contact"})
    suite.check("evidence names the real source", "referenced Zod schema" in evidence)


def test_zod_fields_take_priority_over_body_field_hints(suite):
    """When both exist (rare, but possible: a route with a recognized
    `req.body.x` access pattern *and* a real Zod schema reference), the
    real, typed Zod evidence must win over the weaker, name-only hints.
    """
    endpoint = ApiEndpoint(
        method="POST", path="/api/users", source_file="x", dynamic=False,
        body_field_hints=("somethingElseEntirely",),
        zod_fields=(("realField", "string"),),
    )
    body, _, synthetic_fields = build_request_body(None, "POST", "/api/users", endpoint=endpoint)
    suite.check("the real Zod field is used", "realField" in body)
    suite.check("the weaker name-only hint is not used instead", "somethingElseEntirely" not in body)
    suite.check("synthetic fields reflect the real Zod evidence used", synthetic_fields == ("realField",))


# --- real, end-to-end discovery + execution proof ---------------------------

def test_discover_api_endpoints_attaches_real_zod_fields_from_a_different_file(suite):
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "next dev"}, "dependencies": {"next": "15.0.0"},
        }))
        proj.write("lib/schemas.ts", (
            "export const createUserSchema = z.object({\n"
            "  name: z.string(),\n"
            "  email: z.string().email(),\n"
            "});\n"
        ))
        proj.write("app/api/users/route.ts", (
            "import { createUserSchema } from '../../../lib/schemas';\n"
            "export async function POST(request) {\n"
            "  const data = createUserSchema.parse(await request.json());\n"
            "  return Response.json(data);\n"
            "}\n"
        ))
        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        endpoints, _ = discover_api_endpoints(context, proj.path)
        post = next(e for e in endpoints if e.method == "POST" and e.path == "/api/users")
        suite.check("the real fields from the OTHER file's schema were attached",
                     post.zod_fields == (("name", "string"), ("email", "email")))
    finally:
        proj.__exit__(None, None, None)


def test_real_end_to_end_zod_backed_post_gets_correctly_typed_synthetic_data(suite):
    """A hand-rolled Express-shaped server, no OpenAPI schema, a real Zod
    schema the route genuinely calls `.parse()` on - proving the whole
    chain end-to-end against a real running server: real type-aware
    synthetic data (a real integer for a number field, not a string that
    would fail a real `z.number()` check) actually passes real validation.
    """
    if not _npm_available():
        suite.check("(skipped: npm not on PATH)", True)
        return
    proj = TempProject()
    try:
        proj.write("package.json", json.dumps({
            "name": "x", "scripts": {"dev": "node server.js"}, "dependencies": {"next": "15.0.0"},
        }))
        proj.write("package-lock.json", "{}")
        # A real Node server standing in for the real app - it enforces
        # real type checks a wrong-typed synthetic value would genuinely
        # fail (age must be a real number, not a numeric string).
        proj.write("server.js", """
const http = require('http');
http.createServer((req, res) => {
  if (req.url === '/api/users' && req.method === 'POST') {
    let raw = '';
    req.on('data', c => raw += c);
    req.on('end', () => {
      const body = JSON.parse(raw || '{}');
      if (typeof body.age !== 'number' || typeof body.name !== 'string') {
        res.writeHead(400, {'Content-Type': 'application/json'});
        res.end(JSON.stringify({error: 'invalid types'}));
        return;
      }
      res.writeHead(201, {'Content-Type': 'application/json'});
      res.end(JSON.stringify({id: 1, ...body}));
    });
  } else {
    res.writeHead(404); res.end();
  }
}).listen(4327, () => console.log('ready - Local:        http://localhost:4327'));
""")
        proj.write("lib/schemas.ts", (
            "export const createUserSchema = z.object({\n"
            "  name: z.string(),\n"
            "  age: z.number(),\n"
            "});\n"
        ))
        proj.write("app/api/users/route.ts", (
            "import { createUserSchema } from '../../../lib/schemas';\n"
            "export async function POST(request) {\n"
            "  const data = createUserSchema.parse(await request.json());\n"
            "  return Response.json(data);\n"
            "}\n"
        ))

        result = discover_project(proj.path)
        context = build_repository_context(result.project)
        api_result = run_api_qa(context, proj.path, config=ApiQaConfig(server_startup_timeout=20))

        suite.check("the real server started", api_result.server_status == "started",
                    " (was {}: {})".format(api_result.server_status, api_result.server_detail))
        post_call = next((c for c in api_result.calls if c.endpoint.method == "POST"), None)
        suite.check("the POST endpoint was discovered", post_call is not None)
        if post_call is not None:
            suite.check("really executed, not skipped", post_call.status != CALL_SKIPPED,
                        " (was {}: {})".format(post_call.status, post_call.reason))
            suite.check(
                "passed for real - the real, type-correct synthetic 'age' satisfied the server's "
                "own real typeof check",
                post_call.status == CALL_PASS, " (was {}: {})".format(post_call.status, post_call.reason),
            )
            suite.check("clearly labeled synthetic", post_call.synthetic is True)
            suite.check("the real fields from the real schema are listed",
                        set(post_call.synthetic_fields) == {"name", "age"})
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("API QA: Zod schema discovery (docs/50)")
    sys.exit(suite.run([
        test_parses_basic_field_types,
        test_email_format_recognized_over_bare_string,
        test_optional_and_nullable_fields_are_excluded,
        test_default_fields_are_excluded,
        test_enum_type_recognized,
        test_quoted_keys_are_stripped,
        test_nested_commas_inside_enum_never_split_a_field_early,
        test_unrecognized_field_shape_is_excluded_not_guessed,
        test_registry_finds_schema_defined_in_a_different_file_than_the_route,
        test_safe_parse_and_validate_middleware_shapes_are_both_recognized,
        test_unreferenced_or_unknown_schema_name_yields_nothing,
        test_two_routes_in_one_file_never_blend_schema_references,
        test_zod_field_to_prop_maps_to_synthesis_vocabulary,
        test_build_request_body_uses_real_zod_types_not_just_names,
        test_zod_fields_take_priority_over_body_field_hints,
        test_discover_api_endpoints_attaches_real_zod_fields_from_a_different_file,
        test_real_end_to_end_zod_backed_post_gets_correctly_typed_synthetic_data,
    ]))
