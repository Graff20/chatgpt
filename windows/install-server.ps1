# Run in an elevated PowerShell after unpacking the complete CehServer distribution.
param([string]$InstallDir="$env:ProgramData\CehServer",[string]$DataDir="$env:ProgramData\CehData",[string]$PublicUrl="",[string]$DatabaseCopy="")
$ErrorActionPreference='Stop'
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Запустите PowerShell от имени администратора' }
if (!(Test-Path "$PSScriptRoot\..\dist\CehServer\CehServer.exe")) { throw 'Рядом нужен полный дистрибутив dist/CehServer' }
New-Item -ItemType Directory -Force $InstallDir,$DataDir | Out-Null
if (!$DatabaseCopy -and !(Test-Path "$DataDir\ceh.sqlite3")) {
 Add-Type -AssemblyName System.Windows.Forms
 $pick=New-Object System.Windows.Forms.OpenFileDialog
 $pick.Title='Выберите резервную копию, скачанную из Цех на рабочем ПК'
 $pick.Filter='Резервная копия SQLite|*.sqlite3;*.db|Все файлы|*.*'
 if ($pick.ShowDialog() -ne 'OK') { throw 'Копия базы не выбрана. Установка остановлена без замены данных' }
 $DatabaseCopy=$pick.FileName
}
& icacls $DataDir /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Не удалось защитить папку базы' }
if ($DatabaseCopy) {
 if (Test-Path ($DatabaseCopy+'-wal')) { throw 'Выбрана рабочая база с WAL. Сначала скачайте резервную копию из программы' }
 if (Test-Path "$DataDir\ceh.sqlite3") { throw 'База уже существует. Автоматическая замена запрещена' }
 Copy-Item -LiteralPath $DatabaseCopy -Destination "$DataDir\ceh.sqlite3"
}
Copy-Item "$PSScriptRoot\..\dist\CehServer\*" $InstallDir -Recurse -Force
if (!(Test-Path "$InstallDir\CehAdmin.exe")) { throw 'Нужен CehAdmin.exe из сборки Windows' }
& "$InstallDir\CehAdmin.exe" init-admin --data $DataDir
if ($LASTEXITCODE -ne 0) { throw 'Создание администратора не завершено; автозапуск не установлен' }
$arguments="--secure --host 127.0.0.1 --port 8765 --data `"$DataDir`" --log-file `"$DataDir\logs\server.log`""
if ($PublicUrl) { $arguments += " --public-url `"$PublicUrl`"" }
$action=New-ScheduledTaskAction -Execute "$InstallDir\CehServer.exe" -Argument $arguments -WorkingDirectory $InstallDir
$trigger=New-ScheduledTaskTrigger -AtStartup
$settings=New-ScheduledTaskSettingsSet -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName 'CehServer' -Action $action -Trigger $trigger -Settings $settings -User 'SYSTEM' -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName 'CehServer'
Write-Host 'Автозапуск установлен. Сервер слушает только 127.0.0.1:8765. Для удалённого доступа настройте защищённый HTTPS/VPN по инструкции.'

Start-Sleep -Seconds 2
Start-Process 'http://127.0.0.1:8765/login'
