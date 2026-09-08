"""Temporary workspace & safe patch application (Phase E Part 2): workspace.py.

Pure filesystem tests, no network, no provider, no live LLM - `apply_repair`
only ever consumes a `RepairProposal`-shaped object as plain data. Real
`RepairProposal` instances are used throughout (imported from repair.py) so
these tests exercise the real production type, not a parallel stand-in - but
a handful of plain duck-typed doubles are also used specifically to prove
"malformed proposal" handling does not depend on the real dataclass at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import REPO_ROOT, Suite, TempProject  # noqa: E402

from qa_agent.ai.repair import RepairProposal  # noqa: E402
from qa_agent.ai.workspace import (  # noqa: E402
    AppliedRepair,
    TemporaryWorkspace,
    apply_repair,
    cleanup_workspace,
    create_workspace,
)
from qa_agent.ai import workspace as workspace_module  # noqa: E402


def _proposal(file, start_line, end_line, replacement,
              explanation="why", confidence=0.9, model="mock"):
    return RepairProposal(
        finding=None, explanation=explanation, replacement=replacement,
        confidence=confidence, model=model, file=str(file),
        start_line=start_line, end_line=end_line,
    )


class _Malformed:
    """A plain object satisfying none of RepairProposal's attributes -
    proves validation is duck-typed, not isinstance-based."""


def _workspace():
    """create_workspace(), narrowed for tests: a real temporary-directory
    creation failure here would mean the whole test environment is already
    broken, so tests assert this the same way every other Optional return
    in this project is asserted at its point of use (e.g. repair.py's own
    RepairProposal | None) - satisfies pyright's own narrowing, not just a
    human reader.
    """
    ws = create_workspace()
    assert ws is not None
    return ws


# --- create_workspace / cleanup_workspace -----------------------------------


def test_create_workspace_creates_a_real_directory(suite):
    ws = create_workspace()
    try:
        suite.check("a TemporaryWorkspace is returned", isinstance(ws, TemporaryWorkspace))
        assert ws is not None
        suite.check("its root really exists on disk", ws.root.is_dir())
        suite.check("it starts empty", ws.files == {})
    finally:
        cleanup_workspace(ws)


def test_create_workspace_uses_the_given_prefix(suite):
    ws = create_workspace(prefix="custom-prefix-")
    try:
        assert ws is not None
        suite.check("the directory name carries the given prefix",
                    ws.root.name.startswith("custom-prefix-"))
    finally:
        cleanup_workspace(ws)


def test_create_workspace_each_call_is_independent(suite):
    ws1 = create_workspace()
    ws2 = create_workspace()
    try:
        assert ws1 is not None and ws2 is not None
        suite.check("two calls produce two distinct directories", ws1.root != ws2.root)
    finally:
        cleanup_workspace(ws1)
        cleanup_workspace(ws2)


def test_create_workspace_failure_is_graceful(suite):
    """The exact scenario this part requires: a directory that cannot be
    created must never raise out of create_workspace() - it must come back
    as None.
    """
    original = workspace_module.tempfile.mkdtemp

    def _broken_mkdtemp(*args, **kwargs):
        raise OSError("simulated: disk full")

    workspace_module.tempfile.mkdtemp = _broken_mkdtemp
    try:
        result = create_workspace()
    finally:
        workspace_module.tempfile.mkdtemp = original
    suite.check("no workspace, no exception", result is None)


def test_cleanup_workspace_removes_the_directory(suite):
    ws = create_workspace()
    assert ws is not None
    root = ws.root
    cleanup_workspace(ws)
    suite.check("the directory is gone after cleanup", not root.exists())


def test_cleanup_workspace_is_safe_to_call_twice(suite):
    ws = create_workspace()
    assert ws is not None
    cleanup_workspace(ws)
    try:
        cleanup_workspace(ws)
        suite.check("a second cleanup on an already-removed workspace does not raise", True)
    except Exception as exc:  # noqa: BLE001
        suite.check("a second cleanup on an already-removed workspace does not raise", False,
                    "  raised {!r}".format(exc))


def test_cleanup_workspace_is_safe_on_none(suite):
    try:
        cleanup_workspace(None)
        suite.check("cleanup_workspace(None) does not raise", True)
    except Exception as exc:  # noqa: BLE001
        suite.check("cleanup_workspace(None) does not raise", False, "  raised {!r}".format(exc))


def test_workspace_context_manager_cleans_up_on_exit(suite):
    with _workspace() as ws:
        root = ws.root
        suite.check("the directory exists inside the with block", root.is_dir())
    suite.check("the directory is gone after the with block exits", not root.exists())


def test_no_filesystem_leak_after_cleanup(suite):
    """Applying a real repair (which creates a subdirectory inside the
    workspace) still leaves nothing behind once cleaned up.
    """
    with TempProject() as project:
        source = project / "leak.py"
        source.write_text("x = 1\n", encoding="utf-8", newline="")
        ws = create_workspace()
        assert ws is not None
        applied = apply_repair(ws, _proposal(source, 1, 1, "x = 2"))
        suite.check("the repair applied", applied.ok)
        root = ws.root
        cleanup_workspace(ws)
        suite.check("nothing survives cleanup, including the copied file's own subdirectory",
                    not root.exists())


# --- apply_repair: success, and originals are never touched ----------------


def test_apply_repair_success_single_file(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("import os\nimport json\n\nprint('hi')\n", encoding="utf-8", newline="")
        applied = apply_repair(ws, _proposal(source, 2, 2, ""))
        suite.check("an AppliedRepair is returned", isinstance(applied, AppliedRepair))
        suite.check("ok is True", applied.ok)
        suite.check("a workspace_file path is given", applied.workspace_file is not None)
        assert applied.workspace_file is not None
        suite.check("the workspace file really exists", applied.workspace_file.exists())
        suite.check("the workspace file lives inside the workspace, not the project",
                     ws.root in applied.workspace_file.parents)
        suite.check("patched_text reflects the deleted line",
                     applied.patched_text == "import os\n\nprint('hi')\n")
        suite.check("the workspace file on disk matches patched_text",
                     applied.workspace_file.read_text(encoding="utf-8") == applied.patched_text)


def test_apply_repair_original_file_is_byte_identical_after(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        original_bytes = b"import os\nimport json\n\nprint('hi')\n"
        source.write_bytes(original_bytes)
        applied = apply_repair(ws, _proposal(source, 2, 2, "# removed"))
        suite.check("the repair applied", applied.ok)
        suite.check("the real source file is byte-for-byte unchanged",
                     source.read_bytes() == original_bytes)


def test_apply_repair_replaces_only_the_requested_range(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\ntwo\nthree\nfour\nfive\n", encoding="utf-8", newline="")
        applied = apply_repair(ws, _proposal(source, 2, 3, "TWO\nTHREE"))
        assert applied.ok
        suite.check("lines outside the range and the replacement are exactly right",
                     applied.patched_text == "one\nTWO\nTHREE\nfour\nfive\n")


def test_apply_repair_multiline_replacement_can_change_line_count(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\ntwo\nthree\n", encoding="utf-8", newline="")
        applied = apply_repair(ws, _proposal(source, 2, 2, "TWO-A\nTWO-B\nTWO-C"))
        assert applied.ok
        suite.check("a one-line range can be replaced by several lines",
                     applied.patched_text == "one\nTWO-A\nTWO-B\nTWO-C\nthree\n")


# --- apply_repair: rejected inputs -------------------------------------------


def test_apply_repair_invalid_range_start_after_end(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\ntwo\nthree\n", encoding="utf-8", newline="")
        applied = apply_repair(ws, _proposal(source, 3, 1, "x"))
        suite.check("a backwards range is rejected, not silently swapped", not applied.ok)
        suite.check("a clear reason is given", bool(applied.error))


def test_apply_repair_invalid_range_zero_start_line(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\ntwo\n", encoding="utf-8", newline="")
        applied = apply_repair(ws, _proposal(source, 0, 1, "x"))
        suite.check("line 0 is rejected", not applied.ok)


def test_apply_repair_invalid_range_beyond_file_length(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\ntwo\n", encoding="utf-8", newline="")
        applied = apply_repair(ws, _proposal(source, 1, 99, "x"))
        suite.check("a range past the real end of the file is rejected", not applied.ok)
        suite.check("the file's real line count is named in the error",
                     "2" in (applied.error or ""))


def test_apply_repair_missing_source_file(suite):
    with TempProject() as project, _workspace() as ws:
        missing = project / "does_not_exist.py"
        applied = apply_repair(ws, _proposal(missing, 1, 1, "x"))
        suite.check("a missing file produces a clear failure, not a crash", not applied.ok)
        suite.check("the missing path is named", str(missing) in (applied.error or ""))


def test_apply_repair_malformed_proposal_missing_attributes(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\n", encoding="utf-8", newline="")
        applied = apply_repair(ws, _Malformed())
        suite.check("an object with none of the required attributes is rejected", not applied.ok)
        suite.check("no exception escaped", True)  # implicit: we got here at all


def test_apply_repair_malformed_proposal_wrong_types(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\n", encoding="utf-8", newline="")
        bad = _proposal(source, "one", 1, "x")  # start_line is a string, not an int
        applied = apply_repair(ws, bad)
        suite.check("a non-integer start_line is rejected", not applied.ok)


def test_apply_repair_malformed_proposal_boolean_line_not_accepted_as_int(suite):
    """bool is an int subclass in Python - the same trap already guarded
    against in config.py and response_parser.py (Phase D Part 6 / E Part 1).
    """
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\n", encoding="utf-8", newline="")
        bad = _proposal(source, True, 1, "x")
        applied = apply_repair(ws, bad)
        suite.check("start_line=True is rejected, not accepted as line 1", not applied.ok)


def test_apply_repair_malformed_proposal_non_string_replacement(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\n", encoding="utf-8", newline="")
        bad = _proposal(source, 1, 1, None)
        applied = apply_repair(ws, bad)
        suite.check("a non-string replacement is rejected", not applied.ok)


def test_apply_repair_no_workspace(suite):
    applied = apply_repair(None, _proposal("whatever.py", 1, 1, "x"))
    suite.check("a missing workspace produces a clear failure, not a crash", not applied.ok)


def test_apply_repair_never_raises_on_an_attribute_that_raises(suite):
    """The last line of defence: even a proposal whose attribute access
    itself raises must not be able to crash apply_repair.
    """
    class _Explosive:
        @property
        def file(self):
            raise RuntimeError("something nobody anticipated")

    with TempProject(), _workspace() as ws:
        applied = apply_repair(ws, _Explosive())
        suite.check("an exception inside attribute access still yields ok=False, not a crash",
                     not applied.ok)


# --- multiple / sequential repairs, and multiple files ----------------------


def test_apply_repair_multiple_sequential_repairs_same_file(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("one\ntwo\nthree\n", encoding="utf-8", newline="")
        first = apply_repair(ws, _proposal(source, 1, 1, "ONE"))
        assert first.ok
        # The second repair's line numbers are relative to the file *after*
        # the first repair, exactly as apply_repair documents.
        second = apply_repair(ws, _proposal(source, 3, 3, "THREE"))
        assert second.ok
        suite.check("both edits are present in the final content",
                     second.patched_text == "ONE\ntwo\nTHREE\n")
        suite.check("only one copy of the file exists in the workspace (reused, not re-copied)",
                     len(ws.files) == 1)
        suite.check("the original file is still completely untouched",
                     source.read_text(encoding="utf-8") == "one\ntwo\nthree\n")


def test_apply_repair_multiple_files_in_one_workspace(suite):
    with TempProject() as project, _workspace() as ws:
        a = project / "a.py"
        b = project / "b.py"
        a.write_text("aaa\n", encoding="utf-8", newline="")
        b.write_text("bbb\n", encoding="utf-8", newline="")
        applied_a = apply_repair(ws, _proposal(a, 1, 1, "AAA"))
        applied_b = apply_repair(ws, _proposal(b, 1, 1, "BBB"))
        suite.check("both repairs succeed independently", applied_a.ok and applied_b.ok)
        assert applied_a.workspace_file is not None and applied_b.workspace_file is not None
        suite.check("two distinct files are tracked", len(ws.files) == 2)
        suite.check("each workspace copy has its own content",
                     applied_a.workspace_file.read_text(encoding="utf-8") == "AAA\n"
                     and applied_b.workspace_file.read_text(encoding="utf-8") == "BBB\n")


def test_apply_repair_same_basename_different_directories_does_not_collide(suite):
    with TempProject() as project, _workspace() as ws:
        (project / "pkg1").mkdir()
        (project / "pkg2").mkdir()
        a = project / "pkg1" / "util.py"
        b = project / "pkg2" / "util.py"
        a.write_text("from pkg1\n", encoding="utf-8", newline="")
        b.write_text("from pkg2\n", encoding="utf-8", newline="")
        applied_a = apply_repair(ws, _proposal(a, 1, 1, "FROM PKG1"))
        applied_b = apply_repair(ws, _proposal(b, 1, 1, "FROM PKG2"))
        suite.check("both succeed despite sharing a basename", applied_a.ok and applied_b.ok)
        assert applied_a.workspace_file is not None and applied_b.workspace_file is not None
        suite.check("their workspace copies are different files",
                     applied_a.workspace_file != applied_b.workspace_file)
        suite.check("neither copy's content leaked into the other",
                     applied_a.workspace_file.read_text(encoding="utf-8") == "FROM PKG1\n"
                     and applied_b.workspace_file.read_text(encoding="utf-8") == "FROM PKG2\n")


# --- encoding and newline preservation --------------------------------------


def test_apply_repair_preserves_unicode_content(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_text("greeting = 'héllo wörld 🎉'\nprint(greeting)\n", encoding="utf-8", newline="")
        applied = apply_repair(ws, _proposal(source, 2, 2, "print(greeting.upper())  # ünïcödé"))
        assert applied.ok and applied.patched_text is not None and applied.workspace_file is not None
        suite.check("unicode on the untouched line survives", "héllo wörld 🎉" in applied.patched_text)
        suite.check("unicode in the replacement itself survives", "ünïcödé" in applied.patched_text)
        suite.check("the workspace file decodes correctly as UTF-8",
                     applied.workspace_file.read_text(encoding="utf-8") == applied.patched_text)


def test_apply_repair_preserves_utf8_bom(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_bytes(b"\xef\xbb\xbfx = 1\ny = 2\n")
        applied = apply_repair(ws, _proposal(source, 2, 2, "y = 3"))
        assert applied.ok and applied.workspace_file is not None
        patched_bytes = applied.workspace_file.read_bytes()
        suite.check("the BOM is preserved on the patched workspace copy",
                     patched_bytes.startswith(b"\xef\xbb\xbf"))
        suite.check("the content after the BOM is correct",
                     patched_bytes == b"\xef\xbb\xbfx = 1\ny = 3\n")


def test_apply_repair_preserves_crlf_newlines(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_bytes(b"one\r\ntwo\r\nthree\r\n")
        applied = apply_repair(ws, _proposal(source, 2, 2, "TWO"))
        assert applied.ok and applied.workspace_file is not None
        patched_bytes = applied.workspace_file.read_bytes()
        suite.check("CRLF is used throughout the patched file, including around the replacement",
                     patched_bytes == b"one\r\nTWO\r\nthree\r\n")


def test_apply_repair_preserves_lf_newlines(suite):
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_bytes(b"one\ntwo\nthree\n")
        applied = apply_repair(ws, _proposal(source, 2, 2, "TWO"))
        assert applied.ok and applied.workspace_file is not None
        patched_bytes = applied.workspace_file.read_bytes()
        suite.check("plain LF is preserved, not switched to CRLF",
                     patched_bytes == b"one\nTWO\nthree\n")


def test_apply_repair_multiline_replacement_adopts_the_files_own_newline_style(suite):
    """The model's replacement text arrives as a plain Python string
    (prompts.py/response_parser.py never see raw bytes) - its own internal
    line breaks must not leak a foreign newline style into a CRLF file.
    """
    with TempProject() as project, _workspace() as ws:
        source = project / "a.py"
        source.write_bytes(b"one\r\ntwo\r\nthree\r\n")
        applied = apply_repair(ws, _proposal(source, 2, 2, "TWO-A\nTWO-B"))
        assert applied.ok and applied.workspace_file is not None
        patched_bytes = applied.workspace_file.read_bytes()
        suite.check("the replacement's own line break also becomes CRLF",
                     patched_bytes == b"one\r\nTWO-A\r\nTWO-B\r\nthree\r\n")


# --- provider independence ---------------------------------------------------


def test_apply_repair_is_provider_independent(suite):
    """apply_repair must behave identically no matter which provider/model
    produced the proposal - it only ever looks at file/start_line/end_line/
    replacement.
    """
    with TempProject() as project:
        source = project / "a.py"
        source.write_text("one\ntwo\n", encoding="utf-8", newline="")
        results = []
        for model_name in ("ollama", "mock", "some-future-provider"):
            with _workspace() as ws:
                proposal = _proposal(source, 2, 2, "TWO", model=model_name)
                applied = apply_repair(ws, proposal)
                results.append(applied.patched_text)
        suite.check("identical patched output regardless of proposal.model",
                     len(set(results)) == 1 and results[0] == "one\nTWO\n")


def test_workspace_module_does_not_import_provider_or_prompt_infrastructure(suite):
    """Checked directly against the real source: workspace.py has no reason
    to import a provider, a prompt builder, or a schema module. Checks for
    actual import statements rather than bare word occurrences - the same
    false-positive class documented in test_ai_provider.py/test_ai_repair.py
    (a docstring merely *mentioning* a concept, e.g. this module's own "no
    provider is imported" sentence, must not trip an isolation check).
    """
    source = (REPO_ROOT / "qa_agent" / "ai" / "workspace.py").read_text(encoding="utf-8")
    forbidden_imports = [
        "from .provider", "from .ollama", "from .mock", "from .prompts",
        "from .schemas", "from .response_parser",
        "import provider", "import ollama", "import mock", "import prompts",
        "import schemas", "import response_parser",
    ]
    found = [token for token in forbidden_imports if token in source]
    suite.check("no provider/prompt/schema imports appear in workspace.py's real source",
                not found, "  [{}]".format(found))


def test_workspace_module_never_touches_git_or_the_real_pipeline(suite):
    source = (REPO_ROOT / "qa_agent" / "ai" / "workspace.py").read_text(encoding="utf-8")
    forbidden_imports = [
        "import subprocess", "from .gitdiff", "from .runner", "from .analysis_bridge",
    ]
    found = [token for token in forbidden_imports if token in source]
    suite.check("no subprocess/git/pipeline imports appear in workspace.py's real source",
                not found, "  [{}]".format(found))


if __name__ == "__main__":
    suite = Suite("Temporary workspace & safe patch application (Phase E Part 2)")
    sys.exit(suite.run([
        test_create_workspace_creates_a_real_directory,
        test_create_workspace_uses_the_given_prefix,
        test_create_workspace_each_call_is_independent,
        test_create_workspace_failure_is_graceful,
        test_cleanup_workspace_removes_the_directory,
        test_cleanup_workspace_is_safe_to_call_twice,
        test_cleanup_workspace_is_safe_on_none,
        test_workspace_context_manager_cleans_up_on_exit,
        test_no_filesystem_leak_after_cleanup,
        test_apply_repair_success_single_file,
        test_apply_repair_original_file_is_byte_identical_after,
        test_apply_repair_replaces_only_the_requested_range,
        test_apply_repair_multiline_replacement_can_change_line_count,
        test_apply_repair_invalid_range_start_after_end,
        test_apply_repair_invalid_range_zero_start_line,
        test_apply_repair_invalid_range_beyond_file_length,
        test_apply_repair_missing_source_file,
        test_apply_repair_malformed_proposal_missing_attributes,
        test_apply_repair_malformed_proposal_wrong_types,
        test_apply_repair_malformed_proposal_boolean_line_not_accepted_as_int,
        test_apply_repair_malformed_proposal_non_string_replacement,
        test_apply_repair_no_workspace,
        test_apply_repair_never_raises_on_an_attribute_that_raises,
        test_apply_repair_multiple_sequential_repairs_same_file,
        test_apply_repair_multiple_files_in_one_workspace,
        test_apply_repair_same_basename_different_directories_does_not_collide,
        test_apply_repair_preserves_unicode_content,
        test_apply_repair_preserves_utf8_bom,
        test_apply_repair_preserves_crlf_newlines,
        test_apply_repair_preserves_lf_newlines,
        test_apply_repair_multiline_replacement_adopts_the_files_own_newline_style,
        test_apply_repair_is_provider_independent,
        test_workspace_module_does_not_import_provider_or_prompt_infrastructure,
        test_workspace_module_never_touches_git_or_the_real_pipeline,
    ]))
