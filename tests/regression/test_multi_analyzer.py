"""Phase C Part 2: the ESLint adapter's parsing, and the generic runner
mechanisms it needed (docs/14-first-multi-language-analyzer.md) - ok_exit_codes,
cwd anchoring, and the shutil.which pre-check for a shell-launched tool.

Pure unit tests: no live eslint process anywhere in this file. The JSON
fixtures below are real captured `eslint --format=json` output (trimmed of
fields the adapter never reads, e.g. `fix`/`suggestions`), not hand-invented -
recorded while writing docs/14, verified against an actual eslint 9.39.5 run.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from harness import Suite, TempProject  # noqa: E402

from qa_agent import runner  # noqa: E402
from qa_agent.adapters import (  # noqa: E402
    ADAPTERS, ESLintAdapter, Finding, MypyAdapter, PyrightAdapter, RuffAdapter, ShellCheckAdapter,
    ToolError,
)

# --- real captured eslint --format=json shapes ------------------------------

CLEAN = """[{"filePath":"C:\\\\proj\\\\clean.js","messages":[],"suppressedMessages":[],
"errorCount":0,"warningCount":0,"fatalErrorCount":0}]"""

FINDINGS = """[{"filePath":"C:\\\\proj\\\\bad.js","messages":[
{"ruleId":"no-unused-vars","severity":2,"message":"'reallyUnused' is assigned a value but never used.","line":1,"column":5},
{"ruleId":"eqeqeq","severity":1,"message":"Expected '===' and instead saw '=='.","line":5,"column":12}
],"suppressedMessages":[],"errorCount":1,"warningCount":1,"fatalErrorCount":0}]"""

FATAL_SYNTAX_ERROR = """[{"filePath":"C:\\\\proj\\\\broken.js","messages":[
{"ruleId":null,"fatal":true,"severity":2,"message":"Parsing error: Unexpected keyword 'return'","line":2,"column":3}
],"suppressedMessages":[],"errorCount":1,"warningCount":0,"fatalErrorCount":1}]"""

NODE_MODULES_IGNORED = """[{"filePath":"C:\\\\proj\\\\node_modules\\\\pkg\\\\index.js","messages":[
{"ruleId":null,"fatal":false,"severity":1,
"message":"File ignored by default because it is located under the node_modules directory.","nodeType":null}
],"suppressedMessages":[],"errorCount":0,"warningCount":1,"fatalErrorCount":0}]"""


def test_eslint_parses_a_clean_file(suite):
    findings = ESLintAdapter().parse(CLEAN)
    suite.check("clean file yields no findings", findings == [])


def test_eslint_parses_real_findings(suite):
    findings = ESLintAdapter().parse(FINDINGS)
    suite.check("both messages become findings", len(findings) == 2)
    error, warning = findings
    suite.check("severity 2 decodes to 'error'", error.severity == "error")
    suite.check("severity 1 decodes to 'warning'", warning.severity == "warning")
    suite.check("ruleId is prefixed onto the message",
                error.message == "no-unused-vars: 'reallyUnused' is assigned a value but never used.")
    suite.check("line comes from the message, not the file", error.line == 1 and warning.line == 5)
    suite.check("tool is always 'eslint'", all(f.tool == "eslint" for f in findings))
    suite.check("file is the JSON's filePath verbatim", error.file == "C:\\proj\\bad.js")
    suite.check("findings are the shared Finding type", all(isinstance(f, Finding) for f in findings))


def test_eslint_fatal_syntax_error_is_a_finding_not_a_crash(suite):
    findings = ESLintAdapter().parse(FATAL_SYNTAX_ERROR)
    suite.check("a fatal parse error becomes exactly one finding", len(findings) == 1)
    suite.check("null ruleId falls back to the bare message",
                findings[0].message == "Parsing error: Unexpected keyword 'return'")
    suite.check("still classified as an error", findings[0].severity == "error")


def test_eslint_node_modules_advisory_is_a_finding(suite):
    findings = ESLintAdapter().parse(NODE_MODULES_IGNORED)
    suite.check("the ignored-file advisory becomes one finding, not a crash", len(findings) == 1)
    suite.check("reported as a warning, matching eslint's own severity 1",
                findings[0].severity == "warning")


def test_eslint_rejects_invalid_json_as_a_tool_error(suite):
    try:
        ESLintAdapter().parse("not json at all")
        suite.check("invalid JSON raises ToolError", False)
    except ToolError:
        suite.check("invalid JSON raises ToolError", True)


def test_eslint_empty_stdout_is_no_findings(suite):
    suite.check("empty stdout parses to no findings", ESLintAdapter().parse("   ") == [])


# --- real captured pyright --outputjson shapes (Phase C Part 5) -------------

PYRIGHT_CLEAN = """{"version":"1.1.411","generalDiagnostics":[],
"summary":{"filesAnalyzed":1,"errorCount":0,"warningCount":0,"informationCount":0}}"""

PYRIGHT_TYPE_ERROR = """{"version":"1.1.411","generalDiagnostics":[
{"file":"C:\\\\proj\\\\bad.py","severity":"error",
"message":"Operator \\"+\\" not supported for types \\"int\\" and \\"Literal['hello']\\"",
"range":{"start":{"line":1,"character":11},"end":{"line":1,"character":22}},
"rule":"reportOperatorIssue"}
],"summary":{"filesAnalyzed":1,"errorCount":1,"warningCount":0,"informationCount":0}}"""

# A fatal parse error has no `rule` at all - verified against real output.
PYRIGHT_NO_RULE = """{"version":"1.1.411","generalDiagnostics":[
{"file":"C:\\\\proj\\\\broken.py","severity":"error","message":"Expected expression",
"range":{"start":{"line":4,"character":0},"end":{"line":4,"character":1}}}
],"summary":{"filesAnalyzed":1,"errorCount":1,"warningCount":0,"informationCount":0}}"""


def test_pyright_parses_a_clean_file(suite):
    suite.check("clean file yields no findings", PyrightAdapter().parse(PYRIGHT_CLEAN) == [])


def test_pyright_parses_a_real_type_error(suite):
    findings = PyrightAdapter().parse(PYRIGHT_TYPE_ERROR)
    suite.check("one finding", len(findings) == 1)
    finding = findings[0]
    suite.check("severity passed through verbatim (pyright's own word)",
                finding.severity == "error")
    suite.check("rule is prefixed onto the message",
                finding.message.startswith("reportOperatorIssue: "))
    suite.check("line is converted from pyright's 0-indexed range - verified "
                "against real output (a known line-2 error reported as line 1)",
                finding.line == 2)
    suite.check("tool is 'pyright'", finding.tool == "pyright")
    suite.check("file is the JSON's file field verbatim", finding.file == "C:\\proj\\bad.py")


def test_pyright_missing_rule_falls_back_to_bare_message(suite):
    findings = PyrightAdapter().parse(PYRIGHT_NO_RULE)
    suite.check("one finding, not a crash on the missing rule field", len(findings) == 1)
    suite.check("bare message, no prefix", findings[0].message == "Expected expression")


def test_pyright_lowercase_drive_letter_is_normalized(suite):
    """The real bug found by testing a mixed-language project, not by
    inspection: pyright echoes a lowercase drive letter ("c:\\..."), unlike
    every other adapter, which silently broke Part 3's "sorted by file"
    guarantee (pyright's own findings always sorted last, since "c" > "C").
    """
    lowercase = """{"generalDiagnostics":[
    {"file":"c:\\\\Users\\\\x\\\\bad.py","severity":"error","message":"m",
    "range":{"start":{"line":0,"character":0},"end":{"line":0,"character":1}}}
    ]}"""
    finding = PyrightAdapter().parse(lowercase)[0]
    suite.check("the drive letter is uppercased", finding.file == "C:\\Users\\x\\bad.py")


def test_pyright_rejects_invalid_json(suite):
    try:
        PyrightAdapter().parse("not json")
        suite.check("invalid JSON raises ToolError", False)
    except ToolError:
        suite.check("invalid JSON raises ToolError", True)


# --- real captured shellcheck --format=json shapes (Phase C Part 5) ---------

SHELLCHECK_CLEAN = "[]"

# Real captured output: unlike eslint/pyright, shellcheck's JSON is already a
# flat list of findings - no per-file grouping to unwrap.
SHELLCHECK_FINDINGS = """[
{"file":"scratch/simple.sh","line":2,"column":6,"level":"info","code":2086,
"message":"Double quote to prevent globbing and word splitting."},
{"file":"scratch/err.sh","line":2,"column":6,"level":"style","code":2160,
"message":"Instead of '[ true ]', just use 'true'."},
{"file":"scratch/err.sh","line":6,"column":1,"level":"error","code":1089,
"message":"Parsing stopped here. Is this keyword correctly matched up?"}
]"""


def test_shellcheck_parses_a_clean_file(suite):
    suite.check("empty array yields no findings", ShellCheckAdapter().parse(SHELLCHECK_CLEAN) == [])


def test_shellcheck_parses_real_findings(suite):
    findings = ShellCheckAdapter().parse(SHELLCHECK_FINDINGS)
    suite.check("all three messages become findings", len(findings) == 3)
    info, style, error = findings
    suite.check("severity is shellcheck's own 'level' word, passed through verbatim",
                {info.severity, style.severity, error.severity} == {"info", "style", "error"})
    suite.check("the numeric code is prefixed as SC<code>, matching shellcheck's own wiki naming",
                info.message == "SC2086: Double quote to prevent globbing and word splitting.")
    suite.check("line comes straight from the JSON, no index adjustment needed",
                info.line == 2 and error.line == 6)
    suite.check("tool is 'shellcheck'", all(f.tool == "shellcheck" for f in findings))
    suite.check("findings are the shared Finding type", all(isinstance(f, Finding) for f in findings))


def test_shellcheck_rejects_invalid_json(suite):
    try:
        ShellCheckAdapter().parse("not json")
        suite.check("invalid JSON raises ToolError", False)
    except ToolError:
        suite.check("invalid JSON raises ToolError", True)


# --- real captured `mypy -O json` shapes (Phase C Part 7) -------------------
#
# Unlike every other adapter here, mypy prints one JSON object *per line*
# (JSON Lines), not a single array or object - verified directly against a
# real 2.3.1 run, not assumed from documentation.

MYPY_CLEAN = ""

MYPY_TYPE_ERROR = (
    '{"file": "C:\\\\proj\\\\bad.py", "line": 6, "column": 13, "end_line": 6, '
    '"end_column": 20, "message": "Argument 1 to \\"add\\" has incompatible type '
    '\\"str\\"; expected \\"int\\"", "hint": null, "code": "arg-type", "severity": "error"}'
)

MYPY_WITH_HINT = (
    '{"file": "C:\\\\proj\\\\hint.py", "line": 3, "column": 11, "end_line": 3, '
    '"end_column": 12, "message": "Unsupported operand types for + (\\"None\\" and \\"int\\")", '
    '"hint": "Left operand is of type \\"int | None\\"", "code": "operator", "severity": "error"}'
)

# Two findings across two files, one JSON object per line - the shape a real
# multi-file batch produces.
MYPY_MULTIPLE_FINDINGS = MYPY_TYPE_ERROR + "\n" + (
    '{"file": "C:\\\\proj\\\\note.py", "line": 2, "column": 12, "end_line": 2, '
    '"end_column": 13, "message": "Revealed type is \\"int\\"", "hint": null, '
    '"code": "misc", "severity": "note"}'
)


def test_mypy_requests_absolute_paths(suite):
    """The real bug found while building this adapter (docs/step-log.md,
    Phase C Part 7): without --show-absolute-path, mypy's own "file" field
    is inconsistent even within one invocation - some already-absolute input
    files come back relative to mypy's own cwd, others stay absolute -
    verified directly with a real multi-file batch and again on a real
    external repository. Left alone, this would silently break Part 3's
    "sorted by file" guarantee, the same way Part 5's pyright lowercase-
    drive-letter bug did. The flag is the whole fix - no parse()-side
    normalizer needed, unlike pyright's.
    """
    command = MypyAdapter().build_command(["a.py"])
    suite.check("--show-absolute-path is always requested",
                "--show-absolute-path" in command)


def test_mypy_parses_a_clean_file(suite):
    suite.check("empty stdout yields no findings", MypyAdapter().parse(MYPY_CLEAN) == [])


def test_mypy_parses_a_real_type_error(suite):
    findings = MypyAdapter().parse(MYPY_TYPE_ERROR)
    suite.check("one finding", len(findings) == 1)
    finding = findings[0]
    suite.check("code is prefixed onto the message",
                finding.message.startswith("arg-type: "))
    suite.check("severity passed through verbatim (mypy's own word)",
                finding.severity == "error")
    suite.check("line comes straight from the JSON, no index adjustment needed",
                finding.line == 6)
    suite.check("tool is 'mypy'", finding.tool == "mypy")
    suite.check("file is the JSON's file field verbatim", finding.file == "C:\\proj\\bad.py")
    suite.check("findings are the shared Finding type", isinstance(finding, Finding))


def test_mypy_hint_is_appended_to_the_message(suite):
    """mypy's own supplementary suggestion - genuinely useful, kept rather
    than dropped, unlike every other adapter's fields this project ignores.
    """
    finding = MypyAdapter().parse(MYPY_WITH_HINT)[0]
    suite.check("the hint is appended to the message, not lost",
                finding.message.endswith("(hint: Left operand is of type \"int | None\")"))


def test_mypy_parses_multiple_findings_json_lines(suite):
    """The real structural difference from every other adapter here: JSON
    Lines, not a single array or object - one JSON object per stdout line.
    """
    findings = MypyAdapter().parse(MYPY_MULTIPLE_FINDINGS)
    suite.check("both lines become findings", len(findings) == 2)
    suite.check("both files represented",
                {f.file for f in findings} == {"C:\\proj\\bad.py", "C:\\proj\\note.py"})
    note = next(f for f in findings if f.file == "C:\\proj\\note.py")
    suite.check("mypy's own 'note' severity is passed through verbatim, not invented",
                note.severity == "note")


def test_mypy_rejects_invalid_json(suite):
    try:
        MypyAdapter().parse("not json at all")
        suite.check("invalid JSON raises ToolError", False)
    except ToolError:
        suite.check("invalid JSON raises ToolError", True)


def test_mypy_one_bad_line_does_not_silently_drop_the_rest(suite):
    """A single malformed line must be reported, not swallowed - and must
    not silently discard whatever valid findings came before it either.
    """
    mixed = MYPY_TYPE_ERROR + "\nnot json\n"
    try:
        MypyAdapter().parse(mixed)
        suite.check("a malformed line among real ones still raises ToolError", False)
    except ToolError as exc:
        suite.check("a malformed line among real ones still raises ToolError", True)
        suite.check("the error names which line", "line 2" in str(exc))


# --- adapter contract: all five registered adapters declare it consistently -

def test_adapter_contracts(suite):
    ruff, eslint = RuffAdapter(), ESLintAdapter()
    suite.check("ruff claims .py only", ruff.extensions == frozenset({".py"}))
    suite.check("eslint claims .js only (TypeScript deliberately deferred)",
                eslint.extensions == frozenset({".js"}))
    suite.check("ruff's --exit-zero means only 0 is ever a normal exit",
                ruff.ok_exit_codes == frozenset({0}))
    suite.check("eslint allows exit 1 too (findings present, not a failure)",
                eslint.ok_exit_codes == frozenset({0, 1}))
    suite.check("ruff is a real .exe, no shell needed", ruff.use_shell is False)
    suite.check("eslint's npm .cmd shim needs a shell on Windows", eslint.use_shell is True)
    suite.check("all five adapters are registered (Phase C Parts 5 and 7)",
                {type(a) for a in ADAPTERS} ==
                {RuffAdapter, ESLintAdapter, PyrightAdapter, ShellCheckAdapter, MypyAdapter})
    suite.check("pyright shares .py with ruff - the first real exercise of two "
                "adapters on one extension",
                PyrightAdapter().extensions == frozenset({".py"}))
    suite.check("shellcheck claims a brand new extension none of the others touch",
                ShellCheckAdapter().extensions == frozenset({".sh"}))
    suite.check("mypy shares .py with ruff and pyright - the first real exercise "
                "of three adapters on one extension",
                MypyAdapter().extensions == frozenset({".py"}))
    suite.check("mypy's exit-2 ambiguity (fatal failure vs. a syntax error mid-batch) "
                "is resolved conservatively - never in ok_exit_codes",
                MypyAdapter().ok_exit_codes == frozenset({0, 1}))


# --- the generic runner mechanisms Part 2 added, exercised directly ---------

class _FakeAdapter:
    """A minimal stand-in - proves the runner's mechanisms are adapter-blind,
    not secretly eslint-specific."""

    name = "fake"
    extensions = frozenset({".fake"})
    ok_exit_codes = frozenset({0, 3})  # a deliberately unusual set
    use_shell = False

    def build_command(self, files):
        return ["fake-tool", *files]

    def parse(self, stdout):
        return []


def test_ok_exit_codes_is_generic_not_hardcoded(suite):
    calls = {}

    def fake_run(command, **kwargs):
        calls.update(kwargs)

        class Proc:
            returncode = 3  # only "ok" for _FakeAdapter, would fail for ruff/eslint
            stdout = "[]"
            stderr = ""

        return Proc()

    original = runner.subprocess.run
    original_which = runner.shutil.which
    runner.subprocess.run = fake_run
    runner.shutil.which = lambda name: "/usr/bin/" + name  # pretend it's on PATH
    try:
        with TempProject() as root:
            target = root / "x.fake"
            target.write_text("", encoding="utf-8")
            result = runner._invoke(_FakeAdapter(), [target])
        suite.check("exit code 3 accepted because THIS adapter declared it ok", result == [])
        suite.check("use_shell is threaded through to subprocess.run",
                     calls.get("shell") is False)
    finally:
        runner.subprocess.run = original
        runner.shutil.which = original_which


def test_ok_exit_codes_rejects_what_isnt_declared(suite):
    def fake_run(command, **kwargs):
        class Proc:
            returncode = 1  # not in _FakeAdapter.ok_exit_codes = {0, 3}
            stdout = ""
            stderr = "kaboom"

        return Proc()

    original = runner.subprocess.run
    original_which = runner.shutil.which
    runner.subprocess.run = fake_run
    runner.shutil.which = lambda name: "/usr/bin/" + name
    try:
        with TempProject() as root:
            target = root / "x.fake"
            target.write_text("", encoding="utf-8")
            try:
                runner._invoke(_FakeAdapter(), [target])
                suite.check("an undeclared exit code raises ToolError", False)
            except ToolError as exc:
                suite.check("an undeclared exit code raises ToolError", True)
                suite.check("the tool's own stderr is surfaced verbatim", "kaboom" in str(exc))
    finally:
        runner.subprocess.run = original
        runner.shutil.which = original_which


def test_missing_tool_is_caught_before_ever_invoking_a_shell(suite):
    """The exact bug found while implementing this part: with use_shell=True,
    a genuinely missing tool never raises FileNotFoundError at all - the shell
    reports "not recognized" as an ordinary nonzero exit with empty stdout,
    which eslint's own ok_exit_codes={0,1} would otherwise misread as "ran
    cleanly, no findings". shutil.which() is checked first specifically to
    prevent that.
    """
    called = {"run": False}

    def fake_run(command, **kwargs):
        called["run"] = True
        raise AssertionError("subprocess.run must not be reached")

    class _ShellAdapter(_FakeAdapter):
        use_shell = True

    original = runner.subprocess.run
    original_which = runner.shutil.which
    runner.subprocess.run = fake_run
    runner.shutil.which = lambda name: None  # genuinely not found
    try:
        with TempProject() as root:
            target = root / "x.fake"
            target.write_text("", encoding="utf-8")
            try:
                runner._invoke(_ShellAdapter(), [target])
                suite.check("a missing tool raises before invoking anything", False)
            except ToolError as exc:
                suite.check("a missing tool raises before invoking anything", True)
                suite.check("...with the standard, clear message",
                            "not installed or not on PATH" in str(exc))
        suite.check("subprocess.run was never reached", called["run"] is False)
    finally:
        runner.subprocess.run = original
        runner.shutil.which = original_which


def test_batch_cwd_single_file(suite):
    with TempProject() as root:
        target = (root / "a.fake").resolve()
        target.write_text("", encoding="utf-8")
        cwd = runner._batch_cwd([target])
        suite.check("single file's cwd is its own directory", cwd == target.parent)


def test_batch_cwd_shared_directory(suite):
    with TempProject() as root:
        a = (root / "a.fake").resolve()
        b = (root / "b.fake").resolve()
        a.write_text("", encoding="utf-8")
        b.write_text("", encoding="utf-8")
        cwd = runner._batch_cwd([a, b])
        suite.check("shared directory is used directly", cwd == root.resolve())


def test_batch_cwd_nested_directories_climb_to_common_ancestor(suite):
    with TempProject() as root:
        nested = root / "src" / "deep"
        nested.mkdir(parents=True)
        a = (root / "top.fake").resolve()
        b = (nested / "bottom.fake").resolve()
        a.write_text("", encoding="utf-8")
        b.write_text("", encoding="utf-8")
        cwd = runner._batch_cwd([a, b])
        suite.check("common ancestor of a nested batch is the shared root", cwd == root.resolve())


def test_batch_cwd_no_common_ancestor_falls_back_to_none(suite):
    # Two absolute paths that cannot share a Windows drive-relative root -
    # os.path.commonpath raises ValueError for this by construction, with no
    # filesystem access at all, so this is deterministic without real drives.
    a = Path("C:/one/a.fake")
    b = Path("D:/two/b.fake") if sys.platform == "win32" else Path("/other-root/b.fake")
    if sys.platform != "win32":
        return suite.check("(skipped - drive-mismatch case is Windows-specific)", True)
    cwd = runner._batch_cwd([a, b])
    suite.check("no common ancestor falls back to None (inherited cwd), not a guess", cwd is None)


# --- Part 6: command-line length aware batching (docs/step-log.md, Phase C
# Part 6) - discovered by dogfooding a real, deeply-nested repository: a
# 166-file batch built a ~14,300 character eslint command, which cmd.exe
# rejected ("The command line is too long") as an ordinary exit code with
# empty stdout - silently read as "no findings" rather than a tool failure,
# because the old chunker only ever counted files, never the actual command
# length that a use_shell=True launch is subject to. ------------------------

def test_chunks_keeps_a_small_batch_together(suite):
    files = [Path("a.py"), Path("b.py"), Path("c.py")]
    batches = list(runner._chunks(files, use_shell=False))
    suite.check("a handful of short paths stays in one batch", batches == [files])


def test_chunks_respects_the_file_count_cap(suite):
    files = [Path("f{}.py".format(i)) for i in range(runner.MAX_FILES_PER_CALL + 1)]
    batches = list(runner._chunks(files, use_shell=False))
    suite.check("more files than the per-call cap still splits on count alone",
                len(batches) == 2 and len(batches[0]) == runner.MAX_FILES_PER_CALL)


def test_chunks_splits_by_length_for_shell_tools(suite):
    """The actual bug: enough files with long-enough paths must split even
    while comfortably under MAX_FILES_PER_CALL, when launched through a
    shell (eslint) - and must NOT split at the same file count when launched
    directly (ruff/pyright/shellcheck), matching cmd.exe's much smaller real
    line budget versus CreateProcess's.
    """
    long_name = "x" * 80
    files = [Path("D:/deeply/nested/project/{}{}.js".format(long_name, i)) for i in range(80)]

    shell_batches = list(runner._chunks(files, use_shell=True))
    direct_batches = list(runner._chunks(files, use_shell=False))

    suite.check("a shell-launched batch splits once the command would be too long for cmd.exe",
                len(shell_batches) > 1)
    suite.check("the same files launched directly (no cmd.exe involved) stay in one batch",
                len(direct_batches) == 1)
    suite.check("every file still appears exactly once, in order, across shell batches",
                [f for batch in shell_batches for f in batch] == files)


def test_chunks_never_drops_an_oversized_single_file(suite):
    """A single file whose own resolved path already exceeds the shell
    budget must still be sent - alone - rather than dropped or waiting
    forever for a batch that could never fit it.
    """
    huge = Path("D:/" + ("x" * 7000) + ".js")
    batches = list(runner._chunks([huge], use_shell=True))
    suite.check("the oversized file gets its own one-file batch", batches == [[huge]])


def test_run_merges_findings_across_multiple_length_split_batches(suite):
    """End-to-end proof at the run() level: a shell-launched adapter whose
    files would have built one too-long command now makes several _invoke
    calls instead, and every batch's findings still reach the final result -
    this is the real failure mode the bug had: before this fix, an oversized
    shell batch failed silently with zero findings, not an error and not a
    partial result.
    """
    calls = []

    class _ShellFindingsAdapter:
        name = "shellfake"
        extensions = frozenset({".shellfake"})
        ok_exit_codes = frozenset({0})
        use_shell = True

        def build_command(self, files):
            return ["fake-tool", *files]

        def parse(self, stdout):
            # stdout doubles as a call counter here, giving each batch's
            # finding a distinct file so dedup has no reason to merge them.
            return [Finding(file=stdout, line=1, severity="error", message="m", tool=self.name)]

    def fake_run(command, **kwargs):
        calls.append(command)

        class Proc:
            returncode = 0
            stdout = "batch-{}".format(len(calls))
            stderr = ""

        return Proc()

    original_adapters = runner.ADAPTERS
    original_run = runner.subprocess.run
    original_which = runner.shutil.which
    runner.ADAPTERS = (_ShellFindingsAdapter(),)
    runner.subprocess.run = fake_run
    runner.shutil.which = lambda name: "/usr/bin/" + name
    try:
        with TempProject() as root:
            # A long, real directory nesting - the exact shape that triggered
            # the bug - so each resolved path alone is long enough that only
            # a handful fit in the shell budget.
            deep = root
            for _ in range(3):
                deep = deep / ("segment_" + "x" * 80)
            deep.mkdir(parents=True)
            files = []
            for i in range(40):
                target = deep / "f{}.shellfake".format(i)
                target.write_text("", encoding="utf-8")
                files.append(target)

            result = runner.run(files)
    finally:
        runner.ADAPTERS = original_adapters
        runner.subprocess.run = original_run
        runner.shutil.which = original_which

    suite.check("the deeply-nested batch needed more than one shell invocation",
                len(calls) > 1)
    suite.check("every batch's findings survive, none silently lost",
                len(result.findings) == len(calls))


# --- Part 3: unified reporting - merge, deterministic order, dedup, result
# isolation (docs/15-unified-reporting.md), exercised directly -------------
#
# ruff and eslint never claim the same extension today, so a real duplicate
# or a real "two adapters on one file" scenario can't happen through them yet
# (docs/13 section 9 built the dispatch for exactly this future case). Tested
# here with two fake adapters sharing a fake extension instead - the same
# spirit as _FakeAdapter above: proving the mechanism is generic, not
# something that only happens to work for ruff+eslint's disjoint extensions.

class _OkProc:
    returncode = 0
    stdout = ""
    stderr = ""


class _FindingsAdapter:
    """A fake adapter that returns a fixed list of findings regardless of
    what subprocess.run is mocked to produce - lets a test control exactly
    what "two tools disagreeing/agreeing on the same file" looks like.
    """

    extensions = frozenset({".dup"})
    ok_exit_codes = frozenset({0})
    use_shell = False

    def __init__(self, name, findings):
        self.name = name
        self._findings = findings

    def build_command(self, files):
        return ["fake-tool", *files]

    def parse(self, stdout):
        return list(self._findings)


def _with_fake_adapters(adapters, body):
    """Run body() with runner.ADAPTERS replaced and subprocess/shutil mocked
    so no real process is ever launched - restores everything afterward even
    if body() raises.
    """
    original_adapters = runner.ADAPTERS
    original_run = runner.subprocess.run
    original_which = runner.shutil.which
    runner.ADAPTERS = adapters
    runner.subprocess.run = lambda command, **kw: _OkProc()
    runner.shutil.which = lambda name: "/usr/bin/" + name
    try:
        return body()
    finally:
        runner.ADAPTERS = original_adapters
        runner.subprocess.run = original_run
        runner.shutil.which = original_which


def test_ordering_is_deterministic_regardless_of_adapter_order(suite):
    """The same two adapters, registered in either order, must produce
    identical output - proving order comes from the findings themselves
    (file, then line), never from which adapter happened to run first.
    """
    a = Finding(file="z.dup", line=1, severity="error", message="m1", tool="alpha")
    b = Finding(file="a.dup", line=5, severity="error", message="m2", tool="beta")
    c = Finding(file="a.dup", line=2, severity="error", message="m3", tool="alpha")
    alpha = _FindingsAdapter("alpha", [a, c])
    beta = _FindingsAdapter("beta", [b])

    with TempProject() as root:
        target = root / "x.dup"
        target.write_text("", encoding="utf-8")

        order_1 = _with_fake_adapters(
            (alpha, beta), lambda: [f.message for f in runner.run([target]).findings])
        order_2 = _with_fake_adapters(
            (beta, alpha), lambda: [f.message for f in runner.run([target]).findings])

    suite.check("identical output regardless of adapter registration order", order_1 == order_2)
    suite.check("sorted by file then line, not by tool name or registration order",
                order_1 == ["m3", "m2", "m1"])  # a.dup:2, a.dup:5, z.dup:1


def test_duplicate_findings_are_removed_conservatively(suite):
    """The exact same file+line+severity+message from two different tools
    collapses to one finding. A finding that is merely similar - same file
    and line, a different message - is never merged.
    """
    exact_1 = Finding(file="x.dup", line=3, severity="error", message="same issue", tool="alpha")
    exact_2 = Finding(file="x.dup", line=3, severity="error", message="same issue", tool="beta")
    similar = Finding(file="x.dup", line=3, severity="error", message="different issue", tool="beta")
    alpha = _FindingsAdapter("alpha", [exact_1])
    beta = _FindingsAdapter("beta", [exact_2, similar])

    with TempProject() as root:
        target = root / "x.dup"
        target.write_text("", encoding="utf-8")
        result = _with_fake_adapters((alpha, beta), lambda: runner.run([target]))

    suite.check("the exact duplicate collapses to one finding", len(result.findings) == 2)
    messages = [f.message for f in result.findings]
    suite.check("the merely-similar finding is never merged away",
                "different issue" in messages)
    suite.check("both distinct messages present exactly once", sorted(messages) ==
                ["different issue", "same issue"])
    survivor = next(f for f in result.findings if f.message == "same issue")
    suite.check("which copy of the duplicate survives is deterministic (sorts first: alpha)",
                survivor.tool == "alpha")


def test_no_findings_lost_when_only_some_files_have_duplicates(suite):
    """Dedup must never remove a finding that merely shares a file with a
    duplicate elsewhere - only an exact (file, line, severity, message) match
    is ever collapsed.
    """
    dup_a = Finding(file="x.dup", line=1, severity="error", message="dup", tool="alpha")
    dup_b = Finding(file="x.dup", line=1, severity="error", message="dup", tool="beta")
    unique = Finding(file="x.dup", line=99, severity="warning", message="unrelated", tool="beta")
    alpha = _FindingsAdapter("alpha", [dup_a])
    beta = _FindingsAdapter("beta", [dup_b, unique])

    with TempProject() as root:
        target = root / "x.dup"
        target.write_text("", encoding="utf-8")
        result = _with_fake_adapters((alpha, beta), lambda: runner.run([target]))

    suite.check("3 reported, 1 exact duplicate removed, 2 remain", len(result.findings) == 2)
    suite.check("the unrelated finding on a different line always survives",
                any(f.message == "unrelated" for f in result.findings))


def test_a_failing_adapter_never_hides_a_successful_one(suite):
    """The runner-level proof of result isolation (docs/15-unified-reporting
    .md): one adapter raising ToolError must not prevent another, already-
    processed adapter's findings from being returned.
    """
    ok_finding = Finding(file="x.dup", line=1, severity="error", message="real", tool="good")

    class _GoodAdapter(_FindingsAdapter):
        pass

    class _BadAdapter(_FindingsAdapter):
        def parse(self, stdout):
            raise ToolError("simulated failure")

    good = _GoodAdapter("good", [ok_finding])
    bad = _BadAdapter("bad", [])

    with TempProject() as root:
        target = root / "x.dup"
        target.write_text("", encoding="utf-8")
        result = _with_fake_adapters((good, bad), lambda: runner.run([target]))

    suite.check("run() returns normally, does not raise", result is not None)
    suite.check("the good adapter's finding survives", len(result.findings) == 1 and
                result.findings[0].message == "real")
    suite.check("the bad adapter's failure is recorded, not silently dropped",
                len(result.tool_errors) == 1 and result.tool_errors[0][0] == "bad")
    suite.check("both adapters are still listed as attempted",
                set(result.tools_used) == {"good", "bad"})


# --- Part 4: config consumption inside run() (docs/16-configuration-system
# .md), exercised directly with fake adapters - the config object itself is
# tested separately in test_config.py; this proves run() actually consumes
# one correctly. --------------------------------------------------------

from qa_agent.config import Config  # noqa: E402


def test_config_none_is_byte_identical_to_no_config(suite):
    """The single hardest invariant of Part 4 (docs/16 section 14): passing
    config=None must behave exactly like never having built one at all.
    """
    finding = Finding(file="x.dup", line=1, severity="error", message="m", tool="alpha")
    alpha = _FindingsAdapter("alpha", [finding])
    with TempProject() as root:
        target = root / "x.dup"
        target.write_text("", encoding="utf-8")
        without = _with_fake_adapters((alpha,), lambda: runner.run([target]))
        with_none = _with_fake_adapters((alpha,), lambda: runner.run([target], config=None))
    suite.check("config=None matches the true default", len(without.findings) == 1)
    suite.check("...and so does explicitly passing None",
                [f.message for f in with_none.findings] == [f.message for f in without.findings])


def test_config_disables_an_adapter(suite):
    # Distinct fake extensions - if both shared .dup, disabling beta would
    # just let alpha's own dispatch pick up beta's file instead of the file
    # genuinely going unclaimed, since dispatch is by extension, not by which
    # specific adapter instance a test happens to be thinking about.
    class _BetaAdapter(_FindingsAdapter):
        extensions = frozenset({".dup2"})

    kept = Finding(file="x.dup", line=1, severity="error", message="kept", tool="alpha")
    dropped = Finding(file="y.dup2", line=1, severity="error", message="dropped", tool="beta")
    alpha = _FindingsAdapter("alpha", [kept])
    beta = _BetaAdapter("beta", [dropped])

    with TempProject() as root:
        (root / "x.dup").write_text("", encoding="utf-8")
        (root / "y.dup2").write_text("", encoding="utf-8")
        cfg = Config(analyzers=frozenset({"alpha"}))
        result = _with_fake_adapters(
            (alpha, beta), lambda: runner.run([root / "x.dup", root / "y.dup2"], config=cfg))

    suite.check("only the enabled adapter's finding survives", len(result.findings) == 1 and
                result.findings[0].message == "kept")
    suite.check("the disabled adapter's file is honestly skipped, not silently dropped",
                any(reason == "disabled by config" for _, reason in result.skipped))
    suite.check("...distinct from a genuinely unsupported extension",
                not any("no tool configured" in reason for _, reason in result.skipped))


def test_config_extra_ignore_and_include(suite):
    with TempProject() as root:
        (root / "keep_me").mkdir()
        (root / "keep_me" / "a.dup").write_text("", encoding="utf-8")
        (root / "skip_me").mkdir()
        (root / "skip_me" / "b.dup").write_text("", encoding="utf-8")
        alpha = _FindingsAdapter("alpha", [])
        cfg = Config(extra_ignore=frozenset({"skip_me"}))
        files, _ = _with_fake_adapters(
            (alpha,), lambda: runner.collect_paths(
                [root], ignored_dirs=runner.IGNORED_DIRS | cfg.extra_ignore))
        suite.check("the extra-ignored directory's file is never even collected",
                    not any(f.name == "b.dup" for f in files))
        suite.check("an unrelated directory is unaffected",
                    any(f.name == "a.dup" for f in files))

        cfg_included = Config(extra_ignore=frozenset({"skip_me"}), include=frozenset({"skip_me"}))
        effective = runner.IGNORED_DIRS | cfg_included.extra_ignore - cfg_included.include
        files_included, _ = runner.collect_paths([root], ignored_dirs=effective)
        suite.check("include overrides the ignore, the file is collected after all",
                    any(f.name == "b.dup" for f in files_included))


def test_config_severity_filter_hides_below_threshold(suite):
    err = Finding(file="x.dup", line=1, severity="error", message="e", tool="alpha")
    warn = Finding(file="x.dup", line=2, severity="warning", message="w", tool="alpha")
    unknown = Finding(file="x.dup", line=3, severity="unknown", message="u", tool="alpha")
    alpha = _FindingsAdapter("alpha", [err, warn, unknown])

    with TempProject() as root:
        target = root / "x.dup"
        target.write_text("", encoding="utf-8")
        cfg = Config(min_severity="error")
        result = _with_fake_adapters((alpha,), lambda: runner.run([target], config=cfg))

    messages = {f.message for f in result.findings}
    suite.check("the warning is hidden", "w" not in messages)
    suite.check("the error survives", "e" in messages)
    suite.check("an unrecognized severity is never silently hidden", "u" in messages)
    suite.check("the hidden count is reported, not just silently shrunk", result.filtered == 1)


def test_config_no_severity_filter_keeps_everything(suite):
    err = Finding(file="x.dup", line=1, severity="error", message="e", tool="alpha")
    warn = Finding(file="x.dup", line=2, severity="warning", message="w", tool="alpha")
    alpha = _FindingsAdapter("alpha", [err, warn])
    with TempProject() as root:
        target = root / "x.dup"
        target.write_text("", encoding="utf-8")
        result = _with_fake_adapters((alpha,), lambda: runner.run([target]))
    suite.check("no config -> no filtering at all", len(result.findings) == 2)
    suite.check("filtered count is zero", result.filtered == 0)


if __name__ == "__main__":
    suite = Suite("Multi-analyzer: ESLint adapter and generic runner mechanisms")
    sys.exit(suite.run([
        test_eslint_parses_a_clean_file,
        test_eslint_parses_real_findings,
        test_eslint_fatal_syntax_error_is_a_finding_not_a_crash,
        test_eslint_node_modules_advisory_is_a_finding,
        test_eslint_rejects_invalid_json_as_a_tool_error,
        test_eslint_empty_stdout_is_no_findings,
        test_pyright_parses_a_clean_file,
        test_pyright_parses_a_real_type_error,
        test_pyright_missing_rule_falls_back_to_bare_message,
        test_pyright_lowercase_drive_letter_is_normalized,
        test_pyright_rejects_invalid_json,
        test_shellcheck_parses_a_clean_file,
        test_shellcheck_parses_real_findings,
        test_shellcheck_rejects_invalid_json,
        test_mypy_requests_absolute_paths,
        test_mypy_parses_a_clean_file,
        test_mypy_parses_a_real_type_error,
        test_mypy_hint_is_appended_to_the_message,
        test_mypy_parses_multiple_findings_json_lines,
        test_mypy_rejects_invalid_json,
        test_mypy_one_bad_line_does_not_silently_drop_the_rest,
        test_adapter_contracts,
        test_ok_exit_codes_is_generic_not_hardcoded,
        test_ok_exit_codes_rejects_what_isnt_declared,
        test_missing_tool_is_caught_before_ever_invoking_a_shell,
        test_batch_cwd_single_file,
        test_batch_cwd_shared_directory,
        test_batch_cwd_nested_directories_climb_to_common_ancestor,
        test_batch_cwd_no_common_ancestor_falls_back_to_none,
        test_chunks_keeps_a_small_batch_together,
        test_chunks_respects_the_file_count_cap,
        test_chunks_splits_by_length_for_shell_tools,
        test_chunks_never_drops_an_oversized_single_file,
        test_run_merges_findings_across_multiple_length_split_batches,
        test_ordering_is_deterministic_regardless_of_adapter_order,
        test_duplicate_findings_are_removed_conservatively,
        test_no_findings_lost_when_only_some_files_have_duplicates,
        test_a_failing_adapter_never_hides_a_successful_one,
        test_config_none_is_byte_identical_to_no_config,
        test_config_disables_an_adapter,
        test_config_extra_ignore_and_include,
        test_config_severity_filter_hides_below_threshold,
        test_config_no_severity_filter_keeps_everything,
    ]))
