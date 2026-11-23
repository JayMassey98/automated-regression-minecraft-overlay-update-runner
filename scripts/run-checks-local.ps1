<#
    .SYNOPSIS
    Runs local code formatting and lint checks.

    .DESCRIPTION
    Runs the formatting and linting tools used to validate the repository
    before changes are pushed to GitHub.

    rumdl is used as a local substitute for CI's markdownlint-cli2.
    The tools are not identical, so results may differ slightly.

    .PARAMETER Install
    Installs the latest required tools and updates Git submodules
    to their latest remote revisions.

    .EXAMPLE
    .\run-checks-local.ps1

    Runs formatting and lint checks without modifying files.

    .EXAMPLE
    .\run-checks-local.ps1 -Install

    Installs the latest tools, updates Git submodules, and runs the checks.

    .NOTES
    Formatting fixes are offered interactively when the script runs.
#>

[CmdletBinding()]
param(
    [switch]$Install
)

# Continue running independent checks when an individual step fails.
$ErrorActionPreference = "Continue"

$script:FailureCount = 0

$ToolPackages = @(
    @{ Name = "actionlint"; Id = "rhysd.actionlint" }
    @{ Name = "Biome"; Id = "BiomeJS.Biome" }
    @{ Name = "Ruff"; Id = "astral-sh.ruff" }
    @{ Name = "rumdl"; Id = "rvben.rumdl" }
)

function Confirm-FileFixOption {

    Write-Section "File Fix Option"
    Write-Host ""
    Write-Host "(Y) Apply file fixes"
    Write-Host "(N) Check files only"
    Write-Host ""

    $Result = $null

    try {
        $Answer = Read-Host "Apply file fixes? (Y/[N])"
        $Result = ($Answer -match '^(?i)y$')
        Write-Host ""
        Write-Host "Selected: $Result"
    } catch {
        Write-Host ""
        Write-Host "Interactive input unavailable; defaulting to check files only." -ForegroundColor Yellow
    }

    return $Result
}

function Invoke-CheckStep {
    param(
        [Parameter(Mandatory)][string]$Name,
        [string]$Tool,
        [object]$Arguments = @(),
        [scriptblock]$ScriptBlock,
        [switch]$FailOnOutput
    )

    Write-Section $Name

    try {
        if ([bool]$Tool -eq [bool]$ScriptBlock) {
            throw "Specify exactly one of Tool or ScriptBlock."
        }

        if ($ScriptBlock) {
            & $ScriptBlock
            Write-Result "$Name completed." Pass
            return
        }

        $Command = Get-Command $Tool -ErrorAction SilentlyContinue

        if (-not $Command) {
            Write-Result "Required tool '$Tool' is missing or not on PATH." Fail
            Write-Host "Install it (or rerun with -Install). Continuing with remaining checks."
            return
        }

        if ($Command.CommandType -eq "Application") {
            $Output = & $Command @Arguments 2>&1
            $Result = $LASTEXITCODE

            $Output | ForEach-Object {
                if (-not [string]::IsNullOrWhiteSpace([string]$_)) {
                    Write-Host $_
                }
            }

            if ($Result -ne 0) {
                Write-Result "$Name failed (exit code $Result)." Fail
                return
            }
        } else {
            $Output = & $Command @Arguments

            if ($FailOnOutput -and $Output) {
                $Output | Format-Table -AutoSize | Out-Host
                Write-Result "$Name reported diagnostics." Fail
                return
            }
        }

        Write-Result "$Name completed." Pass

    } catch {

        $ErrorMessage = $_.Exception.Message -split "\r?\n"

        Write-Result $ErrorMessage[0] Fail

        if ($ErrorMessage.Count -gt 1) {
            $ErrorMessage | Select-Object -Skip 1 | ForEach-Object {
                Write-Host $_
            }
        }
    }
}

function Invoke-FormatPowerShell {
    param(
        [string]$Path = "scripts",
        [switch]$Fix
    )

    if (-not (Test-CommandAvailable "Invoke-Formatter")) {
        throw "Invoke-Formatter unavailable; install PSScriptAnalyzer."
    }

    $Files = @(Get-ChildItem -Path $Path -Filter "*.ps1" -Recurse -File)

    $ChangedFiles = @()

    foreach ($File in $Files) {
        $Original = Get-Content -Path $File.FullName -Raw
        $Formatted = Invoke-Formatter -ScriptDefinition $Original -Settings CodeFormattingOTBS

        if ($Original -eq $Formatted) {
            continue
        }

        $ChangedFiles += $File.Name

        if ($Fix) {
            Set-Content -Path $File.FullName -Value $Formatted -NoNewline
            Write-Host "Formatted $($File.Name)" -ForegroundColor Yellow
        } else {
            Write-Host "Formatting required: $($File.Name)" -ForegroundColor Yellow
        }
    }

    if ($ChangedFiles.Count -gt 0 -and -not $Fix) {
        throw "$($ChangedFiles.Count) PowerShell file(s) require formatting."
    }

    if ($ChangedFiles.Count -gt 0) {
        Write-Host "Formatted $($ChangedFiles.Count) PowerShell file(s)."
    } else {
        Write-Host "All PowerShell files are correctly formatted."
    }
}

function Install-PSScriptAnalyzer {
    Write-Section "Install/update PSScriptAnalyzer"

    try {
        $Installed = Get-Module -ListAvailable -Name PSScriptAnalyzer

        if ($Installed) {
            Write-Host "Updating PSScriptAnalyzer..."
            Update-Module -Name PSScriptAnalyzer -Scope CurrentUser -Force -ErrorAction Stop
        } else {
            Write-Host "Installing PSScriptAnalyzer..."
            Install-Module -Name PSScriptAnalyzer -Scope CurrentUser -Force `
                -SkipPublisherCheck -ErrorAction Stop
        }

        Write-Result "PSScriptAnalyzer installation completed." Pass
    } catch {
        Write-Result "PSScriptAnalyzer installation failed: $($_.Exception.Message)" Fail
    }
}

function Install-Tool {
    param([Parameter(Mandatory)][hashtable]$Package)

    Write-Section "Install/update $($Package.Name)"

    if (-not (Test-CommandAvailable "winget")) {
        Write-Result "WinGet unavailable; cannot install $($Package.Name)." Fail
        return
    }

    $Listing = & winget list --id $Package.Id --exact --accept-source-agreements 2>&1
    $ListExitCode = $LASTEXITCODE

    $Installed = ($ListExitCode -eq 0) -and (
        ($Listing | Out-String) -match [regex]::Escape($Package.Id)
    )

    $Operation = if ($Installed) { "upgrade" } else { "install" }
    $Verb = if ($Installed) { "Updating" } else { "Installing" }

    Write-Host "$Verb $($Package.Name)..."

    $Result = Invoke-WinGet -Operation $Operation -PackageId $Package.Id

    if ($Result -ne 0) {
        Write-Result "$Operation $($Package.Name) failed (exit code $Result)." Fail
        return
    }

    Write-Result "$($Package.Name) $Operation completed." Pass
}

function Invoke-WinGet {
    param(
        [Parameter(Mandatory)][string]$Operation,
        [Parameter(Mandatory)][string]$PackageId
    )

    & winget $Operation --id $PackageId --exact --silent `
        --accept-package-agreements --accept-source-agreements

    return $LASTEXITCODE
}


function Set-PythonScriptExecutablePermission {
    [CmdletBinding(SupportsShouldProcess)]
    param(
        [switch]$Fix
    )

    $Files = @(
        Get-ChildItem -Path "scripts" -Filter "*.py" -Recurse -File |
            ForEach-Object {
                Resolve-Path -Relative $_.FullName
            }
    )

    Write-Section "Enable Python Script Permissions"

    if (-not (Test-CommandAvailable "git")) {
        Write-Result "Git unavailable; cannot update executable permissions." Fail
        return
    }

    $AllSuccessful = $true

    foreach ($File in $Files) {
        if ($Fix) {
            if (-not $PSCmdlet.ShouldProcess($File, "Set executable bit in Git index")) {
                $AllSuccessful = $false
                continue
            }

            & git update-index --chmod=+x -- $File
            $Result = $LASTEXITCODE

            if ($Result -ne 0) {
                Write-Result "Could not set executable permission for $File (exit code $Result)." Fail
                $AllSuccessful = $false
            }

            continue
        }

        $IndexEntry = & git ls-files --stage -- $File
        $Result = $LASTEXITCODE

        if ($Result -ne 0 -or -not $IndexEntry) {
            Write-Result "Could not verify executable permission for $File." Fail
            $AllSuccessful = $false
            continue
        }

        if ($IndexEntry -notmatch '^100755\s') {
            Write-Result "$File is not executable in the Git index." Fail
            $AllSuccessful = $false
        }
    }

    if ($AllSuccessful) {
        Write-Result "Python script permissions enabled." Pass
    }
}

function Sync-GitSubmodule {
    param(
        [switch]$PullLatest
    )

    Write-Section "Sync Git Submodules"

    if (-not (Test-CommandAvailable "git")) {
        Write-Result "Git unavailable; cannot sync submodules." Fail
        return
    }

    $Arguments = if ($PullLatest) {
        @("submodule", "update", "--init", "--recursive", "--remote", "--merge")
    } else {
        @("submodule", "update", "--init", "--recursive")
    }

    & git @Arguments
    $Result = $LASTEXITCODE

    if ($Result -ne 0) {
        Write-Result "Submodule sync failed (exit code $Result)." Fail
        return
    }

    $Message = if ($PullLatest) {
        "Git submodules synced to latest remote revisions."
    } else {
        "Git submodules synced to pinned commits."
    }

    Write-Result $Message Pass
}


function Test-CommandAvailable {
    param([Parameter(Mandatory)][string]$Name)

    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Test-VariableNamingConvention {
    param(
        [string]$Path = "scripts"
    )

    $AutomaticVariables = @(
        "_"
        "?"
        "^"
        "args"
        "input"
        "this"
        "foreach"
        "switch"
        "true"
        "false"
        "null"
        "LASTEXITCODE"
        "OFS"
        "HOME"
        "PID"
        "PROFILE"
        "PWD"
    )

    $Issues = @()
    $Files = @(Get-ChildItem -Path $Path -Filter "*.ps1" -Recurse -File)

    foreach ($File in $Files) {
        $Tokens = $null
        $ParseErrors = $null

        $Ast = [System.Management.Automation.Language.Parser]::ParseFile(
            $File.FullName,
            [ref]$Tokens,
            [ref]$ParseErrors
        )

        if ($ParseErrors.Count -gt 0) {
            throw "Unable to parse $($File.Name): $($ParseErrors[0].Message)"
        }

        $Variables = $Ast.FindAll({
                param($Node)
                $Node -is [System.Management.Automation.Language.VariableExpressionAst]
            }, $true)

        foreach ($Variable in $Variables) {
            $VariablePath = $Variable.VariablePath

            # Ignore environment and other non-scope drives.
            if ($VariablePath.IsDriveQualified -and
                $VariablePath.DriveName -notin @("script", "global", "local", "private")) {
                continue
            }

            $VariableName = $VariablePath.UserPath -replace '^[^:]+:', ''

            # Ignore PowerShell automatic variables.
            if ($AutomaticVariables -contains $VariableName) {
                continue
            }

            # Require PascalCase.
            if ($VariableName -cnotmatch '^[A-Z][a-zA-Z0-9]*$') {
                $Issues += "$($File.Name):$($Variable.Extent.StartLineNumber) - `$$VariableName"
            }
        }
    }

    if ($Issues.Count -gt 0) {
        throw "PowerShell variable naming convention failed.`n`nVariables must use PascalCase:`n$($Issues -join "`n")"
    }
}

function Write-Result {
    param(
        [Parameter(Mandatory)][string]$Message,
        [Parameter(Mandatory)][ValidateSet("Fail", "Info", "Pass", "Warning")][string]$Status
    )

    $Colour = switch ($Status) {
        Fail { "Red" }
        Info { "Gray" }
        Pass { "Green" }
        Warning { "Yellow" }
    }

    Write-Host "${Status}: $Message" -ForegroundColor $Colour

    if ($Status -eq "Fail") {
        $script:FailureCount++
    }
}

function Write-Section {
    param([Parameter(Mandatory)][string]$Title)

    Write-Host ""
    Write-Host "================================================================================" -ForegroundColor Cyan
    Write-Host "$Title" -ForegroundColor Cyan
    Write-Host "================================================================================" -ForegroundColor Cyan
}

# =============================================================================
# Main
# =============================================================================

Push-Location (Split-Path -Parent $PSScriptRoot)

try {
    # -----------------
    # Tool Installation
    # -----------------

    $Fix = Confirm-FileFixOption

    if ($Install) {
        foreach ($Package in $ToolPackages) {
            Install-Tool -Package $Package
        }

        Install-PSScriptAnalyzer

        Write-Host ""
        Write-Host "Note: newly installed tools may not be visible to this PowerShell process if PATH changed. Reopen PowerShell and rerun if necessary." -ForegroundColor Yellow
    }

    # ----------------------
    # Repository Preparation
    # ----------------------

    Sync-GitSubmodule -PullLatest:$Install
    Set-PythonScriptExecutablePermission -Fix:$Fix

    # --------------------
    # Format Configuration
    # --------------------

    $BiomeFormatArguments = if ($Fix) {
        @("format", "--write", "scripts/")
    } else {
        @("format", "scripts/")
    }

    $RuffFormatArguments = if ($Fix) {
        @("format", ".")
    } else {
        @("format", "--check", ".")
    }

    $FormatChecks = @(
        @{
            Name      = "Format JavaScript Files (Biome)"
            Tool      = "biome"
            Arguments = $BiomeFormatArguments
        }
        @{
            Name        = "Format PowerShell Files (Invoke-Formatter)"
            ScriptBlock = {
                Invoke-FormatPowerShell -Path "scripts" -Fix:$Fix
            }
        }
        @{
            Name      = "Format Python Files (Ruff)"
            Tool      = "ruff"
            Arguments = $RuffFormatArguments
        }
    )

    # ------------------
    # Lint Configuration
    # ------------------

    $BiomeLintArguments = if ($Fix) {
        @("lint", "--write", "scripts/")
    } else {
        @("lint", "scripts/")
    }

    $RuffLintArguments = if ($Fix) {
        @("check", "--fix", ".")
    } else {
        @("check", ".")
    }

    $LintChecks = @(
        @{
            Name      = "Lint GitHub Workflows (ActionLint)"
            Tool      = "actionlint"
            Arguments = @(".github/workflows/self-test.yml")
        }
        @{
            Name      = "Lint JavaScript Files (Biome)"
            Tool      = "biome"
            Arguments = $BiomeLintArguments
        }
        @{
            Name      = "Lint Markdown Files (Rumdl)"
            Tool      = "rumdl"
            Arguments = @("check", "--color", "never", "README.md")
        }
        @{
            Name         = "Lint PowerShell Files (PSScriptAnalyzer)"
            Tool         = "Invoke-ScriptAnalyzer"
            Arguments    = @{
                Path        = "scripts"
                Recurse     = $true
                ExcludeRule = @("PSAvoidUsingWriteHost")
            }
            FailOnOutput = $true
        }
        @{
            Name        = "Lint PowerShell Files (Test-VariableNamingConvention)"
            ScriptBlock = {
                Test-VariableNamingConvention -Path "scripts"
            }
        }
        @{
            Name      = "Lint Python Files (Ruff)"
            Tool      = "ruff"
            Arguments = $RuffLintArguments
        }
    )

    # ---------------
    # Check Execution
    # ---------------

    foreach ($Check in ($FormatChecks + $LintChecks)) {
        Invoke-CheckStep @Check
    }

} finally {
    Pop-Location
}

Write-Section "Final Result"

if ($script:FailureCount -eq 0) {
    Write-Host "All checks passed. Code changes are ready to be pushed." -ForegroundColor Green
    $ExitCode = 0
} else {
    Write-Host "$script:FailureCount step(s) failed. See the output above for details." -ForegroundColor Red
    $ExitCode = 1
}

Write-Host ""

exit $ExitCode