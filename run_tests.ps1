#!/usr/bin/env pwsh
# يشغّل كل مجموعات الاختبار الثلاث + الـ lint.
# سابقاً كان يشغّل api/ وحدها، فبقيت اختبارات recorder/ و dashboard/ خارج التغطية
# (واختبارات اللوحة كانت معطّلة أصلاً لغياب conftest يضيف مسارها).
$ErrorActionPreference = "Continue"
$root = "C:\Users\rr\Desktop\aoi"
$failed = @()

function Invoke-Suite($name, $dir, $paths) {
    Write-Output "`n=== TESTS: $name ==="
    Set-Location -LiteralPath $dir
    py -m pytest $paths -q --tb=short
    if ($LASTEXITCODE -ne 0) { $script:failed += $name }
}

# api: يُشغَّل من api/ ليلتقط pyproject (testpaths, asyncio_mode)
Invoke-Suite "api"       "$root\api"       "tests/"
# recorder و dashboard: استيراد مسطّح، كلٌّ من مجلّده
Invoke-Suite "recorder"  "$root\recorder"  "tests/"
Invoke-Suite "dashboard" "$root\dashboard" "tests/"

Write-Output "`n=== LINT ==="
Set-Location -LiteralPath "$root\api"
py -m ruff check src/ tests/ --fix
py -m ruff check src/ tests/
if ($LASTEXITCODE -ne 0) { $failed += "lint(api)" }

Set-Location -LiteralPath $root
py -m ruff check recorder/ dashboard/ --exclude tests
if ($LASTEXITCODE -ne 0) { $failed += "lint(recorder/dashboard)" }

Write-Output "`n=== SUMMARY ==="
if ($failed.Count -gt 0) {
    Write-Output ("FAILED: " + ($failed -join ", "))
    exit 1
}
Write-Output "all suites passed"
