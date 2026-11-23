# Run the CI lint checks locally. With -Install, install or upgrade the tools and submodules first.
# rumdl is a local Markdown-lint substitute for CI's markdownlint-cli2, not the same tool.
[CmdletBinding()]
param(
    [switch]$Install
)

$ErrorActionPreference = "Continue"
$script:Failures = 0

$ToolPackages = @(
    @{ Name = "actionlint"; Id = "rhysd.actionlint"; Command = "actionlint" },
    @{ Name = "Biome"; Id = "BiomeJS.Biome"; Command = "biome" },
    @{ Name = "Ruff"; Id = "astral-sh.ruff"; Command = "ruff" },
    @{ Name = "rumdl (local markdownlint-cli2 substitute)"; Id = "rvben.rumdl"; Command = "rumdl" }
)

function Invoke-InstallOrUpgrade {
    param([Parameter(Mandatory = $true)]$Package)

    Write-Host ""
    Write-Host "===== Install/update $($Package.Name) =====" -ForegroundColor Cyan
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Write-Host "FAIL: WinGet is unavailable; cannot install $($Package.Name)." -ForegroundColor Red
        $script:Failures++
        return
    }

    # Use WinGet's installed-package listing to choose install vs upgrade.
    $listing = & winget list --id $Package.Id --exact --accept-source-agreements 2>&1
    $listExit = $LASTEXITCODE
    $isInstalled = ($listExit -eq 0) -and (($listing | Out-String) -match [regex]::Escape($Package.Id))

    if ($isInstalled) {
        Write-Host "Updating $($Package.Name) to latest available..." 
        & winget upgrade --id $Package.Id --exact --silent --accept-package-agreements --accept-source-agreements
        $result = $LASTEXITCODE
        if ($result -ne 0) {
            Write-Host "FAIL: Upgrade attempt for $($Package.Name) failed (exit $result)." -ForegroundColor Red
            $script:Failures++
        } else {
            Write-Host "Install/update command completed for $($Package.Name)." -ForegroundColor Green
        }
    } else {
        Write-Host "Installing $($Package.Name)..." 
        & winget install --id $Package.Id --exact --silent --accept-package-agreements --accept-source-agreements
        $result = $LASTEXITCODE
        if ($result -ne 0) {
            Write-Host "FAIL: Install attempt for $($Package.Name) failed (exit $result)." -ForegroundColor Red
            $script:Failures++
        } else {
            Write-Host "Installed $($Package.Name)." -ForegroundColor Green
        }
    }
}

function Sync-GitSubmodules {
    param([switch]$PullLatest)

    Write-Host ""
    if ($PullLatest) {
        Write-Host "===== Update Git submodules to latest remote =====" -ForegroundColor Cyan
    } else {
        Write-Host "===== Sync Git submodules to pinned commit =====" -ForegroundColor Cyan
    }

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "FAIL: Git is unavailable; cannot sync submodules." -ForegroundColor Red
        $script:Failures++
        return
    }

    if ($PullLatest) {
        & git submodule update --init --recursive --remote --merge
    } else {
        & git submodule update --init --recursive
    }

    $result = $LASTEXITCODE
    if ($result -ne 0) {
        Write-Host "FAIL: Git submodule command failed (exit code $result)." -ForegroundColor Red
        $script:Failures++
    } else {
        Write-Host "PASS: Git submodules synced." -ForegroundColor Green
    }
}

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
        Write-Host "Install it (or rerun with -Install). This check is skipped; remaining checks continue."
        $script:Failures++
        return
    }

    try {
        & $command.Source @Arguments
        $result = $LASTEXITCODE
        if ($result -ne 0) {
            Write-Host "FAIL: $Name (exit code $result)" -ForegroundColor Red
            $script:Failures++
        } else {
            Write-Host "PASS: $Name" -ForegroundColor Green
        }
    } catch {
        Write-Host "FAIL: $Name - $($_.Exception.Message)" -ForegroundColor Red
        $script:Failures++
    }
}

function Set-PythonScriptExecutablePermissions {
    $files = @(
        "scripts/build_version_pack.py",
        "scripts/generate_test_runner.py",
        "scripts/get_minecraft_versions.py",
        "scripts/run_server_tests.py"
    )

    Write-Host ""
    Write-Host "===== Ensure Python scripts are executable in Git =====" -ForegroundColor Cyan

    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "FAIL: Git is unavailable; cannot update executable permissions." -ForegroundColor Red
        $script:Failures++
        return
    }

    foreach ($file in $files) {
        & git update-index --chmod=+x -- $file
        $result = $LASTEXITCODE

        if ($result -ne 0) {
            Write-Host "FAIL: Could not set executable permission for $file." -ForegroundColor Red
            $script:Failures++
        } else {
            Write-Host "PASS: Set executable permission for $file in Git index." -ForegroundColor Green
        }
    }
}

Push-Location (Split-Path -Parent $PSScriptRoot)
try {
    if ($Install) {
        foreach ($package in $ToolPackages) {
            Invoke-InstallOrUpgrade $package
        }
        Write-Host ""
        Write-Host "Note: newly installed tools may not be visible to this PowerShell process if PATH changed. If a command is not found, reopen PowerShell and rerun without -Install." -ForegroundColor Yellow
    }

    Sync-GitSubmodules -PullLatest:$Install
    Set-PythonScriptExecutablePermissions

    Invoke-LintStep "GitHub Actions workflow lint" "actionlint" @(".github/workflows/self-test.yml")
    Invoke-LintStep "Biome scripts check" "biome" @("check", "scripts/")
    Write-Host "NOTE: rumdl is a local Markdown-lint substitute for CI markdownlint-cli2." -ForegroundColor Yellow
    Invoke-LintStep "Local Markdown lint (rumdl)" "rumdl" @("check", "README.md")
    Invoke-LintStep "Ruff lint" "ruff" @("check", "--fix", ".")
    Invoke-LintStep "Ruff format" "ruff" @("format", ".")

    Write-Host ""
    if ($script:Failures -eq 0) {
        Write-Host "ALL LOCAL CHECKS PASSED." -ForegroundColor Green
        exit 0
    }
    Write-Host "$script:Failures step(s) failed. See output; checks ran independently." -ForegroundColor Red
    exit 1
} finally {
    Pop-Location
}