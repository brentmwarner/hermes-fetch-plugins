$ErrorActionPreference = "Stop"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    Write-Error "This check is for Windows."
    exit 1
}

$setup = Join-Path (Split-Path -Parent $PSScriptRoot) "computer_setup.py"
$setupArguments = @(
    $setup,
    "--target", "tcp://127.0.0.1:5900",
    "--kind", "Windows desktop",
    "--ask-vnc-password",
    "--wait-seconds", "15"
)
$python = Get-Command python -ErrorAction SilentlyContinue
$pythonLauncher = Get-Command py -ErrorAction SilentlyContinue
if ($python) {
    & $python.Source @setupArguments
} elseif ($pythonLauncher) {
    & $pythonLauncher.Source -3 @setupArguments
} else {
    Write-Error "Python is required to configure the Fetch computer bridge."
    exit 1
}
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
Write-Output "Fetch saved the dedicated VNC password on this PC. The iPhone will connect without a transport credential prompt."
