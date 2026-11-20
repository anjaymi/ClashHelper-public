$ErrorActionPreference = 'Stop'

try {
    $clashRoot = Split-Path -Parent $PSScriptRoot
    $clashPython = Join-Path $clashRoot '.venv\Scripts\python.exe'
    $clashPythonw = Join-Path $clashRoot '.venv\Scripts\pythonw.exe'
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        throw 'GitHub CLI is missing. Install it and run gh auth login first.'
    }
    if (-not (Test-Path -LiteralPath $clashPython)) {
        & py -3.12 -m venv (Join-Path $clashRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
    }
    $clashCheck = Start-Process -FilePath $clashPython -ArgumentList @('-c', '"import yaml"') -WindowStyle Hidden -Wait -PassThru
    if ($clashCheck.ExitCode -ne 0) {
        $clashRequirements = Join-Path $clashRoot 'requirements.txt'
        $clashInstall = Start-Process -FilePath $clashPython -ArgumentList @('-m', 'pip', 'install', '-r', '"' + $clashRequirements + '"') -WindowStyle Hidden -Wait -PassThru
        if ($clashInstall.ExitCode -ne 0) { throw 'Dependency installation failed. Check your network.' }
    }
    $clashScript = Join-Path $clashRoot 'subscription_manager.py'
    Start-Process -FilePath $clashPythonw -WorkingDirectory $clashRoot -ArgumentList @('"' + $clashScript + '"') -WindowStyle Hidden
} catch {
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show($_.Exception.Message, 'ClashHelper', 'OK', 'Error') | Out-Null
}
