@echo off
setlocal
set "BATCH_APACHE_SCRIPT=%~f0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -Command "$source=[IO.File]::ReadAllText($env:BATCH_APACHE_SCRIPT,[Text.Encoding]::UTF8); $marker=[char]10+'# POWERSHELL-BEGIN'; & ([scriptblock]::Create($source.Substring($source.IndexOf($marker)+$marker.Length)))"
set "batchConfigExitCode=%errorlevel%"
echo.
if /I not "%~1"=="--no-pause" pause
exit /b %batchConfigExitCode%
# POWERSHELL-BEGIN
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Get-FileDigest([string]$FilePath) {
    if (Test-Path -LiteralPath $FilePath -PathType Leaf) {
        $sha256 = [Security.Cryptography.SHA256]::Create()
        $stream = [IO.File]::OpenRead($FilePath)
        try {
            return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace('-', '')
        }
        finally {
            $stream.Dispose()
            $sha256.Dispose()
        }
    }
    return $null
}

function Assert-LocalPath([string]$FilePath) {
    $fullPath = [IO.Path]::GetFullPath($FilePath)
    if (-not $fullPath.StartsWith($script:apacheRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Путь вышел за пределы выбранного каталога Apache.'
    }
    $probePath = $fullPath
    while ($probePath -and $probePath.Length -ge $script:apacheRoot.Length) {
        if (Test-Path -LiteralPath $probePath) {
            if ((Get-Item -LiteralPath $probePath -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Ссылки и junction в путях изменяемых файлов не поддерживаются.'
            }
        }
        $probePath = [IO.Path]::GetDirectoryName($probePath)
    }
}

function Invoke-ApacheCheck([switch]$Includes) {
    $checkOptions = @('-d', $script:apacheRoot.Replace('\', '/'), '-f', $script:mainConf.Replace('\', '/'), '-t')
    if ($Includes) { $checkOptions += @('-D', 'DUMP_INCLUDES') }
    $oldPreference = $ErrorActionPreference
    try {
        # Windows PowerShell превращает stderr httpd в ErrorRecord даже для предупреждений.
        $ErrorActionPreference = 'Continue'
        $checkOutput = @(& $script:httpd @checkOptions 2>&1)
        $checkCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $oldPreference }
    $checkText = ($checkOutput | ForEach-Object { $_.ToString() }) -join [Environment]::NewLine
    if ($checkCode -ne 0) { throw "Проверка Apache завершилась ошибкой:`n$checkText" }
    if ($Includes) { return $checkText }
    Write-Host 'Проверка Apache: Syntax OK.'
}

function Save-Snapshot([string]$Directory) {
    Assert-LocalPath $Directory
    [void][IO.Directory]::CreateDirectory($Directory)
    Copy-Item -LiteralPath $script:mainConf -Destination (Join-Path $Directory 'httpd.conf')
    $fragmentExisted = Test-Path -LiteralPath $script:targetConf -PathType Leaf
    if ($fragmentExisted) {
        Copy-Item -LiteralPath $script:targetConf -Destination (Join-Path $Directory 'httpd-batch-proxy.conf')
    }
    return [pscustomobject]@{
        schemaVersion = 1
        apacheRoot = $script:apacheRoot
        createdAt = [DateTime]::UtcNow.ToString('o')
        fragmentExisted = [bool]$fragmentExisted
        mainHash = Get-FileDigest $script:mainConf
        fragmentHash = Get-FileDigest $script:targetConf
        state = 'prepared'
    }
}

function Save-Manifest($Manifest, [string]$Directory) {
    $Manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $Directory 'backup.json') -Encoding UTF8
}

function Restore-Snapshot($Manifest, [string]$Directory) {
    Assert-LocalPath $Directory
    $savedMain = Join-Path $Directory 'httpd.conf'
    $savedFragment = Join-Path $Directory 'httpd-batch-proxy.conf'
    Assert-LocalPath $savedMain
    Assert-LocalPath $savedFragment
    if ($Manifest.apacheRoot -ine $script:apacheRoot -or $Manifest.schemaVersion -ne 1) {
        throw 'Эта резервная копия относится к другому каталогу Apache или формату.'
    }
    if ((Get-FileDigest $savedMain) -ne $Manifest.mainHash -or -not $Manifest.mainHash) {
        throw 'Резервная копия httpd.conf повреждена или отсутствует.'
    }
    if ($Manifest.fragmentExisted -and ((Get-FileDigest $savedFragment) -ne $Manifest.fragmentHash -or -not $Manifest.fragmentHash)) {
        throw 'Резервная копия фрагмента повреждена или отсутствует.'
    }
    Copy-Item -LiteralPath $savedMain -Destination $script:mainConf -Force
    if ($Manifest.fragmentExisted) {
        Copy-Item -LiteralPath $savedFragment -Destination $script:targetConf -Force
    }
    elseif (Test-Path -LiteralPath $script:targetConf -PathType Leaf) {
        # Удаляется только конкретный созданный фрагмент, без рекурсивных операций.
        Remove-Item -LiteralPath $script:targetConf -Force
    }
}

try {
    Write-Host '1 — внести настройки /batch/ с резервным копированием'
    Write-Host '2 — восстановить файлы из резервной копии'
    Write-Host '0 — выход'
    $menuChoice = Read-Host 'Выберите действие'
    if ($menuChoice -eq '1') { $Action = 'Apply' }
    elseif ($menuChoice -eq '2') { $Action = 'Restore' }
    else { exit 0 }
    $enteredRoot = Read-Host 'Путь установки Apache [C:\Apache24]'
    if ([string]::IsNullOrWhiteSpace($enteredRoot)) { $enteredRoot = 'C:\Apache24' }
    $script:apacheRoot = [IO.Path]::GetFullPath($enteredRoot.Trim().Trim('"')).TrimEnd('\')
    $script:httpd = Join-Path $apacheRoot 'bin\httpd.exe'
    $script:mainConf = Join-Path $apacheRoot 'conf\httpd.conf'
    $script:targetConf = Join-Path $apacheRoot 'conf\extra\httpd-batch-proxy.conf'
    $script:backupRoot = Join-Path $apacheRoot 'conf\1c-batch-backups'
    foreach ($checkedPath in @($mainConf, $targetConf, $backupRoot)) { Assert-LocalPath $checkedPath }
    if (-not (Test-Path -LiteralPath $httpd -PathType Leaf) -or -not (Test-Path -LiteralPath $mainConf -PathType Leaf)) {
        throw 'В указанном каталоге не найдены bin\httpd.exe и conf\httpd.conf.'
    }

    if ($Action -eq 'Apply') {
        $sourceConf = Join-Path (Split-Path -Parent $env:BATCH_APACHE_SCRIPT) 'httpd-batch-proxy.conf'
        if (-not (Test-Path -LiteralPath $sourceConf -PathType Leaf)) { throw 'Рядом со скриптом отсутствует httpd-batch-proxy.conf.' }
        Invoke-ApacheCheck
        $includedBefore = Invoke-ApacheCheck -Includes
        $targetUrl = $targetConf.Replace('\', '/')
        if ($includedBefore.IndexOf($targetUrl, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
            (Get-FileDigest $sourceConf) -eq (Get-FileDigest $targetConf)) {
            Write-Host 'Эта конфигурация уже подключена. Изменения не требуются.'
            exit 0
        }
        $mainBytes = [IO.File]::ReadAllBytes($mainConf)
        if ($mainBytes -contains 0) { throw 'Кодировка httpd.conf с нулевыми байтами не поддерживается. Файл не изменён.' }
        $stamp = [DateTime]::Now.ToString('yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
        $backupDir = Join-Path $backupRoot $stamp
        $manifest = Save-Snapshot $backupDir
        Save-Manifest $manifest $backupDir
        Write-Host "Резервная копия: $backupDir"
        try {
            [void][IO.Directory]::CreateDirectory((Split-Path -Parent $targetConf))
            Copy-Item -LiteralPath $sourceConf -Destination $targetConf -Force
            # Если фрагмент уже подхвачен Include с шаблоном, второй Include не добавляется.
            $includedAfter = Invoke-ApacheCheck -Includes
            if ($includedAfter.IndexOf($targetUrl, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
                $addition = [Text.Encoding]::ASCII.GetBytes("`r`n# BEGIN 1c-batch proxy`r`nInclude `"conf/extra/httpd-batch-proxy.conf`"`r`n# END 1c-batch proxy`r`n")
                $newBytes = New-Object byte[] ($mainBytes.Length + $addition.Length)
                [Array]::Copy($mainBytes, 0, $newBytes, 0, $mainBytes.Length)
                [Array]::Copy($addition, 0, $newBytes, $mainBytes.Length, $addition.Length)
                [IO.File]::WriteAllBytes($mainConf, $newBytes)
            }
            Invoke-ApacheCheck
            $manifest.state = 'applied'
            Save-Manifest $manifest $backupDir
        }
        catch {
            $applyFailure = $_
            Restore-Snapshot $manifest $backupDir
            $manifest.state = 'rolled-back'
            Save-Manifest $manifest $backupDir
            Write-Host 'Изменения отменены; исходные файлы восстановлены.'
            throw $applyFailure
        }
        Write-Host 'Настройки /batch/ записаны. Для активации перезапустите свою службу Apache в окне «Службы» Windows.'
    }
    else {
        $copies = @()
        if (Test-Path -LiteralPath $backupRoot -PathType Container) {
            foreach ($directory in (Get-ChildItem -LiteralPath $backupRoot -Directory | Sort-Object Name -Descending)) {
                Assert-LocalPath $directory.FullName
                $manifestPath = Join-Path $directory.FullName 'backup.json'
                Assert-LocalPath $manifestPath
                if (Test-Path -LiteralPath $manifestPath -PathType Leaf) {
                    $saved = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
                    if ($saved.state -in @('applied', 'prepared') -and $saved.apacheRoot -ieq $apacheRoot) {
                        $copies += [pscustomobject]@{ Directory = $directory.FullName; Manifest = $saved }
                    }
                }
            }
        }
        if ($copies.Count -eq 0) { throw 'Резервных копий для восстановления не найдено.' }
        for ($copyIndex = 0; $copyIndex -lt $copies.Count; $copyIndex++) {
            Write-Host ("{0} — {1}" -f ($copyIndex + 1), (Split-Path -Leaf $copies[$copyIndex].Directory))
        }
        Write-Host '0 — выход'
        $selection = Read-Host 'Выберите резервную копию (1 — последняя)'
        if ([string]::IsNullOrWhiteSpace($selection) -or $selection -eq '0') { exit 0 }
        $copyNumber = 0
        if (-not [int]::TryParse($selection, [ref]$copyNumber) -or $copyNumber -lt 1 -or $copyNumber -gt $copies.Count) {
            throw 'Неверный номер резервной копии.'
        }
        $chosen = $copies[$copyNumber - 1]
        # Сохраняем текущее состояние перед восстановлением, включая последующие ручные правки.
        $rescueDir = Join-Path $chosen.Directory ('before-restore\' + [guid]::NewGuid().ToString('N'))
        $rescueManifest = Save-Snapshot $rescueDir
        Save-Manifest $rescueManifest $rescueDir
        try {
            Restore-Snapshot $chosen.Manifest $chosen.Directory
            Invoke-ApacheCheck
            $chosen.Manifest.state = 'restored'
            Save-Manifest $chosen.Manifest $chosen.Directory
        }
        catch {
            Restore-Snapshot $rescueManifest $rescueDir
            throw
        }
        Write-Host "Файлы восстановлены. Состояние перед восстановлением сохранено: $rescueDir"
        Write-Host 'Для активации перезапустите свою службу Apache в окне «Службы» Windows.'
    }
    exit 0
}
catch {
    Write-Host ("Ошибка: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
