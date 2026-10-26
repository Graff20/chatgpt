param([string]$InstallDir="$env:ProgramData\CehServer",[string]$DataDir="$env:ProgramData\CehData")
$ErrorActionPreference='Stop'
if (!(Test-Path "$PSScriptRoot\..\dist\CehServer\CehServer.exe")) { throw 'Распакуйте полную новую сборку перед обновлением' }
$backup=Join-Path $DataDir ('updates\'+(Get-Date -Format 'yyyyMMdd-HHmmss'))
New-Item -ItemType Directory -Force "$backup\program" | Out-Null
New-Item -ItemType File -Force "$DataDir\maintenance.flag" | Out-Null
try {
 # SQLite backup API includes committed WAL pages while the service is still running.
 & "$InstallDir\CehAdmin.exe" backup --data $DataDir --output "$backup\ceh.sqlite3"
 if ($LASTEXITCODE -ne 0) { throw 'Согласованная копия базы не создана' }
 Stop-ScheduledTask -TaskName 'CehServer'
 Start-Sleep -Seconds 2
 if (Get-Process CehServer -ErrorAction SilentlyContinue) { throw 'Сервер не остановлен. Не заменяйте работающие файлы' }
 Copy-Item "$InstallDir\*" "$backup\program" -Recurse -Force
 Copy-Item "$PSScriptRoot\..\dist\CehServer\*" $InstallDir -Recurse -Force
 Start-ScheduledTask -TaskName 'CehServer'
 $ready=$false
 for ($attempt=0;$attempt -lt 20;$attempt++) {
  try { $status=Invoke-RestMethod http://127.0.0.1:8765/api/auth/me; if ($status.enabled) { $ready=$true;break } } catch {}
  Start-Sleep -Seconds 1
 }
 if (!$ready) { throw 'Новая версия не прошла проверку запуска. Режим обслуживания сохранён' }
 Remove-Item "$DataDir\maintenance.flag"
 Write-Host "Сервер обновлён. Копия программы и базы: $backup"
} catch { Write-Host "Обновление не завершено. Данные не удалялись. Не снимайте режим обслуживания до проверки. Копия: $backup";throw }
