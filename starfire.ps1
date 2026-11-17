#
# starfire.ps1:   BUILD AND RUN LAUNCHER (WINDOWS)
# AUTHOR:         DANIEL DESAI
# UPDATED:        2026-11-17
# VERSION:        4.6.0 (BELLATRIX)

# The Windows counterpart of starfi.sh: builds starfire.exe (via PyInstaller) into a dist folder, and/or runs it.
# The default dist folder is dist\. Needs Python 3 (python.org or winget; not the Microsoft Store stub).
#
# Flags:
#   (none)            Run only, don't build
#   -Build            Build only, don't run
#   -All              Build and run
#   -Clean            Delete the dist folder first; on its own it only deletes it
#   -New NAME         Build into a new dist folder called NAME; needs -Build or -All
# starfi.sh's --install-desktop has no equivalent: pin dist\starfire.exe to Start yourself.
#
# Typical usages (from PowerShell; add -ExecutionPolicy Bypass if scripts are blocked):
#   * Run from an existing dist:               .\starfire.ps1
#   * Build in an existing dist:               .\starfire.ps1 -Clean -Build
#   * Rebuild and run in an existing dist:     .\starfire.ps1 -Clean -All
#   * Delete the dist folder:                  .\starfire.ps1 -Clean
#   * Build as a new dist:                     .\starfire.ps1 -Build -New dist2
#
# Output: a dist folder containing starfire.exe and (if placed next to this script) jira_config.json.
# Modules already in the dist folder are unaffected, unless -Clean removes the folder.

param([switch]$Build, [switch]$All, [switch]$Clean, [string]$New = "")

$ErrorActionPreference = "Stop"
function Fail($msg, $code = 1) { [Console]::Error.WriteLine("ERROR: $msg"); exit $code }
Set-Location $PSScriptRoot

$Dist = "dist"
if ($All) { $Build = $true }
if ($New) {
    if (-not $Build) { Fail "New dist requires build" }
    if ($New -in @(".", "..") -or $New -match '[\\/:]' -or $New.StartsWith("-")) {
        Fail "dist name must be a plain folder name, not '$New'" 2
    }
    if (Test-Path $New) { Fail "$New already exists. Pick another name, or build into it without -New." }
    $Dist = $New
    $Clean = $false
}

# -Clean on its own just deletes the dist folder.
if ($Clean -and -not $Build) {
    if (Test-Path $Dist) { Remove-Item -Recurse -Force $Dist }
    Write-Host "Cleaned $Dist\"
    exit 0
}

if ($Build) {
    $missing = @("starfire.py", "static\starfire.css", "static\test.html", "static\campaign.html", "static\system.html") |
        Where-Object { -not (Test-Path $_) }
    if ($missing) { Fail ("missing required file(s) next to starfire.ps1: " + ($missing -join ", ")) }
    if (-not (Test-Path signet.py)) { Write-Warning "no signet.py next to starfire.ps1 -- building without Signet, so Signals are off." }

    # python on PATH, skipping the Microsoft Store stub; else the py launcher
    $py = Get-Command python, py -ErrorAction SilentlyContinue | Where-Object { $_.Source -notlike "*WindowsApps*" } | Select-Object -First 1
    if (-not $py) { Fail "Python 3 not found on PATH (install it from python.org or with winget)" }
    $py = $py.Source

    if ($Clean -and (Test-Path $Dist)) { Remove-Item -Recurse -Force $Dist; Write-Host "Cleaned $Dist\" }

    & $py -m pip install --quiet pyinstaller python-docx numpy
    if ($LASTEXITCODE) { exit $LASTEXITCODE }

    $work = Join-Path ([IO.Path]::GetTempPath()) ("starfire-build-" + [guid]::NewGuid())
    New-Item -ItemType Directory -Force $Dist | Out-Null
    try {
        # PyInstaller on Windows takes SOURCE;DEST (';', as the source path has a drive-letter ':')
        & $py -m PyInstaller --onefile --name starfire `
            --add-data "$PSScriptRoot\static;static" `
            --distpath "$PSScriptRoot\$Dist" `
            --workpath $work `
            --specpath $work `
            starfire.py
        if ($LASTEXITCODE) { exit $LASTEXITCODE }
    } finally {
        Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
    }

    # Jira is optional: copied in only if you have a config next to this script.
    if (Test-Path jira_config.json) {
        Copy-Item jira_config.json "$Dist\jira_config.json"
        $note = "copied jira_config.json"
    } else {
        $note = "no jira_config.json here -- Jira ticketing stays off"
    }
    Write-Host "`nBuilt: $Dist\starfire.exe ($note)"
}

# The default action, and the second half of -All. Runs from the dist folder.
if (-not $Build -or $All) {
    if (-not (Test-Path "$Dist\starfire.exe")) { Fail "$Dist\starfire.exe not found. Build first: .\starfire.ps1 -Build" }
    $c = New-Object Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", 5791); $busy = $true } catch { $busy = $false } finally { $c.Close() }
    if ($busy) { Fail "port 5791 is already in use (Starfire may already be running)." }
    Set-Location $Dist
    & .\starfire.exe
    exit $LASTEXITCODE
}
