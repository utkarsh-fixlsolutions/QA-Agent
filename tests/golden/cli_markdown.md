# QA Agent Report

- **Run at:** <TIMESTAMP>
- **Input:** <PROJECT>\has_issues.py
- **Checked:** 1 file(s)
- **Tools:** ruff

## Findings (3)

| File | Line | Severity | Message | Tool |
| --- | ---: | --- | --- | --- |
| `<PROJECT>\has_issues.py` | 1 | error | F401: `json` imported but unused | ruff |
| `<PROJECT>\has_issues.py` | 5 | error | E711: Comparison to `None` should be `cond is None` | ruff |
| `<PROJECT>\has_issues.py` | 7 | error | F841: Local variable `y` is assigned to but never used | ruff |
