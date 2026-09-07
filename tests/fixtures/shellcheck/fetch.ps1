# A real ShellCheck install for the shellcheck-adapter test suites
# (docs/step-log.md, Phase C Part 5) - not a runtime dependency of qa_agent
# itself. Run this once before running tests that need a live shellcheck
# binary; they skip gracefully (clearly reported, not silently) if this
# hasn't been done. shellcheck.exe itself is gitignored, like eslint's
# node_modules - re-run this script to restore it after a clean checkout.
#
# ShellCheck has no pip/npm distribution - it's a standalone Haskell binary,
# so unlike eslint (npm install) this fetches the official GitHub release
# directly rather than going through a package manager.
#
#   powershell -File tests/fixtures/shellcheck/fetch.ps1

$ErrorActionPreference = "Stop"
$version = "v0.11.0"
$dir = $PSScriptRoot
$zip = Join-Path $dir "shellcheck.zip"

Invoke-WebRequest -Uri "https://github.com/koalaman/shellcheck/releases/download/$version/shellcheck-$version.zip" -OutFile $zip
Expand-Archive -Path $zip -DestinationPath $dir -Force
Remove-Item $zip
Remove-Item (Join-Path $dir "LICENSE.txt") -ErrorAction SilentlyContinue
Remove-Item (Join-Path $dir "README.txt") -ErrorAction SilentlyContinue

& (Join-Path $dir "shellcheck.exe") --version
