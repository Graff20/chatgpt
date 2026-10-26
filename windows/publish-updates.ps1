param([string]$DataDir="$env:ProgramData\CehData",[Parameter(Mandatory=$true)][string]$PublicUrl,[string]$AndroidFile="",[string]$WindowsFile="")
$ErrorActionPreference='Stop'
$uri=[Uri]$PublicUrl
if (!$uri.IsAbsoluteUri -or $uri.Scheme -ne 'https' -or $uri.AbsolutePath -ne '/' -or $uri.Query -or $uri.UserInfo) { throw 'Нужен корневой HTTPS-адрес сервера' }
$PublicUrl=$PublicUrl.TrimEnd('/')
$manifest=@{android=@{latest=1;minimum=1};windows=@{latest='0.6.0';minimum='0.6.0'}}
if (Test-Path "$DataDir\updates.json") {
 $old=Get-Content "$DataDir\updates.json" -Raw | ConvertFrom-Json
 foreach ($key in @('android','windows')) { if ($old.$key) { $entry=@{}; $old.$key.PSObject.Properties | ForEach-Object {$entry[$_.Name]=$_.Value};$manifest[$key]=$entry } }
}
New-Item -ItemType Directory -Force "$DataDir\updates" | Out-Null
if ($AndroidFile) {
 $meta=Get-Content ($AndroidFile+'.json') -Raw | ConvertFrom-Json
 $hash=(Get-FileHash -LiteralPath $AndroidFile -Algorithm SHA256).Hash.ToLower()
 if ($meta.package -ne 'ru.ceh.mobile' -or $hash -ne $meta.sha256 -or [int]$meta.versionCode -lt [int]$manifest.android.latest) { throw 'Проверьте APK, версию и контрольную сумму' }
 $name='ceh-android-'+[int]$meta.versionCode+'.apk'
 if ((Test-Path "$DataDir\updates\$name") -and (Get-FileHash "$DataDir\updates\$name" -Algorithm SHA256).Hash.ToLower() -ne $hash) { throw 'Номер Android-сборки уже опубликован. Увеличьте versionCode' }
 Copy-Item -LiteralPath $AndroidFile -Destination "$DataDir\updates\$name" -Force
 $manifest.android=@{latest=[int]$meta.versionCode;minimum=$manifest.android.minimum;url="$PublicUrl/downloads/$name";sha256=$hash}
}
if ($WindowsFile) {
 $meta=Get-Content ($WindowsFile+'.json') -Raw | ConvertFrom-Json
 $hash=(Get-FileHash -LiteralPath $WindowsFile -Algorithm SHA256).Hash.ToLower()
 if ($meta.version -notmatch '^\d+\.\d+\.\d+$' -or $hash -ne $meta.sha256 -or [version]$meta.version -lt [version]$manifest.windows.latest) { throw 'Проверьте установщик и его контрольную сумму' }
 $name='CehClientSetup-'+$meta.version+'.exe'
 if ((Test-Path "$DataDir\updates\$name") -and (Get-FileHash "$DataDir\updates\$name" -Algorithm SHA256).Hash.ToLower() -ne $hash) { throw 'Версия Windows-клиента уже опубликована. Увеличьте версию клиента' }
 Copy-Item -LiteralPath $WindowsFile -Destination "$DataDir\updates\$name" -Force
 $manifest.windows=@{latest=$meta.version;minimum=$manifest.windows.minimum;url="$PublicUrl/downloads/$name";sha256=$hash}
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content "$DataDir\updates.json.tmp" -Encoding UTF8
Move-Item "$DataDir\updates.json.tmp" "$DataDir\updates.json" -Force
Write-Host 'Пакеты опубликованы. Минимальные версии не повышены. Проверьте обновление сначала на одном устройстве.'
