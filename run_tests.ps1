#!/usr/bin/env pwsh
# يشغّل كل مجموعات الاختبار الثلاث + lint + mypy strict، بلا تعديل تلقائي.
# سابقاً كان يشغّل api/ وحدها، فبقيت اختبارات recorder/ و dashboard/ خارج التغطية
# (واختبارات اللوحة كانت معطّلة أصلاً لغياب conftest يضيف مسارها).
$ErrorActionPreference = "Continue"
$root = $PSScriptRoot
$failed = @()

# اختيارُ المفسّر: `pythonLocation` أوّلاً لأنّ `setup-python` يضبطه في CI وفيه
# ثُبِّتت الحِزم. أمّا `py` فمُشغّلٌ يحلّ إلى أحدث نسخةٍ **مسجَّلة** — وهي 3.14
# على `windows-latest` لا 3.11 التي نصّبها العمل. بذلك مرّت ثلاثُ دفعاتٍ حمراء
# (2026-08-11 مرّتين، و2026-08-17) بلا أن يُشغَّل اختبارٌ واحد. محلياً `py` هو
# 3.11 نفسه فبقيت العلّةُ غيرَ مرئيّة إلّا على الخادم.
$pythonLocation = $env:pythonLocation
if ($pythonLocation -and (Test-Path -LiteralPath (Join-Path $pythonLocation "python.exe"))) {
    $python = Join-Path $pythonLocation "python.exe"
} else {
    $pythonCommand = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pythonCommand) { $pythonCommand = Get-Command python -ErrorAction Stop }
    $python = $pythonCommand.Source
}

# فحصٌ مُسبَق يقول أيَّ مفسّرٍ نستعمل. بلا هذا يُطبع "No module named pytest"
# ستَّ مرّاتٍ ثمّ "FAILED: api, recorder, dashboard, lint…, mypy" — وهو نصٌّ
# يُقرأ كفشلِ ثمانمئة اختبارٍ لا كمفسّرٍ خطأ، فيضيع البحثُ في المكان الخطأ.
#
# ونصوصُ المخرَج هنا ASCII وحدها، والعربيّةُ في التعليقات فقط. السببُ أنّ
# PowerShell 5.1 يقرأ ملفَّ .ps1 بلا BOM بترميز النظام لا UTF-8، فبايتُ الشرطة
# الطويلة (E2 80 94) يصير 0x94 أي علامةَ اقتباسٍ ذكيّةً يقبلها المحلّل خاتمةً
# للنصّ، ثمّ تفتح الشدّةُ (D9 91 → 0x91) نصّاً لا يُغلق: خطأُ تحليلٍ يُسقط الملفَّ
# كلَّه قبل أن يُشغَّل اختبارٌ واحد. والتعليقُ محصَّنٌ منه لأنّه يُلفظ إلى آخر
# السطر بلا نظرٍ في الاقتباس. (وضعُ BOM يحلّها أيضاً لكنّه بايتٌ خفيّ يُسقطه أيُّ
# محرّرٍ فيعود العطبُ صامتاً، ويُفسد سطرَ shebang أعلاه.)
Write-Output "python: $python"
& $python -c "import sys, pytest, ruff, mypy; print('deps ok on Python ' + sys.version.split()[0])"
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

# api: يُشغَّل من api/ ليلتقط pyproject (testpaths, asyncio_mode)
Invoke-Suite "api"       "$root\api"       "tests/"
# recorder و dashboard: استيراد مسطّح، كلٌّ من مجلّده
Invoke-Suite "recorder"  "$root\recorder"  "tests/"
Invoke-Suite "dashboard" "$root\dashboard" "tests/"

Write-Output "`n=== LINT ==="
Set-Location -LiteralPath "$root\api"
& $python -m ruff check src/ tests/
if ($LASTEXITCODE -ne 0) { $failed += "lint(api)" }

# اختباراتُ recorder/dashboard تُفحَص كاختبارات api: استثناؤها كان يخبّئ
# ثلاثةَ عشرَ مخالفةً لا يراها أحد، بلا مقابلٍ يستحقّ.
Set-Location -LiteralPath $root
& $python -m ruff check recorder/ dashboard/
if ($LASTEXITCODE -ne 0) { $failed += "lint(recorder/dashboard)" }

# رسمُ لوحة المفاتيح جافاسكربت لا يمسّها pytest: هذه الأداةُ تستخرج الدوالّ من
# الصفحة وتشغّلها على حمولةٍ تغطّي كلّ حالة. تُتخطّى بلا node بدل أن تُفشل.
Write-Output "`n=== PAGE (node) ==="
if (Get-Command node -ErrorAction SilentlyContinue) {
    & node "$root\dashboard\tools\check_key_render.mjs"
    if ($LASTEXITCODE -ne 0) { $failed += "page(check_key_render)" }
} else {
    Write-Output "node not found - skipping the render check"
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
