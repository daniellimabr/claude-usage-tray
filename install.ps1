# Creates the virtual environment, installs dependencies, adds a shortcut to the
# Windows Startup folder and starts the app.
$ErrorActionPreference = 'Stop'
$app = $PSScriptRoot

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    throw 'Python 3.10+ was not found on PATH. Install it from https://www.python.org/downloads/ and try again.'
}

if (-not (Test-Path "$app\.venv")) { python -m venv "$app\.venv" }
& "$app\.venv\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check -r "$app\requirements.txt"

$startup = [Environment]::GetFolderPath('Startup')
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut("$startup\Claude Usage Tray.lnk")
$lnk.TargetPath = "$app\.venv\Scripts\pythonw.exe"
$lnk.Arguments = "`"$app\claude_usage_tray.py`""
$lnk.WorkingDirectory = $app
$lnk.Description = 'Claude plan usage and Claude Code context in the system tray'
$lnk.Save()

Start-Process -FilePath "$app\.venv\Scripts\pythonw.exe" -ArgumentList "`"$app\claude_usage_tray.py`"" -WorkingDirectory $app
Write-Host 'Installed and running. It will start automatically when you sign in to Windows.'
Write-Host 'If you do not see the icon, check the hidden icons (^) in the taskbar and drag it out.'
