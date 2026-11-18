<#
    freeze_requirements.ps1 - Wave AI (Tier 22)

    Produces the TRUE, authoritative requirements.lock on a real Windows
    machine. requirements.lock as shipped in this repo was resolved in a
    Linux sandbox (see that file's own header comment) and is explicitly
    labeled reference-only for its transitive-dependency section, because
    five of this project's own Windows-only direct dependencies
    (pywin32, pycaw, comtypes, winsdk, keyboard) cannot even be installed
    on Linux, let alone have their own sub-dependencies resolved there.

    Run this on Windows, with Python 3.11, from the project root:

        .\scripts\freeze_requirements.ps1

    What it does:
      1. Creates a clean, throwaway virtual environment (.freeze_venv),
         separate from any venv you actually develop in - this script
         installs EVERYTHING in requirements.txt +
         requirements-voice-local.txt, including every optional extra,
         which you may not want polluting your regular dev venv.
      2. Installs every pin from both requirement files into it.
      3. Runs `pip freeze` and writes the result to requirements.lock,
         OVERWRITING the Linux-sandbox reference lock that ships in this
         repo with the real, Windows-resolved transitive closure.
      4. Deletes the throwaway venv.

    This does NOT touch your own dev venv (wave_env or similar) and does
    NOT install anything into it - only into the disposable one this
    script creates and removes.
#>

param(
    [switch]$KeepVenv  # pass -KeepVenv to inspect .freeze_venv afterward instead of deleting it
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

if (-not (Test-Path "requirements.txt")) {
    Write-Error "requirements.txt not found - run this from the project root (or leave scripts/ where it is)."
    exit 1
}

$venvDir = ".freeze_venv"

if (Test-Path $venvDir) {
    Write-Host "Removing pre-existing $venvDir ..."
    Remove-Item -Recurse -Force $venvDir
}

Write-Host "Creating a clean venv at $venvDir ..."
python -m venv $venvDir

$venvPython = Join-Path $venvDir "Scripts\python.exe"
$venvPip = Join-Path $venvDir "Scripts\pip.exe"

Write-Host "Upgrading pip in the throwaway venv ..."
& $venvPython -m pip install --upgrade pip --quiet

Write-Host "Installing requirements.txt + requirements-voice-local.txt (every optional extra included) ..."
$reqFiles = @("requirements.txt")
if (Test-Path "requirements-voice-local.txt") {
    $reqFiles += "requirements-voice-local.txt"
}
& $venvPip install @($reqFiles | ForEach-Object { "-r"; $_ })

Write-Host "Freezing the resolved environment ..."
$freezeOutput = & $venvPip freeze

# Keep requirements.lock's own header/commentary (Section 1's authoritative
# pins + the explanation of what this file is) but replace the Section-2
# transitive block with the real Windows-resolved freeze. Simplest honest
# approach: write the freeze straight out as the new authoritative lock,
# with a short header noting it's the real Windows resolution, not the
# sandbox-reference one.
$header = @"
# requirements.lock - Wave AI dependency lock (Tier 22).
#
# Generated on a real Windows machine by scripts/freeze_requirements.ps1
# on $(Get-Date -Format "yyyy-MM-dd"), from Python $(& $venvPython -c "import platform; print(platform.python_version())").
# This REPLACES the Linux-sandbox reference lock this repo originally
# shipped with - everything below is a genuine Windows pip freeze of
# requirements.txt + requirements-voice-local.txt installed together,
# including every optional extra. Re-run this script whenever either
# requirements file changes.
"@

$outPath = Join-Path $repoRoot "requirements.lock"
$header + "`n`n" + ($freezeOutput -join "`n") + "`n" | Set-Content -Path $outPath -Encoding utf8

Write-Host "Wrote $outPath ($(($freezeOutput | Measure-Object).Count) packages)."

if (-not $KeepVenv) {
    Write-Host "Cleaning up $venvDir ..."
    Remove-Item -Recurse -Force $venvDir
} else {
    Write-Host "Left $venvDir in place (-KeepVenv was passed)."
}

Write-Host "Done."
