<#
  run-tests.ps1 — run the Accounts Pilot test suite AND record every run.

  Each invocation:
    • runs pytest with verbose output
    • saves the full report to  test-reports/run-<timestamp>.txt
    • appends a one-line summary (timestamp, pass/fail counts, duration) to
      test-reports/history.log

  Usage:   .\scripts\run-tests.ps1            # all tests
           .\scripts\run-tests.ps1 -k walker  # filter (passed through to pytest)
#>
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $PytestArgs)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

$reportDir = Join-Path $root "test-reports"
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null

$stamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"
$reportFile = Join-Path $reportDir "run-$stamp.txt"
$historyFile = Join-Path $reportDir "history.log"

Write-Host "Running test suite -> $reportFile" -ForegroundColor Cyan
$start = Get-Date

# -r A = report all outcomes; tee output to the per-run report file
& $py -m pytest -v -r A @PytestArgs *>&1 | Tee-Object -FilePath $reportFile
$exit = $LASTEXITCODE

$dur = [math]::Round(((Get-Date) - $start).TotalSeconds, 2)
$content = Get-Content $reportFile -Raw

function Find-Count($pattern) {
  $m = [regex]::Match($content, $pattern)
  if ($m.Success) { return [int]$m.Groups[1].Value } else { return 0 }
}
$passed  = Find-Count '(\d+) passed'
$failed  = Find-Count '(\d+) failed'
$errors  = Find-Count '(\d+) error'
$skipped = Find-Count '(\d+) skipped'

$status = if ($exit -eq 0) { "PASS" } else { "FAIL" }
$summary = "{0} | {1} | passed={2} failed={3} errors={4} skipped={5} | {6}s | run-{0}.txt" -f `
  $stamp, $status, $passed, $failed, $errors, $skipped, $dur
Add-Content -Path $historyFile -Value $summary

Write-Host ""
if ($exit -eq 0) {
  Write-Host "RESULT: $status  ($passed passed, $dur s)" -ForegroundColor Green
} else {
  Write-Host "RESULT: $status  ($passed passed, $failed failed, $errors errors, $dur s)" -ForegroundColor Red
}
Write-Host "Recorded -> $historyFile"
exit $exit
