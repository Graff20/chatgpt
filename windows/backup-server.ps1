param([Parameter(Mandatory=$true)][string]$Destination,[string]$InstallDir="$env:ProgramData\CehServer",[string]$DataDir="$env:ProgramData\CehData")
$ErrorActionPreference='Stop'
New-Item -ItemType Directory -Force $Destination | Out-Null
$target=Join-Path $Destination ('ceh-daily-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.sqlite3')
& "$InstallDir\CehAdmin.exe" backup --data $DataDir --output $target
if ($LASTEXITCODE -ne 0) { throw 'Резервная копия не создана' }
Get-ChildItem $Destination -Filter 'ceh-daily-*.sqlite3' -File | Where-Object LastWriteTime -lt (Get-Date).AddDays(-30) | Remove-Item
Write-Host "Копия создана: $target"
