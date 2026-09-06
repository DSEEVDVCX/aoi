#!/usr/bin/env pwsh
# Runs all three test suites + lint + mypy strict, with no auto-fixing.
# It previously ran api/ alone, leaving recorder/ and dashboard/ tests outside
# coverage (and the dashboard tests were disabled outright for lack of a
# conftest adding their path).
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$failed = @()

# Interpreter choice: `pythonLocation` first because `setup-python` sets it in
# CI and the packages were installed into it. `py` is a launcher that resolves
# to the newest **registered** version — 3.14 on `windows-latest`, not the 3.11
# the workflow installed. Three red batches went through that way (2026-08-11
# twice, and 2026-08-17) with not a single test executed. Locally `py` is the
# same 3.11, so the defect stayed invisible except on the server.
$pythonLocation = $env:pythonLocation
if ($pythonLocation -and (Test-Path -LiteralPath (Join-Path $pythonLocation "python.exe"))) {
    $python = Join-Path $pythonLocation "python.exe"
} else {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { $pythonCommand = Get-Command python -ErrorAction Stop }
    $python = $pythonCommand.Source
}

# A pre-check that says which interpreter we are using. Without it, "No module
# named pytest" prints six times followed by "FAILED: api, recorder, dashboard,
# lint..., mypy" — text that reads like eight hundred failing tests rather than
# a wrong interpreter, sending the search to the wrong place.
#
# Output strings here are ASCII only. The reason: PowerShell 5.1 reads a .ps1
# without BOM in the system codepage, not UTF-8, so the em-dash byte pair
# (E2 80 94) becomes 0x94 — a smart quote the parser accepts as a string
# terminator — and an Arabic letter with a high byte opens a string that never
# closes: a parse error that drops the whole file before a single test runs.
# Comments are immune because they are lexed to end-of-line without regard for
# quotes. (A BOM would fix it too, but it is an invisible byte any editor can
# drop, silently reintroducing the defect, and it breaks the shebang line
# above.)
#
# The versions are printed alongside because the difference between your
# machine and the server is what blinded this check: the same commit green
# here and red there, with the message never saying the tools differed. They
# are pinned exactly in `requirements-dev.txt`, so this line differing from
# the server's means your environment is stale, not that the code broke:
# reinstall.
Write-Output "python: $python"
& $python -c "import sys, pytest, ruff, mypy; from importlib.metadata import version as v; print('deps ok on Python ' + sys.version.split()[0] + ' | pytest ' + v('pytest') + ' | pytest-asyncio ' + v('pytest-asyncio') + ' | ruff ' + v('ruff') + ' | mypy ' + v('mypy'))"
if ($LASTEXITCODE -ne 0) {
    Write-Output "`nFATAL: pytest/ruff/mypy are not installed for THIS interpreter."
    Write-Output "       Fix: $python -m pip install -r requirements-dev.txt"
    exit 1
}

function Invoke-Suite($name, $dir, $paths) {
    Write-Output "`n=== TESTS: $name ==="
    Set-Location -LiteralPath $dir
    & $python -m pytest $paths -q --tb=short
    if ($LASTEXITCODE -ne 0) { $script:failed += $name }
}

# api: run from api/ so it picks up pyproject (testpaths, asyncio_mode)
Invoke-Suite "api"       "$root\api"       "tests/"
# recorder and dashboard: flat imports, each from its own folder
Invoke-Suite "recorder"  "$root\recorder"  "tests/"
Invoke-Suite "dashboard" "$root\dashboard" "tests/"

Write-Output "`n=== LINT ==="
Set-Location -LiteralPath "$root\api"
& $python -m ruff check src/ tests/
if ($LASTEXITCODE -ne 0) { $failed += "lint(api)" }

# recorder/dashboard tests are linted like api tests: exempting them was hiding
# thirteen violations nobody saw, for no benefit worth having.
Set-Location -LiteralPath $root
& $python -m ruff check recorder/ dashboard/
if ($LASTEXITCODE -ne 0) { $failed += "lint(recorder/dashboard)" }

# The dashboard's JavaScript rendering is not covered by pytest: the tools
# extract the functions from the page itself and run them on hostile payloads
# and operating states. Node is an explicit requirement so these checks cannot
# silently disappear from a local or CI run.
Write-Output "`n=== PAGE (node) ==="
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCommand) {
    Write-Output "FATAL: node is required for the dashboard render checks."
    Write-Output "       CI installs the pinned version from .github/workflows/ci.yml."
    $failed += "page(node missing)"
} else {
    & node --version
    & node "$root\dashboard\tools\check_key_render.mjs"
    if ($LASTEXITCODE -ne 0) { $failed += "page(check_key_render)" }
    & node "$root\dashboard\tools\check_tile_render.mjs"
    if ($LASTEXITCODE -ne 0) { $failed += "page(check_tile_render)" }
    & node "$root\dashboard\tools\check_watchlist_render.mjs"
    if ($LASTEXITCODE -ne 0) { $failed += "page(check_watchlist_render)" }
    & node "$root\dashboard\tools\check_loop_render.mjs"
    if ($LASTEXITCODE -ne 0) { $failed += "page(check_loop_render)" }
}

Write-Output "`n=== TYPES ==="
Set-Location -LiteralPath "$root\api"
& $python -m mypy src/fomo_api
if ($LASTEXITCODE -ne 0) { $failed += "mypy(api)" }

Write-Output "`n=== SUMMARY ==="
if ($failed.Count -gt 0) {
    Write-Output ("FAILED: " + ($failed -join ", "))
    exit 1
}
Write-Output "all suites passed"
