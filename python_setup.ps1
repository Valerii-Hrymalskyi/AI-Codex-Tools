<#
.SYNOPSIS
    Installs the portable Python that every tool in this repository runs on, plus its packages.

.DESCRIPTION
    Run once after cloning, and again whenever $Version or python-requirements.txt changes.

    1. Downloads the official NuGet package "python" of the pinned version from api.nuget.org.
    2. Refuses it unless its SHA-256 matches the one pinned below.
    3. Unpacks it into Utils\Bin\Windows\Python3 (not in git).
    4. Installs the exact package set from python-requirements.txt (nothing, if it lists none).

    Idempotent: when the right version is already there, only step 4 runs, and pip finds
    everything satisfied. A different version is replaced; a Python that is in use (a running
    session, hook or server started from it) cannot be, and the script says so - naming who
    holds it - instead of leaving a half-deleted install behind.

    The same script is used by AI-Claude-Tools and AI-Codex-Tools; keep the copies identical.

.PARAMETER Destination
    Where to install. Default: Utils\Bin\Windows\Python3 next to this script.

.PARAMETER Force
    Reinstall even when the pinned version is already there.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\python_setup.ps1
#>
[CmdletBinding()]
param(
    [string]$Destination,
    [switch]$Force
)

# Windows PowerShell 5.1 leaves $PSScriptRoot empty inside param() defaults, so the
# script's own folder is resolved here, in the body.
$here = Split-Path -Parent $MyInvocation.MyCommand.Definition
if (-not $Destination) { $Destination = Join-Path $here 'Utils\Bin\Windows\Python3' }

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1: the progress bar makes Invoke-WebRequest many times slower.
$ProgressPreference = 'SilentlyContinue'

# --- Pinned --------------------------------------------------------------------------------
# To upgrade: change $Version, download the .nupkg, put its SHA-256 here (Get-FileHash), and
# check it against the SHA-512 NuGet publishes in the package's catalog entry.
$Version = '3.15.2'
$Sha256  = '294F5ED7AB97148FCBB57B8C06BA428F6D282C2BBAA266DBE3137284497998F0'
$Url     = "https://api.nuget.org/v3-flatcontainer/python/$Version/python.$Version.nupkg"
$Requirements = Join-Path $here 'python-requirements.txt'
# --------------------------------------------------------------------------------------------

$Destination = [IO.Path]::GetFullPath($Destination)
$python = Join-Path $Destination 'python.exe'

function Get-InstalledVersion {
    if (-not (Test-Path $python)) { return $null }
    $v = & $python -c "import platform; print(platform.python_version())" 2>$null
    if ($LASTEXITCODE -ne 0) { return $null }
    return "$v".Trim()
}

# Leftover from a replacement whose old copy was still locked at the time.
if (Test-Path "$Destination.old") { Remove-Item -Recurse -Force "$Destination.old" -ErrorAction SilentlyContinue }

$installed = Get-InstalledVersion
if ($installed -eq $Version -and -not $Force) {
    Write-Host "Python $Version is already in $Destination"
}
else {
    if ($installed) {
        # Refuse up front rather than swap a Python out from under a running process.
        $prefix = $Destination.TrimEnd('\') + '\'
        $busy = @(Get-CimInstance Win32_Process | Where-Object {
            $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) })
        if ($busy.Count) {
            $lines = foreach ($p in $busy) {
                "  PID $($p.ProcessId)  $($p.Name)  $($p.ExecutablePath)"
                "      command:    $($p.CommandLine)"
                # The python.exe itself says little; what to close is whoever started it.
                $parentId = $p.ParentProcessId
                for ($depth = 0; $depth -lt 4 -and $parentId; $depth++) {
                    $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$parentId" -ErrorAction SilentlyContinue
                    if (-not $parent) { break }
                    $label = if ($depth -eq 0) { 'started by:' } else { '      <- by:' }
                    "      $label PID $($parent.ProcessId)  $($parent.Name)  $($parent.ExecutablePath)"
                    $parentId = $parent.ParentProcessId
                }
            }
            # Printed once and plainly: a throw would repeat it inside PowerShell's error record.
            Write-Host "Cannot replace $Destination - it is in use:" -ForegroundColor Red
            $lines | ForEach-Object { Write-Host $_ }
            Write-Host "Close them (see 'started by' above: sessions, hooks or servers using this Python) and run again." -ForegroundColor Red
            exit 1
        }
        Write-Host "Replacing Python $installed with $Version"
    }
    else { Write-Host "Installing Python $Version into $Destination" }

    $parent = Split-Path $Destination -Parent
    New-Item -ItemType Directory -Force $parent | Out-Null

    # Staging next to the destination: same volume, so the final step is a rename, not a copy.
    $staging = "$Destination.setup"
    if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
    New-Item -ItemType Directory $staging | Out-Null
    try {
        $zip = Join-Path $staging "python.$Version.zip"   # Expand-Archive insists on .zip
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Write-Host "Downloading $Url"
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $zip

        $actual = (Get-FileHash -Algorithm SHA256 -Path $zip).Hash
        if ($actual -ne $Sha256) {
            throw "SHA-256 mismatch for python.$Version.nupkg: expected $Sha256, got $actual. Not installing."
        }
        Write-Host "SHA-256 verified"

        Expand-Archive -Path $zip -DestinationPath (Join-Path $staging 'pkg')

        # Order matters: the new copy goes into place BEFORE the old one is deleted, so there is
        # a working Python at every moment. Windows lets a folder be renamed while an exe in it
        # runs, but not deleted - deleting first once left no Python at all.
        $old = "$Destination.old"
        if (Test-Path $Destination) {
            if (Test-Path $old) { Remove-Item -Recurse -Force $old }
            Rename-Item -Path $Destination -NewName (Split-Path $old -Leaf)
        }
        try { Move-Item -Path (Join-Path $staging 'pkg\tools') -Destination $Destination }
        catch {
            if ((Test-Path $old) -and -not (Test-Path $Destination)) { Rename-Item $old (Split-Path $Destination -Leaf) }
            throw
        }
        if (Test-Path $old) {
            try { Remove-Item -Recurse -Force $old }
            catch { Write-Warning "The new Python is in place, but the old copy could not be deleted yet: $old. The next run removes it." }
        }
    }
    finally {
        Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
    }

    $installed = Get-InstalledVersion
    if ($installed -ne $Version) { throw "Installed, but python.exe reports '$installed' instead of $Version" }
    Write-Host "Installed Python $Version"
}

# pip refuses a requirements file without requirements, so a list of none is skipped.
$wanted = @(Get-Content $Requirements -ErrorAction SilentlyContinue |
    Where-Object { $_.Trim() -and -not $_.Trim().StartsWith('#') })
if ($wanted.Count -eq 0) {
    Write-Host "No packages to install: $(Split-Path $Requirements -Leaf) lists none"
}
else {
    Write-Host "Installing packages from $(Split-Path $Requirements -Leaf)"
    & $python -m pip install --disable-pip-version-check --no-warn-script-location -q -r $Requirements
    if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }
}

Write-Host "Ready: $python"
