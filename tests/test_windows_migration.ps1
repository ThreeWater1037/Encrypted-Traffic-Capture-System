#requires -Version 5.1
# Real temporary filesystem and shortcuts, mocked installers/tasks; no server changes.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$scriptPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'deployment\deploy_worker_windows.ps1'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Assert-PlainPath','Assert-PlainTree','Get-LegacyCleanupTargets','Remove-LegacyDeployment','Assert-WorkerTaskOwner','Uninstall-LegacyPackage','Find-ExistingBrowser','Publish-ManagedChrome','Write-Utf8','Initialize-SoftwareDirectory')) {
    $node = $ast.Find({ param($item) $item -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $item.Name -eq $name }, $true)
    . ([scriptblock]::Create($node.Extent.Text))
}
function Assert-Throws {
    param([scriptblock]$Action, [string]$Pattern)
    try { & $Action } catch { if ($_.Exception.Message -notmatch $Pattern) { throw }; return }
    throw "Expected failure: $Pattern"
}
$tempBase = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
$testRoot = Join-Path $tempBase ('worker-migration-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testRoot | Out-Null
function New-OwnedRoot {
    param($Root)
    New-Item -ItemType Directory -Path $Root -Force | Out-Null
    Write-Utf8 (Join-Path $Root 'windows-worker-deployment.json') '{"managed_by":"deploy_worker_windows.ps1"}'
}
try {
    $LegacyRoot = Join-Path $testRoot 'old C worker'
    $RunDir = Join-Path $testRoot 'new D worker\run'
    $BrowserDir = Join-Path $LegacyRoot 'browsers'
    New-OwnedRoot $LegacyRoot
    New-Item -ItemType Directory -Path $RunDir,(Join-Path $LegacyRoot 'project\worker_data'),(Join-Path $LegacyRoot 'run'),(Join-Path $BrowserDir 'chrome-win64') -Force | Out-Null
    Write-Utf8 (Join-Path $LegacyRoot 'project\worker.yaml') 'original-config-secret'
    Write-Utf8 (Join-Path $LegacyRoot 'project\worker_data\capture.pcap') 'old capture to delete'
    $chrome = Join-Path $BrowserDir 'chrome-win64\chrome.exe'
    foreach ($file in @('chrome.exe','chrome.dll','resources.pak')) { Write-Utf8 (Join-Path (Split-Path $chrome -Parent) $file) ('original ' + $file) }
    $outside = Join-Path $testRoot 'unrelated'
    New-Item -ItemType Directory -Path $outside | Out-Null
    Write-Utf8 (Join-Path $outside 'keep.txt') 'must survive'
    $outsideBrowser = Join-Path $outside 'firefox.exe'
    Write-Utf8 $outsideBrowser 'existing Firefox'
    $originalRuntime = @{browsers=@{chrome=$chrome;edge=$outsideBrowser;firefox=$outsideBrowser}} | ConvertTo-Json
    Write-Utf8 (Join-Path $LegacyRoot 'run\runtime.json') $originalRuntime
    $browserHash = (Get-FileHash $chrome).Hash
    function Find-MachineFile { param($RelativePath) return $null }
    # Never read real registry entries while testing fallback discovery.
    function Test-Path {
        param($LiteralPath, $PathType)
        if ($LiteralPath -like 'HK*:*') { return $false }
        if ($PathType) { Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType $PathType }
        else { Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath }
    }
    foreach ($browser in @('chrome','edge','firefox')) {
        Copy-Item -LiteralPath (Join-Path $LegacyRoot 'run\runtime.json') -Destination (Join-Path $RunDir 'legacy-runtime.json') -Force
        $found = Find-ExistingBrowser $browser
        if (-not $found -or ($browser -eq 'chrome' -and $found -ne $chrome)) { throw "Failed to preserve existing $browser." }
    }
    # Execute the production migration branch: backup, ownership record, task removal, cleanup.
    $migrationFile = Join-Path $RunDir 'migration-from-c.json'
    $legacyConfig = Join-Path $RunDir 'legacy-worker.yaml'
    $TaskName = 'TrafficCaptureWorker'; $TaskDescription = 'Managed by deploy_worker_windows.ps1; headless capture Worker'
    $oldTask = [pscustomobject]@{Description=$TaskDescription;Actions=@([pscustomobject]@{
        Execute=(Join-Path $LegacyRoot 'miniconda\envs\traffic-worker\python.exe');Arguments=('-u "' + (Join-Path $LegacyRoot 'run\run_worker.py') + '"')})}
    Assert-WorkerTaskOwner $oldTask $LegacyRoot
    Assert-Throws { Assert-WorkerTaskOwner $oldTask (Join-Path $testRoot 'wrong root') } 'not owned'
    $unrelatedTask = [pscustomobject]@{Description='unrelated task';Actions=$oldTask.Actions}
    Assert-Throws { Assert-WorkerTaskOwner $unrelatedTask $LegacyRoot } 'not owned'
    $MigrateLegacy=$true; $taskRoot=$LegacyRoot; $script:unregistered=$false; $script:survivor=$false
    function Get-CimInstance { param($ClassName) if ($script:survivor) { [pscustomobject]@{ExecutablePath=(Join-Path $LegacyRoot 'miniconda\python.exe')} } }
    function Unregister-ScheduledTask {
        param($TaskName,$TaskPath,[switch]$Confirm)
        if (-not (Test-Path -LiteralPath $legacyConfig) -or -not (Test-Path -LiteralPath $migrationFile)) { throw 'Cleanup must back up before removing old task.' }
        $script:unregistered=$true
    }
    $branch = $ast.Find({param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and $n.Extent.Text.StartsWith('if ($MigrateLegacy)') -and $n.Extent.Text.Contains('Remove-LegacyDeployment')},$true)
    $script:survivor=$true
    Assert-Throws { & ([scriptblock]::Create($branch.Extent.Text)) } 'processes are still running'
    if ($script:unregistered -or -not (Test-Path -LiteralPath (Join-Path $LegacyRoot 'project\worker_data\capture.pcap'))) { throw 'Active old process did not block cleanup.' }
    $script:survivor=$false
    & ([scriptblock]::Create($branch.Extent.Text))
    if (-not $script:unregistered -or [IO.File]::ReadAllText($legacyConfig) -ne 'original-config-secret') { throw 'Migration lost config/task ownership.' }
    if ((Get-FileHash $chrome).Hash -ne $browserHash -or (Test-Path -LiteralPath (Join-Path $LegacyRoot 'project'))) { throw 'Cleanup failed to preserve browser/delete legacy data.' }
    if ([IO.File]::ReadAllText((Join-Path $outside 'keep.txt')) -ne 'must survive') { throw 'Cleanup touched unrelated files.' }
    Assert-Throws { Get-LegacyCleanupTargets $LegacyRoot } 'not a managed deployment'
    Assert-Throws { Get-LegacyCleanupTargets $outside } 'not a managed deployment'
    Assert-Throws { Get-LegacyCleanupTargets ([IO.Path]::GetPathRoot($testRoot)) } 'Unsafe legacy cleanup root'
    Write-Host 'PASS: production migration backs up config, rejects active processes/unrelated tasks, removes old data, preserves all three browser paths.'

    # Locked file simulates an interrupted cleanup; ownership remains for a safe retry.
    $retryRoot = Join-Path $testRoot 'retry'
    New-OwnedRoot $retryRoot
    $lockedFile = Join-Path $retryRoot 'locked.dat'
    Write-Utf8 $lockedFile 'fixture'
    $handle = [IO.File]::Open($lockedFile, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try { Assert-Throws { Remove-LegacyDeployment $retryRoot } '.+' } finally { $handle.Dispose() }
    if (-not (Test-Path -LiteralPath (Join-Path $retryRoot 'windows-worker-deployment.json'))) { throw 'Interrupted cleanup lost ownership marker.' }
    Remove-LegacyDeployment $retryRoot
    if (Test-Path -LiteralPath $retryRoot) { throw 'Retry did not complete.' }
    $junctionRoot=Join-Path $testRoot 'junction root'
    New-OwnedRoot $junctionRoot
    $junction=Join-Path $junctionRoot 'escape'
    New-Item -ItemType Junction -Path $junction -Target $outside | Out-Null
    try { Assert-Throws { Remove-LegacyDeployment $junctionRoot } 'Junction/symlink' }
    finally {
        $resolved=[IO.Path]::GetFullPath($junction)
        if (-not $resolved.StartsWith($testRoot + '\',[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe junction fixture cleanup.' }
        [IO.Directory]::Delete($resolved)
    }
    Write-Host 'PASS: interrupted cleanup is retryable; drive roots, unmanaged directories and junction traversal are rejected.'

    # In-place browser reuse does not need metadata or reinstall. Partial ZIP extraction does.
    if ((Find-ExistingBrowser 'chrome') -ne $chrome) { throw 'Portable Chrome was not reused.' }
    $dll=Join-Path (Split-Path $chrome -Parent) 'chrome.dll'
    Remove-Item -LiteralPath $dll
    $previousLocalAppData=$env:LOCALAPPDATA
    try { $env:LOCALAPPDATA=$outside; if (Find-ExistingBrowser 'chrome') { throw 'Partial Chrome extraction was accepted.' } }
    finally { $env:LOCALAPPDATA=$previousLocalAppData }
    Write-Utf8 $dll 'original chrome.dll'
    $shortcutDirectory=Join-Path $testRoot 'test shortcuts'
    New-Item -ItemType Directory -Path $shortcutDirectory | Out-Null
    Publish-ManagedChrome $chrome @($shortcutDirectory)
    $shortcutPath=Join-Path $shortcutDirectory 'Chrome for Testing.lnk'
    if (-not (Test-Path -LiteralPath $shortcutPath -PathType Leaf)) { throw 'Chrome shortcut missing.' }
    $shell=New-Object -ComObject WScript.Shell
    $shortcut=$shell.CreateShortcut($shortcutPath)
    try {
        if ($shortcut.TargetPath -ne $chrome -or $shortcut.WorkingDirectory -ne (Split-Path $chrome -Parent)) { throw 'Chrome shortcut points to wrong binary.' }
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
    if ((Get-FileHash $chrome).Hash -ne $browserHash) { throw 'Publishing Chrome changed its installed binary.' }
    Write-Host 'PASS: existing Chrome is reused, interrupted extraction is rejected, real .lnk points to preserved browser.'

    $script:uninstallCalls=@()
    $script:hookExpectation=$null
    $script:failUninstall=$false
    function Install-PackageFile {
        param($Path,$Arguments)
        $script:uninstallCalls += [pscustomobject]@{Path=$Path;Arguments=$Arguments}
        if ($null -ne $script:hookExpectation) {
            $hookPath=Join-Path (Split-Path $Path -Parent) 'pre_uninstall.bat'
            $actual=[Convert]::ToBase64String([IO.File]::ReadAllBytes($hookPath))
            if ($actual -ne $script:hookExpectation) { throw 'Unexpected hook changes during uninstall.' }
        }
        if ($script:failUninstall) { throw 'Simulated uninstaller failure' }
        Remove-Item -LiteralPath $script:fixtureBinary
    }
    # The real server hook suppresses the optional update's output. Preserve
    # every other byte and restore the hook if the official uninstaller fails.
    $BackupDir=Join-Path $testRoot 'hook backups'
    New-Item -ItemType Directory -Path $BackupDir | Out-Null
    $knownCommand='"%PREFIX%\_conda.exe" constructor update-bootstrapper >NUL 2>&1'
    foreach ($failure in @($false,$true)) {
        $root=Join-Path $testRoot ('Miniconda hook ' + $failure)
        New-Item -ItemType Directory -Path $root | Out-Null
        $hookPath=Join-Path $root 'pre_uninstall.bat'
        $source="@ECHO OFF`r`n$knownCommand`r`nIF `"%UNINSTALLER_REMOVE_USER_DATA%`" == `"1`" ECHO keep-user-data-logic`r`nREM preserved accented byte: " + [char]233 + "`r`nEXIT /B 0`r`n"
        $encoding=[Text.Encoding]::GetEncoding(28591)
        $bytes=$encoding.GetBytes($source)
        [IO.File]::WriteAllBytes($hookPath,$bytes)
        $script:hookExpectation=[Convert]::ToBase64String($encoding.GetBytes($source.Replace($knownCommand,'REM Worker migration: skip optional bootstrapper self-update.')))
        $script:fixtureBinary=Join-Path $root 'conda.exe'
        $uninstaller=Join-Path $root 'Uninstall-Miniconda3.exe'
        Write-Utf8 $script:fixtureBinary 'fixture'
        Write-Utf8 $uninstaller 'fixture'
        $script:failUninstall=$failure
        if ($failure) {
            Assert-Throws { Uninstall-LegacyPackage 'Miniconda' $root $script:fixtureBinary } 'Simulated uninstaller failure'
            if (-not (Test-Path -LiteralPath $script:fixtureBinary)) { throw 'Failed uninstall deleted software.' }
        } else { Uninstall-LegacyPackage 'Miniconda' $root $script:fixtureBinary }
        if ([Convert]::ToBase64String([IO.File]::ReadAllBytes($hookPath)) -ne [Convert]::ToBase64String($bytes)) { throw 'Surviving hook was not restored byte for byte.' }
        $backupMatches=@(Get-ChildItem -LiteralPath $BackupDir | Where-Object { [Convert]::ToBase64String([IO.File]::ReadAllBytes($_.FullName)) -eq [Convert]::ToBase64String($bytes) })
        if (-not $backupMatches.Count) { throw 'Original hook was not backed up.' }
    }
    $script:hookExpectation=$null; $script:failUninstall=$false
    $callCount=$script:uninstallCalls.Count
    foreach ($unknown in @('"%PREFIX%\_conda.exe" constructor update-bootstrapper --unknown', "$knownCommand`r`n$knownCommand", "$knownCommand`r`n$knownCommand --unknown")) {
        Write-Utf8 $hookPath $unknown
        Assert-Throws { Uninstall-LegacyPackage 'Miniconda' $root $script:fixtureBinary } 'Unrecognized Miniconda'
        if ([IO.File]::ReadAllText($hookPath) -ne $unknown -or $script:uninstallCalls.Count -ne $callCount) { throw 'Unknown hook should fail unchanged before launching uninstall.' }
    }
    Write-Host 'PASS: optional Miniconda update alone is bypassed; hook backup, exact byte preservation, failure restoration and unknown-command rejection.'
    foreach ($name in @('Git','Wireshark','Miniconda')) {
        $root=Join-Path $testRoot ('software ' + $name)
        New-Item -ItemType Directory -Path $root | Out-Null
        $script:fixtureBinary=Join-Path $root 'binary.exe'
        Write-Utf8 $script:fixtureBinary 'never executed'
        $uninstaller=Join-Path $root $(if ($name -eq 'Git') { 'unins000.exe' } elseif ($name -eq 'Miniconda') { 'Uninstall-Miniconda3.exe' } else { 'uninstall-wireshark.exe' })
        Assert-Throws { Uninstall-LegacyPackage $name $root $script:fixtureBinary } 'Missing.*uninstaller'
        if (-not (Test-Path -LiteralPath $script:fixtureBinary)) { throw 'Missing uninstaller must not delete installed software.' }
        Write-Utf8 $uninstaller 'never executed'
        Uninstall-LegacyPackage $name $root $script:fixtureBinary
        $call=$script:uninstallCalls[-1]
        $expected=if ($name -eq 'Git') { '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART' } else { "/S _?=$root" }
        if ($call.Path -ne $uninstaller -or $call.Arguments -ne $expected) { throw 'Incorrect product uninstaller arguments.' }
        if ($name -ne 'Git' -and (Test-Path -LiteralPath $uninstaller)) { throw 'NSIS uninstaller residue was not removed after exit.' }
    }
    # Real file discovery for the old name and modern-name precedence. Never
    # launch the fixtures; Install-PackageFile records the chosen path/arguments.
    foreach ($modernPresent in @($false,$true)) {
        $root=Join-Path $testRoot ('Wireshark fallback ' + $modernPresent)
        New-Item -ItemType Directory -Path $root | Out-Null
        $script:fixtureBinary=Join-Path $root 'tshark.exe'
        Write-Utf8 $script:fixtureBinary 'fixture'
        $legacy=Join-Path $root 'uninstall.exe'
        Write-Utf8 $legacy 'fixture'
        $expected=$legacy
        if ($modernPresent) {
            $expected=Join-Path $root 'uninstall-wireshark.exe'
            Write-Utf8 $expected 'fixture'
        }
        Uninstall-LegacyPackage 'Wireshark' $root $script:fixtureBinary
        if ($script:uninstallCalls[-1].Path -ne $expected) { throw 'Wireshark uninstaller filename priority is incorrect.' }
        if ($modernPresent -and -not (Test-Path -LiteralPath $legacy)) { throw 'Cleanup removed an unselected file.' }
    }
    Assert-Throws { Uninstall-LegacyPackage 'Git' $outside (Join-Path $testRoot 'outside.exe') } 'Unsafe legacy software path'
    Write-Host 'PASS: modern/legacy Wireshark names and priority, validated uninstall paths, waited NSIS process; missing uninstallers preserve the installation.'

    # Execute actual package install branches with mocked installers: never run software.
    $migratedFromC=$true
    $GitRoot=Join-Path $testRoot 'C Git fixture'
    $WiresharkRoot=Join-Path $testRoot 'new D worker\tools\Wireshark'
    $Git=$null; $gitInstallLog=Join-Path $testRoot 'git-install.log'; $tshark=Join-Path $WiresharkRoot 'tshark.exe'
    $Sources=@{WiresharkSha256=('0' * 64)}
    $script:packageEvents=@()
    function Find-MachineFile { param($RelativePath) return ('C:\Program Files\' + $RelativePath) }
    function Find-ExistingGit {
        $binary=Join-Path $GitRoot 'cmd\git.exe'
        if (Test-Path -LiteralPath $binary -PathType Leaf) { return $binary }
        return $null
    }
    function Get-Installer {
        param($Name,$ResolveUrl,$ExpectedSha256)
        $script:packageEvents += 'download:' + $Name
        return (Join-Path $testRoot $Name)
    }
    function Uninstall-LegacyPackage { param($Name,$Root,$Binary) $script:packageEvents += 'uninstall:' + $Name }
    function Install-PackageFile {
        param($Path,$Arguments)
        if ($Path.EndsWith('git.exe')) {
            if (-not $Arguments.EndsWith('/DIR="' + $GitRoot + '"') -or -not $Arguments.Contains('/LOG="' + $gitInstallLog + '"')) { throw 'Git destination/log arguments missing.' }
            if (-not (Test-Path -LiteralPath $GitRoot -PathType Container)) { throw 'Git target must exist before running installer.' }
            $binary=Join-Path $GitRoot 'cmd\git.exe'
        } else {
            if (-not $Arguments.EndsWith('/D=' + $WiresharkRoot)) { throw 'Wireshark destination must be the last unquoted argument.' }
            $binary=$tshark
        }
        $script:packageEvents += 'install:' + [IO.Path]::GetFileName($Path)
        New-Item -ItemType Directory -Path (Split-Path $binary -Parent) -Force | Out-Null
        Write-Utf8 $binary 'fixture installed binary'
    }
    foreach ($expression in @('if (-not $Git)','if (-not (Test-Path -LiteralPath $tshark -PathType Leaf))')) {
        $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and $n.Extent.Text.StartsWith($expression)},$true)
        . ([scriptblock]::Create($node.Extent.Text))
        $count=$script:packageEvents.Count
        . ([scriptblock]::Create($node.Extent.Text))
        if ($script:packageEvents.Count -ne $count) { throw 'Rerun reinstalled an existing package.' }
    }
    if (($script:packageEvents -join ',') -ne 'download:git.exe,install:git.exe,download:wireshark.exe,uninstall:Wireshark,install:wireshark.exe') { throw 'Git must not be uninstalled; Wireshark must be obtained before removing its C: copy.' }
    Write-Host 'PASS: Git target/logging and reuse without uninstall; Wireshark migrates into tools and is reused on retry.'
} finally {
    $resolved=[IO.Path]::GetFullPath($testRoot)
    if (-not $resolved.StartsWith($tempBase,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notlike 'worker-migration-test-*') { throw 'Unsafe migration test cleanup.' }
    Remove-Item -LiteralPath $resolved -Recurse -Force
}
