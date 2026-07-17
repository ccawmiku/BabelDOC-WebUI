$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dist = Join-Path $projectRoot "dist\windows"
$work = Join-Path $projectRoot ".tmp\pyinstaller"

$python = $null
foreach ($relativePath in @(".venv\Scripts\python.exe", ".venv-web\Scripts\python.exe")) {
    $candidate = Join-Path $projectRoot $relativePath
    if (Test-Path -LiteralPath $candidate -PathType Leaf) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    throw "No project virtual environment found. Run start-web.bat once before building the launcher."
}

& $python -m pip install --disable-pip-version-check "pyinstaller>=6.0,<7"

& $python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --console `
    --hide-console "hide-early" `
    --name "BabelDOC-Web" `
    --distpath $dist `
    --workpath $work `
    --specpath $work `
    (Join-Path $projectRoot "tools\windows_launcher.py")

$exe = Join-Path $dist "BabelDOC-Web.exe"
if (-not (Test-Path -LiteralPath $exe)) {
    throw "PyInstaller completed without producing $exe"
}

Write-Output $exe
