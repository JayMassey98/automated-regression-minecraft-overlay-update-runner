# Run the same lint checks as .github/workflows/self-test.yml locally.
# Each check is independent: missing tools fail only their own step; remaining checks still run.
[CmdletBinding()]
param()

$ErrorActionPreference = "Continue"
$script:Failures = 0

function Invoke-LintStep {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Tool,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    Write-Host ""
    Write-Host "===== $Name =====" -ForegroundColor Cyan
    $command = Get-Command $Tool -ErrorAction SilentlyContinue
    if (-not $command) {
        Write-Host "FAIL: Required tool '$Tool' is not installed or not on PATH." -ForegroundColor Red
        Write-Host "Install the latest version, then rerun this script. This check is skipped; other checks continue."
        $script:Failures++
        return
    }

    try {
        & $command.Source @Arguments
        if ($LASTEXITCODE -ne 0) {
            Write-Host "FAIL: $Name (exit code $LASTEXITCODE)" -ForegroundColor Red
            $script:Failures++
        } else {
            Write-Host "PASS: $Name" -ForegroundColor Green
        }
    } catch {
        Write-Host "FAIL: $Name - $($_.Exception.Message)" -ForegroundColor Red
        $script:Failures++
    }
}

$root = $PSScriptRoot
Push-Location $root
try {
    Invoke-LintStep "GitHub Actions workflow lint" "actionlint" @(".github/workflows/self-test.yml")
    Invoke-LintStep "Biome scripts check" "biome" @("check", "scripts/")
    Invoke-LintStep "Markdown lint" "markdownlint-cli2" @("README.md")
    Invoke-LintStep "Ruff lint" "ruff" @("check", ".")
    Invoke-LintStep "Ruff format verification" "ruff" @("format", "--check", ".")

    Write-Host ""
    if ($script:Failures -eq 0) {
        Write-Host "ALL LINT CHECKS PASSED." -ForegroundColor Green
        exit 0
    }
    Write-Host "$script:Failures lint check(s) failed. See above; checks were run independently." -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}
