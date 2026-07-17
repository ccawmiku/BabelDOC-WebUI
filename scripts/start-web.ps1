param(
    [switch]$NoBrowser,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$utf8Encoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = $utf8Encoding
$OutputEncoding = $utf8Encoding
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location -LiteralPath $projectRoot
$env:PYTHONUTF8 = "1"

function Test-CompatiblePython {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    $probe = @"
import struct, sys
bits = struct.calcsize('P') * 8
supported = (3, 10) <= sys.version_info[:2] < (3, 14) and bits == 64
if not supported:
    raise SystemExit(2)
version = chr(46).join(map(str, sys.version_info[:3]))
print(sys.executable, version, bits, sep=chr(124))
"@
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $result = & $Path -c $probe 2>&1
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($env:BABELDOC_DIAGNOSTIC_LOG) {
        $diagnostic = ($result | Out-String).Trim()
        Write-Host "[python-probe] path=$Path exit=$exitCode output=$diagnostic"
    }
    if ($exitCode -ne 0 -or -not $result) {
        return $null
    }
    $parts = ($result | Select-Object -Last 1) -split "\|"
    if ($parts.Count -ne 3) {
        return $null
    }
    return [pscustomobject]@{
        Path = $parts[0]
        Version = $parts[1]
        Architecture = $parts[2]
    }
}

function Resolve-PyLauncherPython {
    param([Parameter(Mandatory = $true)][string]$Selector)

    if (-not (Get-Command py.exe -ErrorAction SilentlyContinue)) {
        return $null
    }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $resolved = & py.exe $Selector -c "import sys; print(sys.executable)" 2>$null
    $exitCode = $LASTEXITCODE
    $ErrorActionPreference = $previousPreference
    if ($exitCode -ne 0 -or -not $resolved) {
        return $null
    }
    return ($resolved | Select-Object -Last 1).Trim()
}

function Add-PythonCandidate {
    param(
        [System.Collections.Generic.List[string]]$List,
        [string]$Path
    )
    if ($Path -and -not $List.Contains($Path)) {
        $List.Add($Path)
    }
}

$venvCandidates = @(
    (Join-Path $projectRoot ".venv\Scripts\python.exe"),
    (Join-Path $projectRoot ".venv-web\Scripts\python.exe")
)
foreach ($venvPython in $venvCandidates) {
    $venvInfo = Test-CompatiblePython -Path $venvPython
    if ($venvInfo) {
        $selectedVenvPython = $venvInfo.Path
        break
    }
}

if (-not $selectedVenvPython) {
    $pythonCandidates = [System.Collections.Generic.List[string]]::new()
    Add-PythonCandidate -List $pythonCandidates -Path $env:BABELDOC_PYTHON

    foreach ($selector in @("-3.12", "-3.13", "-3.11", "-3.10", "-3")) {
        Add-PythonCandidate -List $pythonCandidates -Path (
            Resolve-PyLauncherPython -Selector $selector
        )
    }
    foreach ($commandName in @("python.exe", "python3.exe")) {
        $command = Get-Command $commandName -ErrorAction SilentlyContinue
        if ($command) {
            Add-PythonCandidate -List $pythonCandidates -Path $command.Source
        }
    }

    $commonPatterns = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python*\python.exe"),
        (Join-Path $env:ProgramFiles "Python*\python.exe"),
        (Join-Path $env:USERPROFILE "miniconda3\python.exe"),
        (Join-Path $env:USERPROFILE "anaconda3\python.exe"),
        (Join-Path $env:ProgramData "miniconda3\python.exe"),
        (Join-Path $env:ProgramData "anaconda3\python.exe")
    )
    foreach ($pattern in $commonPatterns) {
        Get-ChildItem -Path $pattern -File -ErrorAction SilentlyContinue | ForEach-Object {
            Add-PythonCandidate -List $pythonCandidates -Path $_.FullName
        }
    }

    $basePython = $null
    foreach ($candidate in $pythonCandidates) {
        $candidateInfo = Test-CompatiblePython -Path $candidate
        if ($candidateInfo) {
            $basePython = $candidateInfo
            break
        }
    }
    if (-not $basePython) {
        throw "No compatible Python found. Install 64-bit Python 3.10-3.13, or set BABELDOC_PYTHON to python.exe."
    }

    $venvDir = Join-Path $projectRoot ".venv"
    if (Test-Path -LiteralPath $venvDir) {
        $venvDir = Join-Path $projectRoot ".venv-web"
    }
    Write-Output "[BabelDOC] Using Python $($basePython.Version) $($basePython.Architecture)-bit: $($basePython.Path)"
    Write-Output "[BabelDOC] Creating web environment: $venvDir"
    & $basePython.Path -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create the Python virtual environment."
    }
    $selectedVenvPython = Join-Path $venvDir "Scripts\python.exe"
}

$selectedInfo = Test-CompatiblePython -Path $selectedVenvPython
if (-not $selectedInfo) {
    throw "The selected virtual environment is not compatible with BabelDOC."
}
Write-Output "[BabelDOC] Selected Python $($selectedInfo.Version) $($selectedInfo.Architecture)-bit: $($selectedInfo.Path)"
if ($CheckOnly) {
    exit 0
}

$previousPreference = $ErrorActionPreference
$ErrorActionPreference = "Continue"
& $selectedVenvPython -c "import fastapi, uvicorn, babeldoc.format.pdf.high_level" 2>$null
$dependenciesReady = $LASTEXITCODE -eq 0
$ErrorActionPreference = $previousPreference
if (-not $dependenciesReady) {
    Write-Output "[BabelDOC] Installing dependencies for the first run..."
    & $selectedVenvPython -m pip install ".[web]"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install BabelDOC web dependencies."
    }
}

$serverArguments = @("-m", "babeldoc.webui.app")
if ($NoBrowser) {
    $serverArguments += "--no-browser"
}
Write-Output "[BabelDOC] Starting at http://127.0.0.1:8787"
& $selectedVenvPython @serverArguments
exit $LASTEXITCODE
