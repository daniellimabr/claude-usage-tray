# Stops the app and removes the Startup shortcut. Delete this folder afterwards to remove everything.
$app = $PSScriptRoot

Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
    Where-Object { $_.CommandLine -like '*claude_usage_tray.py*' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }

$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'Claude Usage Tray.lnk'
if (Test-Path $lnk) { Remove-Item $lnk }

Write-Host "Uninstalled. You can now delete $app"
