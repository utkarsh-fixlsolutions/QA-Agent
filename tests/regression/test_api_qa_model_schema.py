"""Real Mongoose model/schema discovery (`qa_agent.api_qa.model_schema`):
finding a real `new Schema({...})` + `mongoose.model('Name', schemaVar)`
pair, resolving which real model a route's own handler actually uses -
including across a real require/import reference to a separate controller
file, and a string-literal factory-by-name convention - and the resulting
`ApiEndpoint.model_fields` feeding `resolution.py`'s `build_request_body`
with real field-name-*and*-type evidence, the same tier `zod_fields`
already established.

Every fixture here is synthetic and hand-built to exercise a general
Express/Mongoose idiom - never copied from, or named after, any specific
real project. idurar-erp-crm is a validation target for this same code,
exercised separately, not hard-coded into this suite's own logic.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent.project import build_repository_context, discover_project  # noqa: E402
from qa_agent.api_qa import discovery as discovery_module  # noqa: E402
from qa_agent.api_qa.models import ApiEndpoint  # noqa: E402
from qa_agent.api_qa.resolution import build_request_body  # noqa: E402
from qa_agent.api_qa.synthesis import synthesize_value  # noqa: E402
from qa_agent.api_qa.model_schema import (  # noqa: E402
    build_mongoose_schema_registry,
    find_referenced_model_fields,
    mongoose_field_to_prop,
    parse_mongoose_schema_body,
)


def _context_for(files):
    proj = TempProject()
    for rel, text in files.items():
        proj.write(rel, text)
    result = discover_project(proj.path)
    context = build_repository_context(result.project)
    return context, proj


def _detailed_for(files):
    context, proj = _context_for(files)
    outcome = discovery_module.discover_api_endpoints_detailed(context, proj.path)
    return outcome, proj


# --- parse_mongoose_schema_body (pure) --------------------------------------

def test_object_form_required_field_is_recognized(suite):
    fields = parse_mongoose_schema_body("title: { type: String, required: true }")
    suite.check("the real required field, with its real type", fields == (("title", "String"),))


def test_bare_shorthand_is_never_required(suite):
    fields = parse_mongoose_schema_body("title: String, count: Number")
    suite.check("shorthand types carry no required clause - never guessed as required", fields == ())


def test_object_form_without_required_is_excluded(suite):
    fields = parse_mongoose_schema_body("nickname: { type: String, trim: true }")
    suite.check("a real options object with no required:true is not required either", fields == ())


def test_required_array_with_message_form_is_recognized(suite):
    fields = parse_mongoose_schema_body("password: { type: String, required: [true, 'password is required'] }")
    suite.check("Mongoose's own [true, message] required form is recognized",
                 fields == (("password", "String"),))


def test_objectid_type_is_recognized_via_dotted_path(suite):
    fields = parse_mongoose_schema_body("user: { type: mongoose.Schema.ObjectId, ref: 'Admin', required: true }")
    suite.check("only the real, final type segment is kept", fields == (("user", "ObjectId"),))


def test_multiple_required_and_optional_fields_mixed(suite):
    fields = parse_mongoose_schema_body(
        "email: { type: String, required: true }, "
        "name: { type: String, required: true }, "
        "photo: { type: String }, "
        "role: { type: String, default: 'owner' }"
    )
    suite.check("only the two real required fields are kept, in declared order",
                 fields == (("email", "String"), ("name", "String")))


# --- build_mongoose_schema_registry -----------------------------------------

def test_registry_binds_schema_to_its_real_exported_model_name(suite):
    proj = TempProject()
    try:
        proj.write("models/Admin.js", (
            "const mongoose = require('mongoose');\n"
            "const Schema = mongoose.Schema;\n"
            "const adminSchema = new Schema({\n"
            "  email: { type: String, required: true },\n"
            "  name: { type: String, required: true },\n"
            "  photo: { type: String },\n"
            "});\n"
            "module.exports = mongoose.model('Admin', adminSchema);\n"
        ))
        by_model_name, file_to_model_name = build_mongoose_schema_registry(proj.path)
        suite.check("the real model name is bound to its real required fields",
                     by_model_name.get("Admin") == (("email", "String"), ("name", "String")))
        model_file = (proj.path / "models" / "Admin.js").resolve()
        suite.check("the real defining file is bound back to its real model name",
                     file_to_model_name.get(model_file) == "Admin")
    finally:
        proj.__exit__(None, None, None)


def test_schema_and_model_in_different_files_is_not_resolved(suite):
    """A named, accepted scope boundary (this module's own docstring) -
    proven directly so a future change can't silently start guessing here.
    """
    proj = TempProject()
    try:
        proj.write("schema.js", "const mongoose = require('mongoose');\nconst s = new mongoose.Schema({ x: { type: String, required: true } });\nmodule.exports = s;\n")
        proj.write("model.js", "const mongoose = require('mongoose');\nconst s = require('./schema');\nmodule.exports = mongoose.model('Thing', s);\n")
        by_model_name, _ = build_mongoose_schema_registry(proj.path)
        suite.check("cross-file schema/model binding is a real, named gap - never guessed",
                     "Thing" not in by_model_name)
    finally:
        proj.__exit__(None, None, None)


# --- find_referenced_model_fields --------------------------------------------

def test_direct_import_usage_is_recognized(suite):
    proj = TempProject()
    try:
        proj.write("models/Product.js", (
            "const mongoose = require('mongoose');\n"
            "const s = new mongoose.Schema({ title: { type: String, required: true } });\n"
            "module.exports = mongoose.model('Product', s);\n"
        ))
        by_model_name, file_to_model_name = build_mongoose_schema_registry(proj.path)
        controller_abs = (proj.path / "controllers" / "productController.js").resolve()
        controller_text = (
            "const Product = require('../models/Product');\n"
            "exports.create = (req, res) => { const p = new Product(req.body); p.save(); };\n"
        )
        fields = find_referenced_model_fields(controller_abs, controller_text, by_model_name, file_to_model_name)
        suite.check("the real imported model's own required fields are attached",
                     fields == (("title", "String"),))
    finally:
        proj.__exit__(None, None, None)


def test_unused_import_is_not_mistaken_for_real_usage(suite):
    proj = TempProject()
    try:
        proj.write("models/Product.js", (
            "const mongoose = require('mongoose');\n"
            "const s = new mongoose.Schema({ title: { type: String, required: true } });\n"
            "module.exports = mongoose.model('Product', s);\n"
        ))
        by_model_name, file_to_model_name = build_mongoose_schema_registry(proj.path)
        controller_abs = (proj.path / "controllers" / "unrelated.js").resolve()
        # Imported, but never referenced again anywhere else in the file.
        controller_text = "const Product = require('../models/Product');\nexports.ping = (req, res) => res.send('ok');\n"
        fields = find_referenced_model_fields(controller_abs, controller_text, by_model_name, file_to_model_name)
        suite.check("an import with no real further usage is not treated as real evidence", fields == ())
    finally:
        proj.__exit__(None, None, None)


def test_string_literal_factory_convention_is_recognized(suite):
    """idurar-erp-crm's own real shape: a shared factory function that
    takes the model's own real, registered name as a plain string, with no
    import of the model at all in the file that registers the route's
    handler.
    """
    proj = TempProject()
    try:
        proj.write("models/Admin.js", (
            "const mongoose = require('mongoose');\n"
            "const s = new mongoose.Schema({\n"
            "  email: { type: String, required: true },\n"
            "  name: { type: String, required: true },\n"
            "});\n"
            "module.exports = mongoose.model('Admin', s);\n"
        ))
        by_model_name, file_to_model_name = build_mongoose_schema_registry(proj.path)
        controller_abs = (proj.path / "controllers" / "adminAuth.js").resolve()
        controller_text = (
            "const createAuthMiddleware = require('../middlewares/createAuthMiddleware');\n"
            "module.exports = createAuthMiddleware('Admin');\n"
        )
        fields = find_referenced_model_fields(controller_abs, controller_text, by_model_name, file_to_model_name)
        suite.check("the real model named by string literal is attached, with no import at all",
                     fields == (("email", "String"), ("name", "String")))
    finally:
        proj.__exit__(None, None, None)


def test_unrelated_string_never_matches_a_real_model(suite):
    by_model_name = {"Admin": (("email", "String"),)}
    fields = find_referenced_model_fields(Path("x.js"), "doSomething('NotAModel')", by_model_name, {})
    suite.check("a string that names no real, registered model matches nothing", fields == ())


# --- mongoose_field_to_prop / build_request_body integration (pure) --------

def test_mongoose_field_to_prop_maps_to_synthesis_vocabulary(suite):
    suite.check("String -> string", mongoose_field_to_prop("String") == {"type": "string"})
    suite.check("Number -> number", mongoose_field_to_prop("Number") == {"type": "number"})
    suite.check("ObjectId -> string+objectid format",
                 mongoose_field_to_prop("ObjectId") == {"type": "string", "format": "objectid"})
    suite.check("an unrecognized token still degrades to a safe string type",
                 mongoose_field_to_prop("SomeCustomType") == {"type": "string"})


def test_objectid_format_synthesizes_a_real_valid_shape(suite):
    value = synthesize_value("user", mongoose_field_to_prop("ObjectId"))
    suite.check("a real, valid 24-hex-character ObjectId shape, not the bare integer 1",
                 value == "507f1f77bcf86cd799439011")


def test_build_request_body_uses_real_model_types_not_just_names(suite):
    endpoint = ApiEndpoint(
        method="POST", path="/api/login", source_file="x", dynamic=False,
        model_fields=(("email", "String"), ("password", "String")),
    )
    body, evidence, synthetic_fields, _evidence_source = build_request_body(
        None, "POST", "/api/login", endpoint=endpoint,
    )
    suite.check("both real required fields from the model are present", set(body) == {"email", "password"})
    suite.check("the email-shaped field gets an email-shaped value", body["email"] == "qa-agent-test@example.com")
    suite.check("the password-shaped field gets a real password-policy-shaped value",
                 body["password"] == "QaAgentTest123!")
    suite.check("every real field is named as synthetic", set(synthetic_fields) == {"email", "password"})
    suite.check("evidence names the real Mongoose model source", "referenced Mongoose model" in evidence)


def test_model_fields_never_override_a_real_zod_reference(suite):
    """Both real signals happening to exist for the same endpoint is rare,
    but the existing, unmodified Zod precedence must still win - never a
    coin flip between two same-tier sources.
    """
    endpoint = ApiEndpoint(
        method="POST", path="/api/x", source_file="x", dynamic=False,
        zod_fields=(("fromZod", "string"),),
        model_fields=(("fromModel", "String"),),
    )
    body, _, _, _ = build_request_body(None, "POST", "/api/x", endpoint=endpoint)
    suite.check("the existing Zod precedence is unchanged", "fromZod" in body and "fromModel" not in body)


# --- real, end-to-end discovery proof (the idurar-erp-crm shape) -----------

def test_end_to_end_factory_delegated_login_gets_real_model_fields(suite):
    """The exact three-layer shape found in idurar-erp-crm: a route file
    that only references a controller by module alias, a controller that
    delegates to a shared factory by the model's own string name (no
    import, no direct `req.body` access at all), and the model file itself.
    Before this feature, this shape produced zero body evidence at all.
    """
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0", "mongoose": "8.0.0"}}),
        "server.js": (
            "const express = require('express');\n"
            "const app = express();\n"
            "const auth = require('./controllers/adminAuth');\n"
            "app.post('/login', auth.login);\n"
        ),
        "controllers/adminAuth.js": (
            "const createAuthMiddleware = require('../middlewares/createAuthMiddleware');\n"
            "module.exports = createAuthMiddleware('Admin');\n"
        ),
        "models/Admin.js": (
            "const mongoose = require('mongoose');\n"
            "const adminSchema = new mongoose.Schema({\n"
            "  email: { type: String, required: true },\n"
            "  name: { type: String, required: true },\n"
            "});\n"
            "module.exports = mongoose.model('Admin', adminSchema);\n"
        ),
    })
    try:
        login = next((e for e in outcome.endpoints if e.method == "POST" and e.path == "/login"), None)
        suite.check("the login endpoint was discovered", login is not None)
        suite.check("its real required fields were resolved through the factory-by-name convention",
                     login is not None and login.model_fields == (("email", "String"), ("name", "String")),
                     " (got: {})".format(login.model_fields if login else None))
        if login is not None:
            body, _, synthetic_fields, evidence_source = build_request_body(
                None, "POST", "/login", endpoint=login,
            )
            suite.check("a real, non-placeholder body is constructed", set(body) == {"email", "name"})
            suite.check("never falls back to the minimal 'qa-agent-test' placeholder",
                         "qa-agent-test" not in body)
    finally:
        proj.__exit__(None, None, None)


def test_end_to_end_route_chain_with_directly_imported_model(suite):
    """The same general capability via a `.route().verb()` chain (Phase 4)
    and a directly imported model (no factory-by-name indirection) -
    proving the two features compose rather than only working in isolation.
    """
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0", "mongoose": "8.0.0"}}),
        "server.js": (
            "const router = require('express').Router();\n"
            "const items = require('./itemController');\n"
            "router.route('/items').post(items.create);\n"
        ),
        "itemController.js": (
            "const Item = require('./models/Item');\n"
            "exports.create = (req, res) => { const i = new Item(req.body); i.save(); };\n"
        ),
        "models/Item.js": (
            "const mongoose = require('mongoose');\n"
            "const itemSchema = new mongoose.Schema({\n"
            "  title: { type: String, required: true },\n"
            "});\n"
            "module.exports = mongoose.model('Item', itemSchema);\n"
        ),
    })
    try:
        post = next((e for e in outcome.endpoints if e.method == "POST" and e.path == "/items"), None)
        suite.check("the chained POST route was discovered", post is not None)
        suite.check("its real required field was resolved through the imported model",
                     post is not None and post.model_fields == (("title", "String"),),
                     " (got: {})".format(post.model_fields if post else None))
    finally:
        proj.__exit__(None, None, None)


def test_unreferenced_model_is_never_wrongly_attached(suite):
    """A real model existing *somewhere* in the project must never leak
    onto an endpoint whose own handler never actually uses it.
    """
    outcome, proj = _detailed_for({
        "package.json": json.dumps({"name": "x", "scripts": {"start": "node server.js"}, "dependencies": {"express": "4.19.0", "mongoose": "8.0.0"}}),
        "server.js": (
            "const app = require('express')();\n"
            "app.post('/health-check', (req, res) => res.json({ok: true}));\n"
        ),
        "models/Unrelated.js": (
            "const mongoose = require('mongoose');\n"
            "const s = new mongoose.Schema({ secret: { type: String, required: true } });\n"
            "module.exports = mongoose.model('Unrelated', s);\n"
        ),
    })
    try:
        health = next((e for e in outcome.endpoints if e.path == "/health-check"), None)
        suite.check("an unrelated real model never attaches to an endpoint that never uses it",
                     health is not None and health.model_fields == ())
    finally:
        proj.__exit__(None, None, None)


if __name__ == "__main__":
    suite = Suite("Mongoose model/schema discovery")
    exit_code = suite.run([
        test_object_form_required_field_is_recognized,
        test_bare_shorthand_is_never_required,
        test_object_form_without_required_is_excluded,
        test_required_array_with_message_form_is_recognized,
        test_objectid_type_is_recognized_via_dotted_path,
        test_multiple_required_and_optional_fields_mixed,
        test_registry_binds_schema_to_its_real_exported_model_name,
        test_schema_and_model_in_different_files_is_not_resolved,
        test_direct_import_usage_is_recognized,
        test_unused_import_is_not_mistaken_for_real_usage,
        test_string_literal_factory_convention_is_recognized,
        test_unrelated_string_never_matches_a_real_model,
        test_mongoose_field_to_prop_maps_to_synthesis_vocabulary,
        test_objectid_format_synthesizes_a_real_valid_shape,
        test_build_request_body_uses_real_model_types_not_just_names,
        test_model_fields_never_override_a_real_zod_reference,
        test_end_to_end_factory_delegated_login_gets_real_model_fields,
        test_end_to_end_route_chain_with_directly_imported_model,
        test_unreferenced_model_is_never_wrongly_attached,
    ])
    raise SystemExit(exit_code)
