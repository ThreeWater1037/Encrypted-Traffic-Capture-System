# Run with Windows PowerShell 5.1. Loads only function ASTs: no installation side effects.
#requires -Version 5.1
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$scriptPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'deployment\deploy_worker_windows.ps1'
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Invoke-Native','Assert-PlainPath','Install-PackageFile','Get-BinaryListing','Resolve-GitMirrorUrl','Resolve-FirefoxMirrorUrl','Expand-SafeArchive','Get-CompatibleDriver','Get-Installer','Assert-InstallerFile','Assert-SignedInstaller','Backup-IncompleteEnvironment','Install-ManagedChrome','ConvertTo-HttpsDownloadUrl','Get-WorkerTaskCredential','Register-WorkerStartupTask','Write-Utf8')) {
    $node = $ast.Find({ param($item) $item -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $item.Name -eq $name }, $true)
    . ([scriptblock]::Create($node.Extent.Text))
}
function Assert-Throws {
    param([scriptblock]$Action, [string]$Pattern)
    try { & $Action } catch {
        if ($_.Exception.Message -notmatch $Pattern) { throw }
        return
    }
    throw "Expected failure: $Pattern"
}
$shell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
Assert-Throws { Invoke-Native $shell @('-NoProfile','-Command','exit 7') } 'exit 7'
$result = Invoke-Native $shell @('-NoProfile','-Command',"Write-Output 'path with spaces'; exit 0")
if ($result -ne 'path with spaces') { throw 'Native stdout was not preserved.' }
Assert-PlainPath (Join-Path $env:TEMP 'not-yet-created-worker\project')

# Verify installer arguments without launching an installer or touching the machine.
$script:installerExit = 0
$script:launch = $null
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, [switch]$PassThru, [switch]$Wait)
    $script:launch = @{program=$FilePath; arguments=$ArgumentList; window=$WindowStyle; waited=[bool]$Wait}
    [pscustomobject]@{ExitCode=$script:installerExit}
}
Install-PackageFile 'C:\path with spaces\chrome.msi' ''
if ($script:launch.arguments -notlike '/i "C:\path with spaces\chrome.msi" /qn /norestart*' -or $script:launch.window -ne 'Hidden' -or -not $script:launch.waited) {
    throw 'MSI quoting/silent launch is incorrect.'
}
Install-PackageFile 'C:\install\miniconda.exe' '/S /D=C:\path with spaces\miniconda'
if ($script:launch.arguments -ne '/S /D=C:\path with spaces\miniconda') { throw 'Miniconda destination must remain the last, unquoted argument.' }
$script:installerExit = 3010
Assert-Throws { Install-PackageFile 'C:\install\browser.msi' '' } 'reboot'
$script:installerExit = 1603
Assert-Throws { Install-PackageFile 'C:\install\browser.msi' '' } 'exit 1603'
Write-Host 'PASS: PowerShell parser, native errors/stdout, installer quoting, hidden execution and reboot handling.'

# Exercise mirror resolution and all three driver paths without any network or installers.
& {
    $Sources = @{BinaryMirror='https://mirror.example/binary'; GitUrl=''; FirefoxMsiUrl='';
        GeckodriverVersion='0.37.1'; ChromedriverUrl=''; EdgedriverUrl=''; GeckodriverUrl=''}
    function Get-Metadata {
        param($Url)
        $response = @(switch ($Url) {
            'https://mirror.example/binary/git-for-windows/' {
                @('v2.9.0.windows.1/','v2.55.0.windows.5/','v2.56.0-rc1.windows.1/') | ForEach-Object { [pscustomobject]@{name=$_} }
            }
            'https://mirror.example/binary/git-for-windows/v2.55.0.windows.5/' {
                [pscustomobject]@{name='Git-2.55.0.5-64-bit.exe'; url='https://mirror.example/git.exe'}
            }
            'https://mirror.example/binary/firefox/' {
                @('156.0.1/','157.0b1/','latest/','115.20.0esr/') | ForEach-Object { [pscustomobject]@{name=$_} }
            }
            'https://mirror.example/binary/firefox/156.0.1/win64/zh-CN/' {
                [pscustomobject]@{name='Firefox Setup 156.0.1.msi'; url='https://mirror.example/Firefox Setup 156.0.1.msi'}
            }
            'https://mirror.example/binary/chrome-for-testing/latest-patch-versions-per-build.json' {
                [pscustomobject]@{builds=[pscustomobject]@{'154.0.8037'=[pscustomobject]@{version='154.0.8037.57'}}}
            }
            default { throw "Unexpected metadata request: $Url" }
        })
        if ($Url.EndsWith('.json')) { return $response[0] }
        # Windows PowerShell Invoke-RestMethod emits a JSON array as ONE pipeline
        # object. Enumerating a mock here would hide the production nesting bug.
        Write-Output -NoEnumerate $response
    }
    if ((Resolve-GitMirrorUrl) -ne 'https://mirror.example/git.exe') { throw 'Git stable numeric sort failed.' }
    if ((Resolve-FirefoxMirrorUrl) -ne 'https://mirror.example/Firefox Setup 156.0.1.msi') { throw 'Firefox stable selection failed.' }
    $Sources.GitUrl = 'https://custom.example/git.exe'
    if ((Resolve-GitMirrorUrl) -ne $Sources.GitUrl) { throw 'Direct source override was ignored.' }

    $tempBase = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    $testRoot = Join-Path $tempBase ('worker-mirror-test-' + [guid]::NewGuid().ToString('N'))
    $DriverDir = Join-Path $testRoot 'drivers'
    $CacheDir = Join-Path $testRoot 'cache'
    $InstallerDirectory = ''
    New-Item -ItemType Directory -Path $DriverDir,$CacheDir -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    function New-TestZip {
        param($Path, $EntryName)
        $zip = [IO.Compression.ZipFile]::Open($Path, [IO.Compression.ZipArchiveMode]::Create)
        try {
            $entry = $zip.CreateEntry($EntryName)
            $stream = $entry.Open()
            try { $bytes=[Text.Encoding]::UTF8.GetBytes('test fixture'); $stream.Write($bytes,0,$bytes.Length) } finally { $stream.Dispose() }
        } finally { $zip.Dispose() }
    }
    function Get-BrowserVersion {
        param($Path)
        if ($Path -eq 'chrome.exe') { '154.0.8037.50' } else { '154.0.4258.37' }
    }
    function Invoke-Native {
        param($File, $Arguments)
        switch ([IO.Path]::GetFileName($File)) {
            'chromedriver.exe' { 'ChromeDriver 154.0.8037.57' }
            'msedgedriver.exe' { 'MSEdgeDriver 154.0.4258.37' }
            'geckodriver.exe' { 'geckodriver 0.37.1' }
            default { throw 'Tests must not execute downloaded code.' }
        }
    }
    function Get-Installer {
        param($Name, $ResolveUrl, [switch]$Archive)
        if (-not $Archive) { throw 'Driver package must be ZIP.' }
        $url = & $ResolveUrl
        switch ($Name) {
            'chromedriver-154.0.8037.57-win64.zip' { $expected='chrome-for-testing/154.0.8037.57/win64/chromedriver-win64.zip'; $entry='chromedriver-win64/chromedriver.exe' }
            'edgedriver-154.0.4258.37-win64.zip' { $expected='edgedriver/154.0.4258.37/edgedriver_win64.zip'; $entry='msedgedriver.exe' }
            'geckodriver-v0.37.1-win64.zip' { $expected='geckodriver/v0.37.1/geckodriver-v0.37.1-win64.zip'; $entry='geckodriver.exe' }
            default { throw "Unexpected driver archive: $Name" }
        }
        if ($url -ne ($Sources.BinaryMirror + '/' + $expected)) { throw "Wrong mirror URL: $url" }
        $path = Join-Path $CacheDir $Name
        New-TestZip $path $entry
        return $path
    }
    try {
        foreach ($browser in @('chrome','edge','firefox')) {
            $driver = Get-CompatibleDriver $browser "$browser.exe"
            if (-not (Test-Path -LiteralPath $driver -PathType Leaf)) { throw "No local $browser driver." }
        }
        function Get-Metadata { throw 'A compatible local driver must bypass mirror metadata.' }
        foreach ($browser in @('chrome','edge','firefox')) { [void](Get-CompatibleDriver $browser "$browser.exe") }
        $badZip = Join-Path $CacheDir 'unsafe.zip'
        New-TestZip $badZip '../escape.exe'
        Assert-Throws { Expand-SafeArchive $badZip (Join-Path $testRoot 'unpack') } 'Unsafe ZIP'
        if (Test-Path -LiteralPath (Join-Path $testRoot 'escape.exe')) { throw 'ZIP escaped destination.' }
    } finally {
        $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
        if (-not $resolvedTestRoot.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -or
            [IO.Path]::GetFileName($resolvedTestRoot) -notlike 'worker-mirror-test-*') { throw 'Unsafe test cleanup path.' }
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
Write-Host 'PASS: mirror stable selection/overrides, three matching drivers, local reuse without metadata, ZIP traversal rejection.'

# Real file/hash/ZIP operations; only network and certificate-chain statuses are fixtures.
& {
    $tempBase = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    $testRoot = Join-Path $tempBase ('worker-download-test-' + [guid]::NewGuid().ToString('N'))
    $InstallRoot = $testRoot
    $CacheDir = Join-Path $testRoot 'installers'
    $BackupDir = Join-Path $testRoot 'backups'
    $InstallerDirectory = ''
    New-Item -ItemType Directory -Path $CacheDir,$BackupDir -Force | Out-Null
    $fixture = Join-Path $testRoot 'fixture.exe'
    [IO.File]::WriteAllText($fixture, 'known release bytes')
    $digest = (Get-FileHash $fixture -Algorithm SHA256).Hash
    $script:signatureStatus = 'UnknownError'
    function Get-AuthenticodeSignature {
        param($LiteralPath)
        [pscustomobject]@{Status=$script:signatureStatus; StatusMessage='fixture certificate chain unavailable'; SignerCertificate=[pscustomobject]@{Subject='Fixture'}}
    }
    $script:downloadCalls = @()
    $script:allBroken = $false
    function Invoke-WebRequest {
        param($Uri, [switch]$UseBasicParsing, $OutFile, $TimeoutSec)
        $script:downloadCalls += $Uri
        if ($script:allBroken -or $Uri -like '*first*') { [IO.File]::WriteAllText($OutFile, '<html>mirror error</html>') }
        else { Copy-Item -LiteralPath $fixture -Destination $OutFile -Force }
    }
    function Start-Sleep { param($Seconds) }
    try {
        Assert-InstallerFile $fixture -ExpectedSha256 $digest
        Assert-Throws { Assert-InstallerFile $fixture } 'UnknownError.*certificate chain unavailable'
        $script:signatureStatus = 'Valid'
        Assert-Throws { Assert-InstallerFile $fixture -ExpectedSha256 ('0' * 64) } 'SHA256 mismatch'
        $script:signatureStatus = 'UnknownError'

        $rawUrl = 'https://registry.npmmirror.com/-/binary/firefox/156.0.1/win64/zh-CN/Firefox Setup 156.0.1.msi'
        $escapedUrl = $rawUrl.Replace(' ', '%20')
        if ((ConvertTo-HttpsDownloadUrl $rawUrl) -cne $escapedUrl -or
            (ConvertTo-HttpsDownloadUrl $escapedUrl) -cne $escapedUrl) { throw 'URL escaping rejected spaces or double-encoded percent escapes.' }
        if ((ConvertTo-HttpsDownloadUrl 'https://example.test/a%20b.msi?token=a%2Fb%2Bc&x=1') -cne 'https://example.test/a%20b.msi?token=a%2Fb%2Bc&x=1') { throw 'URL query was changed.' }
        foreach ($bad in @('http://example.test/a','file:///C:/a','/relative','https://bad host/a','https://user:password@example.test/a','https://example.test/a#fragment',"https://example.test/a`nb")) {
            Assert-Throws { ConvertTo-HttpsDownloadUrl $bad } 'Invalid installer URL'
        }
        [void](Get-Installer 'firefox.msi' { $rawUrl } -ExpectedSha256 $digest)
        if ($script:downloadCalls[-1] -cne $escapedUrl) { throw 'Downloader did not receive the escaped Firefox URL.' }
        $script:downloadCalls = @()

        $cached = Join-Path $CacheDir 'miniconda.exe'
        [IO.File]::WriteAllText($cached, 'stale latest installer')
        $actual = Get-Installer 'miniconda.exe' { 'https://first.example/package'; 'https://second.example/package' } -ExpectedSha256 $digest
        if ($actual -ne $cached -or (Get-FileHash $actual).Hash -ne $digest) { throw 'Verified fallback was not promoted.' }
        if ($script:downloadCalls.Count -ne 2) { throw 'Fallback did not run immediately after invalid first mirror.' }
        if (@(Get-ChildItem $CacheDir -Filter '*.rejected-*').Count -ne 1) { throw 'Stale cache was not preserved.' }
        [void](Get-Installer 'miniconda.exe' { throw 'Valid cache must not resolve network URLs.' } -ExpectedSha256 $digest)

        $InstallerDirectory = Join-Path $testRoot 'supplied'
        New-Item -ItemType Directory $InstallerDirectory | Out-Null
        [IO.File]::WriteAllText((Join-Path $InstallerDirectory 'miniconda.exe'), 'wrong upload')
        Assert-Throws { Get-Installer 'miniconda.exe' { throw 'No network for bad explicit upload.' } -ExpectedSha256 $digest } 'SHA256 mismatch'
        if ((Get-FileHash $cached).Hash -ne $digest) { throw 'Invalid upload overwrote verified cache.' }
        $InstallerDirectory = $CacheDir
        [void](Get-Installer 'miniconda.exe' { throw 'Local file must not use network.' } -ExpectedSha256 $digest)
        $InstallerDirectory = ''

        $script:allBroken = $true
        $script:downloadCalls = @()
        Assert-Throws { Get-Installer 'unavailable.exe' { 'https://first.example/package'; 'https://second.example/package' } -ExpectedSha256 $digest } 'after 3 attempts per source'
        if ($script:downloadCalls.Count -ne 6 -or (Test-Path (Join-Path $CacheDir 'unavailable.exe'))) { throw 'Invalid download became a cached executable.' }
        Assert-Throws { Get-Installer 'http.exe' { 'http://example/package' } } 'Invalid installer URL'

        Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
        $zipPath = Join-Path $testRoot 'valid.zip'
        $zip = [IO.Compression.ZipFile]::Open($zipPath, [IO.Compression.ZipArchiveMode]::Create)
        $entry = $zip.CreateEntry('binary.txt')
        $writer = New-Object IO.StreamWriter($entry.Open())
        $writer.Write('archive data'); $writer.Dispose(); $zip.Dispose()
        Assert-InstallerFile $zipPath -Archive
        $bytes = [IO.File]::ReadAllBytes($zipPath)
        [IO.File]::WriteAllBytes($zipPath, $bytes[0..10])
        Assert-Throws { Assert-InstallerFile $zipPath -Archive } 'End of Central Directory|central directory|central-directory|中央目录'

        # An unsigned CfT executable must be accepted ONLY inside the pinned ZIP.
        # Rerunning must repair a previous extraction containing chrome.exe alone.
        $chromeZip = Join-Path $CacheDir 'chrome-win64.zip'
        $zip = [IO.Compression.ZipFile]::Open($chromeZip, [IO.Compression.ZipArchiveMode]::Create)
        foreach ($name in @('chrome.exe','chrome.dll')) {
            $entry = $zip.CreateEntry('chrome-win64/' + $name)
            $writer = New-Object IO.StreamWriter($entry.Open())
            $writer.Write('fixture ' + $name); $writer.Dispose()
        }
        $zip.Dispose()
        $Sources = @{ChromeVersion='154.0.8037.57'; ChromeZipSha256=(Get-FileHash $chromeZip).Hash;
            ChromeZipUrl=''; BinaryMirror='https://fixture.example'}
        $BrowserDir = Join-Path $testRoot 'browsers'
        function Get-BrowserVersion { param($Path) '154.0.8037.57' }
        function Get-AuthenticodeSignature { throw 'CfT must use the upstream ZIP hash, not an absent executable signature.' }
        [void](Install-ManagedChrome)
        $dll = Join-Path $BrowserDir 'chrome-win64\chrome.dll'
        [IO.File]::WriteAllText($dll, 'interrupted extraction')
        [void](Install-ManagedChrome)
        if ([IO.File]::ReadAllText($dll) -ne 'fixture chrome.dll') { throw 'Incomplete Chrome extraction was reused.' }
        $Sources.ChromeVersion = '155.0.0.0'
        Assert-Throws { Install-ManagedChrome } 'configured ChromeVersion'

        $partial = Join-Path $InstallRoot 'miniconda\envs\traffic-worker'
        New-Item -ItemType Directory $partial -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $partial 'preserved.txt'), 'keep me')
        Backup-IncompleteEnvironment 'miniconda\envs\traffic-worker'
        if ((Test-Path $partial) -or -not (Test-Path (Join-Path $BackupDir 'incomplete-traffic-worker\preserved.txt'))) { throw 'Interrupted environment was not preserved.' }
        $BackupDir = $tempBase
        Assert-Throws { Backup-IncompleteEnvironment 'miniconda' } 'Unsafe incomplete-environment backup path'
    } finally {
        $resolved = [IO.Path]::GetFullPath($testRoot)
        if (-not $resolved.StartsWith($tempBase, [StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notlike 'worker-download-test-*') { throw 'Unsafe test cleanup path.' }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
Write-Host 'PASS: pinned hash with UnknownError, mismatch rejection, invalid cache recovery, mirror fallback/retries, upload validation, truncated ZIP, unsigned CfT and interrupted environment/extraction recovery.'

# Do not register a real task or request real credentials on the development PC.
& {
    $current = [Security.Principal.WindowsIdentity]::GetCurrent()
    $dummy = New-Object Management.Automation.PSCredential($current.Name, (ConvertTo-SecureString 'fixture-only-not-a-real-password' -AsPlainText -Force))
    $accepted = Get-WorkerTaskCredential $dummy $current.Name $current.User.Value
    if ($accepted.UserName -ne $current.Name) { throw 'The deploying account was not preserved.' }
    Assert-Throws { Get-WorkerTaskCredential $dummy 'SYSTEM' 'S-1-5-18' } 'not a service account'
    Assert-Throws { Get-WorkerTaskCredential $dummy $current.Name 'S-1-5-21-1-2-3-1001' } 'same administrator account'
    $empty = New-Object Management.Automation.PSCredential($current.Name, (New-Object Security.SecureString))
    Assert-Throws { Get-WorkerTaskCredential $empty $current.Name $current.User.Value } 'password is required'
    function Get-Credential { param($UserName, $Message) return $null }
    Assert-Throws { Get-WorkerTaskCredential $null $current.Name $current.User.Value } 'password is required'

    function New-ScheduledTaskAction { param($Execute, $Argument, $WorkingDirectory) [pscustomobject]@{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory} }
    function New-ScheduledTaskTrigger { param([switch]$AtStartup) [pscustomobject]@{AtStartup=[bool]$AtStartup} }
    function New-ScheduledTaskPrincipal { param($UserId,$LogonType,$RunLevel) [pscustomobject]@{UserId=$UserId;LogonType=$LogonType;RunLevel=$RunLevel} }
    function New-ScheduledTaskSettingsSet {
        param([switch]$StartWhenAvailable,$MultipleInstances,$ExecutionTimeLimit,$RestartCount,$RestartInterval,[switch]$AllowStartIfOnBatteries,[switch]$DontStopIfGoingOnBatteries)
        [pscustomobject]@{Enabled=$true;RestartCount=$RestartCount;RestartInterval=$RestartInterval;ExecutionTimeLimit=$ExecutionTimeLimit;MultipleInstances=$MultipleInstances}
    }
    function New-ScheduledTask { param($Action,$Trigger,$Principal,$Settings,$Description) [pscustomobject]@{Actions=@($Action);Triggers=@($Trigger);Principal=$Principal;Settings=$Settings;Description=$Description} }
    $script:taskRegistrations = @()
    function Register-ScheduledTask {
        param($TaskName,$TaskPath,$InputObject,$User,$Password,[switch]$Force)
        if ($Password -ne $dummy.GetNetworkCredential().Password) { throw 'Credential was not passed to Task Scheduler.' }
        $script:taskRegistrations += [pscustomobject]@{Name=$TaskName;Path=$TaskPath;Definition=$InputObject;User=$User;Force=[bool]$Force}
    }
    $TaskName='TrafficCaptureWorker'; $TaskDescription='Managed by deploy_worker_windows.ps1; headless capture Worker'
    $ProjectDir='C:\Worker test\project'; $Python='C:\Worker test\env\python.exe'; $Runner='C:\Worker test\run\run_worker.py'
    Register-WorkerStartupTask $dummy
    $registered=$script:taskRegistrations[-1]
    if ($registered.Definition.Principal.LogonType -ne 'Password' -or $registered.User -ne $current.Name -or
        $registered.Definition.Principal.RunLevel -ne 'Highest' -or -not $registered.Definition.Triggers[0].AtStartup -or
        $registered.Definition.Actions[0].Arguments -ne "-u `"$Runner`"" -or $registered.Definition.Settings.RestartCount -ne 999) {
        throw 'Startup task must use a real account with password logon and preserve its restart/action settings.'
    }

    # Execute the actual repair branch with fixture files and stubbed OS effects.
    $tempBase=[IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
    $testRoot=Join-Path $tempBase ('worker-task-repair-test-' + [guid]::NewGuid().ToString('N'))
    $RunDir=Join-Path $testRoot 'run'; $ProjectDir=Join-Path $testRoot 'project'
    New-Item -ItemType Directory -Path $RunDir,$ProjectDir -Force | Out-Null
    $Python=Join-Path $testRoot 'python.exe'; $Runner=Join-Path $RunDir 'run_worker.py'; $ConfigFile=Join-Path $ProjectDir 'worker.yaml'
    foreach ($file in @($Python,$Runner,$ConfigFile,(Join-Path $RunDir 'deploy_helper.py'))) { Write-Utf8 $file 'preserved fixture content' }
    Write-Utf8 (Join-Path $RunDir 'runtime.json') '{"skip_smoke":true,"smoke_url":"https://old.example","smoke_timeout":60,"drivers":{"EDGEDRIVER_PATH":"existing-driver"}}'
    $RepairTaskOnly=$true; $SkipSmoke=$false; $SmokeUrl='https://www.baidu.com'; $SmokeTimeout=900; $TaskCredential=$dummy
    $oldTask=[pscustomobject]@{Principal=[pscustomobject]@{UserId='SYSTEM'}}
    $script:started=$false; $script:verificationCalls=@()
    function Start-ScheduledTask { param($TaskName,$TaskPath) $script:started=$true }
    function Get-ScheduledTask { param($TaskName,$TaskPath) [pscustomobject]@{State='Running';Settings=[pscustomobject]@{Enabled=$true}} }
    function Invoke-Native {
        param($File,$Arguments)
        if (-not $script:started -or $Arguments[1] -ne 'verify') { throw 'Repair must start the task before capture verification.' }
        $script:verificationCalls += ,$Arguments
    }
    try {
        $branch=$ast.Find({param($n) $n -is [System.Management.Automation.Language.IfStatementAst] -and $n.Extent.Text.StartsWith('if ($RepairTaskOnly)')},$true)
        if (-not $branch) { throw 'Missing repair-only branch.' }
        & ([scriptblock]::Create($branch.Extent.Text))
        $runtime=Get-Content (Join-Path $RunDir 'runtime.json') -Raw | ConvertFrom-Json
        if ($runtime.skip_smoke -or $runtime.drivers.EDGEDRIVER_PATH -ne 'existing-driver' -or $script:verificationCalls.Count -ne 1 -or $script:taskRegistrations.Count -ne 2) {
            throw 'Repair failed to preserve runtime settings and rerun real capture verification.'
        }
        foreach ($file in @($Python,$Runner,$ConfigFile)) {
            if ([IO.File]::ReadAllText($file) -ne 'preserved fixture content') { throw 'Repair changed an installed component or Worker configuration.' }
        }
    } finally {
        $resolved=[IO.Path]::GetFullPath($testRoot)
        if (-not $resolved.StartsWith($tempBase,[StringComparison]::OrdinalIgnoreCase) -or [IO.Path]::GetFileName($resolved) -notlike 'worker-task-repair-test-*') { throw 'Unsafe repair fixture cleanup.' }
        Remove-Item -LiteralPath $resolved -Recurse -Force
        $current.Dispose()
    }
}
Write-Host 'PASS: reject SYSTEM/incorrect/empty credentials, password-logon startup task, repair-only preserves components and reruns capture verification.'
