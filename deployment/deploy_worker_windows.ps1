#requires -Version 5.1
<#
.SYNOPSIS
Install/update a dedicated Windows x64 traffic Worker. Run as Administrator.
.EXAMPLE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\deploy_worker_windows.ps1 -WorkerId win-worker-02
#>
[CmdletBinding()]
param(
    [string]$InstallRoot = 'C:\TrafficWorker',
    [string]$WorkerId = '',
    [string]$MasterIP = '',
    [string]$InstallerDirectory = '',
    [string]$DownloadConfig = '',
    [string]$NpcapOemInstaller = '',
    [switch]$InteractiveNpcap,
    # Verify HTTPS capture with a small document, independent of homepage ads,
    # long-lived requests, and same-document image reuse in Firefox.
    [string]$SmokeUrl = 'https://www.baidu.com/robots.txt',
    [ValidateRange(60, 86400)][int]$SmokeTimeout = 900,
    [switch]$SkipSmoke,
    [switch]$AllowInterrupt,
    [switch]$RotateToken,
    [System.Management.Automation.PSCredential]$TaskCredential,
    [switch]$RepairTaskOnly
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$RepoUrl = 'https://gitee.com/Threewater1037/encrypted-traffic-capture-system.git'
$TaskName = 'TrafficCaptureWorker'
$TaskDescription = 'Managed by deploy_worker_windows.ps1; headless capture Worker'
$Sources = @{
    BinaryMirror = 'https://registry.npmmirror.com/-/binary'
    # Pin the filename AND the upstream digest; mirror "latest" aliases can be years stale.
    # https://repo.anaconda.com/miniconda/ (verified 2026-09-25)
    MinicondaUrl = 'https://mirrors.nju.edu.cn/anaconda/miniconda/Miniconda3-py312_26.7.1-1-Windows-x86_64.exe'
    MinicondaFallbackUrl = 'https://mirrors.pku.edu.cn/anaconda/miniconda/Miniconda3-py312_26.7.1-1-Windows-x86_64.exe'
    MinicondaSha256 = '8ae918681b0830314d85207f7a244352762b543bb83d53e0fcf77fbf270f8331'
    # Chrome for Testing Windows binaries are not necessarily Authenticode-signed.
    # Digest measured from Google's HTTPS artifact AND matched against npmmirror:
    # https://storage.googleapis.com/chrome-for-testing-public/154.0.8037.57/win64/chrome-win64.zip
    ChromeVersion = '154.0.8037.57'
    ChromeZipSha256 = '676f51fb82608330db5510ffba53d9e2762d3d7a99464afce54f9e9e25ad6bf7'
    CondaChannel = 'https://mirror.sjtu.edu.cn/anaconda/cloud/conda-forge'
    PipIndexUrl = 'https://mirrors.aliyun.com/pypi/simple/'
    # https://www.wireshark.org/download/SIGNATURES-4.6.9.txt (verified 2026-09-25)
    WiresharkUrl = 'https://mirrors.nju.edu.cn/wireshark/win64/Wireshark-4.6.9-x64.exe'
    WiresharkFallbackUrl = 'https://mirrors.aliyun.com/wireshark/win64/Wireshark-4.6.9-x64.exe'
    WiresharkSha256 = 'bf9b5ce8a89f244c376a9b1a946276eaa06463dde3e33069a34d7f102f5878cf'
    GitUrl = ''; ChromeZipUrl = ''; FirefoxMsiUrl = ''; EdgeMsiUrl = ''; NpcapUrl = ''
    ChromedriverUrl = ''; EdgedriverUrl = ''; GeckodriverUrl = ''
    GeckodriverVersion = '0.37.1'
}
function ConvertTo-HttpsDownloadUrl {
    param($Url)
    $parsed = $null
    if ($Url -isnot [string] -or $Url -notmatch '^https://' -or $Url -match '[\x00-\x1f\x7f]' -or
        -not [Uri]::TryCreate($Url, [UriKind]::Absolute, [ref]$parsed) -or
        $parsed.Scheme -ne 'https' -or -not $parsed.Host -or $parsed.UserInfo -or $parsed.Fragment) {
        throw 'Invalid installer URL: expected an absolute HTTPS address without credentials, fragments or control characters.'
    }
    # Mirror listings include literal spaces (e.g. Firefox Setup 156.0.1.msi).
    # AbsoluteUri escapes them as %20 and preserves existing percent escapes.
    return $parsed.AbsoluteUri
}
if ($DownloadConfig) {
    $custom = Get-Content -Raw -LiteralPath $DownloadConfig | ConvertFrom-Json
    if ($custom.PSObject.Properties['MinicondaUrl']) { $Sources.MinicondaFallbackUrl = '' }
    if ($custom.PSObject.Properties['WiresharkUrl']) { $Sources.WiresharkFallbackUrl = '' }
    foreach ($property in $custom.PSObject.Properties) {
        if (-not $Sources.ContainsKey($property.Name)) { throw "Unknown download setting: $($property.Name)" }
        $value = [string]$property.Value
        if ($property.Name -eq 'GeckodriverVersion') {
            if ($value -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid GeckodriverVersion.' }
        } elseif ($property.Name -eq 'ChromeVersion') {
            if ($value -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw 'Invalid ChromeVersion.' }
        } elseif ($property.Name -in @('MinicondaSha256','ChromeZipSha256','WiresharkSha256')) {
            if ($value -notmatch '^[a-fA-F0-9]{64}$') { throw "$($property.Name) must be the 64-character upstream SHA256." }
        } else { $value = ConvertTo-HttpsDownloadUrl $value }
        if (-not $value) { throw "Download setting cannot be empty: $($property.Name)" }
        $Sources[$property.Name] = $value
    }
}

function Invoke-Native {
    param([string]$File, [string[]]$Arguments)
    & $File @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$File failed (exit $LASTEXITCODE)." }
}
function Get-GitTrackedPaths {
    param([string]$GitPath, [string]$Repository, [ValidateSet('HEAD','origin/main')][string]$Revision)
    # Git quotes non-ASCII names by default. Read NUL-delimited UTF-8 directly,
    # without PowerShell 5.1's console-codepage decoding or Git's C-style quotes.
    if ($Repository.Contains('"')) { throw 'Repository path cannot contain quotes.' }
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $GitPath
    $info.Arguments = '-C "' + $Repository + '" ls-tree -r --name-only -z ' + $Revision
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = New-Object Text.UTF8Encoding($false, $true)
    $info.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    try {
        [void]$process.Start()
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill()
            $process.WaitForExit()
            throw 'Git file listing timed out.'
        }
        $text = $stdout.GetAwaiter().GetResult()
        $errorText = $stderr.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) { throw "Git file listing failed (exit $($process.ExitCode)): $errorText" }
        $text.Split([char[]]@([char]0), [StringSplitOptions]::RemoveEmptyEntries)
    } finally { $process.Dispose() }
}
function Assert-PlainPath {
    param([string]$Path)
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            if ((Get-Item -Force -LiteralPath $cursor).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Junction/symlink is not supported: $cursor"
            }
        }
        $cursor = [IO.Path]::GetDirectoryName($cursor)
    }
}
function Write-Utf8 {
    param([string]$Path, [string]$Content)
    [IO.File]::WriteAllText($Path, $Content, (New-Object Text.UTF8Encoding $false))
}
function Backup-IncompleteEnvironment {
    param([ValidateSet('miniconda','miniconda\envs\traffic-worker')][string]$RelativePath)
    $root = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\') + '\'
    $source = [IO.Path]::GetFullPath((Join-Path $InstallRoot $RelativePath))
    $destination = [IO.Path]::GetFullPath((Join-Path $BackupDir ('incomplete-' + [IO.Path]::GetFileName($source))))
    if (-not $source.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) -or
        -not $destination.StartsWith($root, [StringComparison]::OrdinalIgnoreCase) -or
        $destination.StartsWith($source + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe incomplete-environment backup path.' }
    Assert-PlainPath $source
    Assert-PlainPath $destination
    if (Test-Path -LiteralPath $source) {
        Move-Item -LiteralPath $source -Destination $destination
        Write-Warning "Preserved incomplete environment at $destination ; rebuilding."
    }
}
function Get-Metadata {
    param([string]$Url)
    # Invoke-RestMethod in Windows PowerShell emits a JSON array as one pipeline
    # object. Return its assigned value so downstream filters receive each item.
    $response = Invoke-RestMethod -Uri $Url -UseBasicParsing -TimeoutSec 60 -Headers @{'User-Agent'='TrafficWorker-Deploy'}
    return $response
}
function Get-Installer {
    param([string]$Name, [scriptblock]$ResolveUrl, [switch]$Archive, [string]$ExpectedSha256 = '')
    $path = Join-Path $CacheDir $Name
    if ($InstallerDirectory -and (Test-Path -LiteralPath (Join-Path $InstallerDirectory $Name) -PathType Leaf)) {
        $supplied = Join-Path $InstallerDirectory $Name
        Assert-InstallerFile $supplied -Archive:$Archive -ExpectedSha256 $ExpectedSha256
        if ([IO.Path]::GetFullPath($supplied) -ne [IO.Path]::GetFullPath($path)) {
            Copy-Item -LiteralPath $supplied -Destination $path -Force
        }
        return $path
    }
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        try {
            Assert-InstallerFile $path -Archive:$Archive -ExpectedSha256 $ExpectedSha256
            return $path
        } catch {
            # Preserve the rejected file for diagnosis; never keep retrying stale cache.
            $rejected = $path + '.rejected-' + [guid]::NewGuid().ToString('N')
            Write-Warning "Replacing invalid cached ${Name}: $($_.Exception.Message)"
            Move-Item -LiteralPath $path -Destination $rejected
        }
    }
    $urls = @(& $ResolveUrl | Where-Object { $_ } | ForEach-Object { ConvertTo-HttpsDownloadUrl $_ })
    if (-not $urls.Count) { throw "No download URL for $Name" }
    $download = Join-Path $CacheDir ('partial\' + $Name)
    New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($download)) -Force | Out-Null
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        foreach ($url in $urls) {
            try {
                Write-Host "Downloading $Name (attempt $attempt/3) from $url"
                Invoke-WebRequest -Uri $url -UseBasicParsing -OutFile $download -TimeoutSec 1800
                Assert-InstallerFile $download -Archive:$Archive -ExpectedSha256 $ExpectedSha256
                Move-Item -LiteralPath $download -Destination $path -Force
                return $path
            } catch {
                $lastError = $_.Exception.Message
                Write-Warning "Download/verification failed for ${Name}: $lastError"
            }
        }
        if ($attempt -lt 3) { Start-Sleep -Seconds 3 }
    }
    throw "Unable to obtain verified $Name after 3 attempts per source. Last error: $lastError"
}
function Assert-InstallerFile {
    param([string]$Path, [switch]$Archive, [string]$ExpectedSha256 = '')
    if ($ExpectedSha256 -and $ExpectedSha256 -notmatch '^[a-fA-F0-9]{64}$') { throw 'Invalid expected SHA256.' }
    $hash = (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash
    if ($ExpectedSha256 -and $hash -ne $ExpectedSha256) {
        throw "SHA256 mismatch: $Path ; expected $ExpectedSha256 ; received $hash"
    }
    if ($Archive) {
        Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($Path)
        try {
            if (-not $zip.Entries.Count) { throw "Empty ZIP: $Path" }
            foreach ($entry in $zip.Entries) {
                $stream = $entry.Open()
                try { $stream.CopyTo([IO.Stream]::Null) } finally { $stream.Dispose() }
            }
        } finally { $zip.Dispose() }
    } elseif ($ExpectedSha256) {
        # A digest pinned from upstream authenticates these exact bytes even if a
        # restricted server cannot validate the Windows timestamp/certificate chain.
        $signature = Get-AuthenticodeSignature -LiteralPath $Path
        if ($signature.Status -ne 'Valid') {
            Write-Warning "Authenticode: $($signature.Status): $($signature.StatusMessage). Accepted ONLY because the upstream pinned SHA256 matches."
        }
        Write-Host "Verified upstream SHA256: $([IO.Path]::GetFileName($Path))"
    } else {
        Assert-SignedInstaller $Path
    }
    Write-Host "SHA256 $([IO.Path]::GetFileName($Path)): $hash"
}
function Get-BinaryListing {
    param([string]$RelativePath)
    $url = $Sources.BinaryMirror.TrimEnd('/') + '/' + $RelativePath.Trim('/') + '/'
    $items = Get-Metadata $url
    foreach ($item in $items) {
        if ($null -eq $item -or -not $item.PSObject.Properties['name'] -or $item.name -isnot [string]) {
            throw "Mirror did not return a file listing with name fields: $url"
        }
        $item
    }
}
function Resolve-GitMirrorUrl {
    if ($Sources.GitUrl) { return $Sources.GitUrl }
    $releases = @(Get-BinaryListing 'git-for-windows' | Where-Object name -match '^v\d+\.\d+\.\d+\.windows\.\d+/$' |
        Sort-Object { [version](($_.name.TrimEnd('/') -replace '^v','') -replace '\.windows','') } -Descending)
    if (-not $releases.Count) { throw 'Git mirror contains no stable releases; supply git.exe with -InstallerDirectory.' }
    $release = $releases[0]
    $asset = @(Get-BinaryListing ('git-for-windows/' + $release.name) | Where-Object name -match '^Git-[\d.]+-64-bit\.exe$')
    if ($asset.Count -ne 1) { throw 'Git mirror is incomplete; supply git.exe with -InstallerDirectory.' }
    return $asset[0].url
}
function Resolve-FirefoxMirrorUrl {
    if ($Sources.FirefoxMsiUrl) { return $Sources.FirefoxMsiUrl }
    $releases = @(Get-BinaryListing 'firefox' | Where-Object name -match '^\d+\.\d+(\.\d+)?/$' |
        Sort-Object { [version]$_.name.TrimEnd('/') } -Descending)
    if (-not $releases.Count) { throw 'Firefox mirror contains no stable releases; supply firefox.msi with -InstallerDirectory.' }
    $release = $releases[0]
    $asset = @(Get-BinaryListing ('firefox/' + $release.name + 'win64/zh-CN') | Where-Object name -match '^Firefox Setup [\d.]+\.msi$')
    if ($asset.Count -ne 1) { throw 'Firefox mirror is incomplete; supply firefox.msi with -InstallerDirectory.' }
    return $asset[0].url
}
function Expand-SafeArchive {
    param([string]$ArchivePath, [string]$Destination)
    Add-Type -AssemblyName System.IO.Compression, System.IO.Compression.FileSystem
    $prefix = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    Assert-PlainPath $Destination
    $zip = [IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath((Join-Path $Destination $entry.FullName))
            if (-not $target.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) -or $entry.FullName.Contains(':')) { throw 'Unsafe ZIP entry.' }
            Assert-PlainPath $target
            if ($entry.FullName.EndsWith('/')) { continue }
            New-Item -ItemType Directory -Path ([IO.Path]::GetDirectoryName($target)) -Force | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
        }
    } finally { $zip.Dispose() }
}
function Get-BrowserVersion {
    param([string]$Path)
    $text = (Get-Item -LiteralPath $Path).VersionInfo.ProductVersion
    $match = [regex]::Match($text, '\d+\.\d+\.\d+\.\d+')
    if (-not $match.Success) { throw "Cannot determine browser version: $Path" }
    return $match.Value
}
function Install-ManagedChrome {
    # Re-extract the verified ZIP on reruns as well: a previous failed extraction
    # can leave chrome.exe present while its DLLs/resources are still missing.
    $archive = Get-Installer 'chrome-win64.zip' {
        if ($Sources.ChromeZipUrl) { $Sources.ChromeZipUrl } else {
            $Sources.BinaryMirror.TrimEnd('/') + '/chrome-for-testing/' + $Sources.ChromeVersion + '/win64/chrome-win64.zip'
        }
    } -Archive -ExpectedSha256 $Sources.ChromeZipSha256
    Expand-SafeArchive $archive $BrowserDir
    $binary = Join-Path $BrowserDir 'chrome-win64\chrome.exe'
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf) -or (Get-BrowserVersion $binary) -ne $Sources.ChromeVersion) {
        throw 'Verified Chrome archive does not contain the configured ChromeVersion.'
    }
    return $binary
}
function Get-CompatibleDriver {
    param([string]$Browser, [string]$BrowserPath)
    $names = @{chrome='chromedriver'; edge='msedgedriver'; firefox='geckodriver'}
    $name = $names[$Browser]
    if ($Browser -eq 'firefox') {
        $version = $Sources.GeckodriverVersion
        $archiveName = "geckodriver-v$version-win64.zip"
        $relativeUrl = "geckodriver/v$version/$archiveName"
        $overrideUrl = $Sources.GeckodriverUrl
    } else {
        $browserVersion = Get-BrowserVersion $BrowserPath
        $build = ($browserVersion.Split('.')[0..2] -join '.')
        $overrideUrl = if ($Browser -eq 'chrome') { $Sources.ChromedriverUrl } else { $Sources.EdgedriverUrl }
        # Local drivers take precedence and can be used without any metadata requests.
        $version = $browserVersion
    }
    foreach ($candidate in @($(if ($InstallerDirectory) { Join-Path $InstallerDirectory "$name.exe" }), (Join-Path $DriverDir "$name.exe"))) {
        if (-not $candidate -or -not (Test-Path -LiteralPath $candidate -PathType Leaf)) { continue }
        $reported = [string](Invoke-Native $candidate @('--version'))
        $match = [regex]::Match($reported, '\d+\.\d+\.\d+(\.\d+)?')
        $compatible = $match.Success -and $(if ($Browser -eq 'firefox') { $match.Value -eq $version } else { ($match.Value.Split('.')[0..2] -join '.') -eq $build })
        if (-not $compatible) {
            if ($InstallerDirectory -and $candidate.StartsWith($InstallerDirectory, [StringComparison]::OrdinalIgnoreCase)) { throw "Supplied $name.exe does not match the browser/GeckodriverVersion." }
            continue
        }
        $destination = Join-Path $DriverDir "$name.exe"
        if ([IO.Path]::GetFullPath($candidate) -ne $destination) { Copy-Item -LiteralPath $candidate -Destination $destination -Force }
        return $destination
    }
    if ($Browser -eq 'chrome') {
        if (-not $overrideUrl) {
            $metadata = Get-Metadata ($Sources.BinaryMirror.TrimEnd('/') + '/chrome-for-testing/latest-patch-versions-per-build.json')
            $property = $metadata.builds.PSObject.Properties[$build]
            if (-not $property) { throw "Chrome $browserVersion has not reached the mirror. Supply matching chromedriver.exe with -InstallerDirectory." }
            $version = $property.Value.version
            if ($version -notmatch '^\d+\.\d+\.\d+\.\d+$' -or ($version.Split('.')[0..2] -join '.') -ne $build) { throw 'Invalid ChromeDriver version in mirror metadata.' }
        }
        $archiveName = "chromedriver-$version-win64.zip"
        $relativeUrl = "chrome-for-testing/$version/win64/chromedriver-win64.zip"
    } elseif ($Browser -eq 'edge') {
        $archiveName = "edgedriver-$version-win64.zip"
        $relativeUrl = "edgedriver/$version/edgedriver_win64.zip"
    }
    $url = if ($overrideUrl) { $overrideUrl } else { $Sources.BinaryMirror.TrimEnd('/') + '/' + $relativeUrl }
    $archive = Get-Installer $archiveName { $url } -Archive
    $unpack = Join-Path $CacheDir ($archiveName + '.unpacked')
    Expand-SafeArchive $archive $unpack
    $binaries = @(Get-ChildItem -LiteralPath $unpack -Recurse -File -Filter "$name.exe")
    if ($binaries.Count -ne 1) { throw "Driver archive must contain exactly one $name.exe" }
    $reported = [string](Invoke-Native $binaries[0].FullName @('--version'))
    $match = [regex]::Match($reported, '\d+\.\d+\.\d+(\.\d+)?')
    $compatible = $match.Success -and $(if ($Browser -eq 'firefox') { $match.Value -eq $version } else { ($match.Value.Split('.')[0..2] -join '.') -eq $build })
    if (-not $compatible) { throw "Unexpected driver version in $archiveName" }
    $destination = Join-Path $DriverDir "$name.exe"
    Copy-Item -LiteralPath $binaries[0].FullName -Destination $destination -Force
    Write-Host "Installed local $name $version from mirror."
    return $destination
}
function Assert-SignedInstaller {
    param([string]$Path)
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid') {
        throw "Installer signature invalid: $Path ($($signature.Status)): $($signature.StatusMessage). Check server clock and certificate-chain connectivity; unsigned/unverified files are not installed."
    }
    Write-Host "Verified installer: $([IO.Path]::GetFileName($Path)); publisher: $($signature.SignerCertificate.Subject)"
}
function Install-PackageFile {
    param([string]$Path, [string]$Arguments)
    $program = $Path
    if ([IO.Path]::GetExtension($Path) -eq '.msi') {
        $program = "$env:SystemRoot\System32\msiexec.exe"
        $Arguments = "/i `"$Path`" /qn /norestart /L*v `"$Path.install.log`" $Arguments"
    }
    $process = Start-Process -FilePath $program -ArgumentList $Arguments -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -in @(1641, 3010)) {
        throw 'Installer requires a reboot. Reboot manually, then rerun this script.'
    }
    if ($process.ExitCode -ne 0) { throw "Installer failed: $Path (exit $($process.ExitCode))." }
}
function Find-MachineFile {
    param([string]$RelativePath)
    foreach ($base in @($env:ProgramW6432, ${env:ProgramFiles(x86)})) {
        if ($base) {
            $candidate = Join-Path $base $RelativePath
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    return $null
}
function Get-LocalHealth {
    $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:5100/api/v1/health')
    $request.Proxy = $null
    $request.Timeout = 10000
    $request.ReadWriteTimeout = 10000
    $response = $request.GetResponse()
    $reader = New-Object IO.StreamReader($response.GetResponseStream())
    try { return ($reader.ReadToEnd() | ConvertFrom-Json) }
    finally { $reader.Dispose(); $response.Dispose() }
}
function Get-Listeners {
    @(Get-NetTCPConnection -State Listen -LocalPort 5100 -ErrorAction SilentlyContinue)
}
function Get-WorkerTaskCredential {
    param([System.Management.Automation.PSCredential]$Credential, [string]$UserName, [string]$UserSid)
    # Edge does not support LocalSystem. Use the deploying administrator's real
    # Windows logon, with Task Scheduler's protected password storage for reboot.
    if ($UserSid -in @('S-1-5-18','S-1-5-19','S-1-5-20')) { throw 'Run deployment from an administrator user session, not a service account.' }
    if (-not $Credential) {
        $Credential = Get-Credential -UserName $UserName -Message 'Worker startup account: enter this Windows account password (not PIN). Edge cannot run as SYSTEM.'
    }
    if (-not $Credential -or $Credential.Password.Length -eq 0) { throw 'A Windows account password is required for startup while logged off.' }
    $account = New-Object Security.Principal.NTAccount($Credential.UserName)
    $sid = $account.Translate([Security.Principal.SecurityIdentifier]).Value
    if ($sid -ne $UserSid) { throw 'Use the same administrator account that is running this elevated PowerShell.' }
    return $Credential
}
function Register-WorkerStartupTask {
    param([System.Management.Automation.PSCredential]$Credential)
    if (-not $Credential) { throw 'Worker task credential is required.' }
    $action = New-ScheduledTaskAction -Execute $Python -Argument "-u `"$Runner`"" -WorkingDirectory $ProjectDir
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $Credential.UserName -LogonType Password -RunLevel Highest
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $definition = New-ScheduledTask -Action $action -Trigger $trigger -Principal $taskPrincipal -Settings $settings -Description $TaskDescription
    # Pass directly to Windows; never persist a plaintext password in runtime.json,
    # the runner, command-line arguments, or deployment logs.
    Register-ScheduledTask -TaskName $TaskName -TaskPath '\' -InputObject $definition -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -Force | Out-Null
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal $identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Open 64-bit PowerShell as Administrator, then run this script again.'
}
if (-not [Environment]::Is64BitProcess -or $env:PROCESSOR_ARCHITECTURE -ne 'AMD64') { throw 'Windows x64 and 64-bit PowerShell are required.' }
$os = Get-CimInstance Win32_OperatingSystem
if ([int]$os.BuildNumber -lt 17763) { throw 'Requires Windows Server 2019+ (Desktop Experience) or Windows 10 1809+ / 11.' }
$installationType = (Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').InstallationType
if ($installationType -eq 'Server Core') { throw 'Server Core is not supported; use Windows Server with Desktop Experience.' }
$smokeUri = [uri]$SmokeUrl
if (-not $smokeUri.IsAbsoluteUri -or $smokeUri.Scheme -ne 'https' -or $smokeUri.UserInfo) { throw 'SmokeUrl must be HTTPS without credentials.' }
if ($MasterIP) {
    $parsedIP = $null
    if (-not [Net.IPAddress]::TryParse($MasterIP, [ref]$parsedIP) -or $parsedIP.AddressFamily -ne 'InterNetwork') { throw 'MasterIP must be one IPv4 address.' }
}
if ($InstallRoot -notmatch '^[A-Za-z]:\\' -or $InstallRoot -match '["\r\n;]') { throw 'InstallRoot must be an absolute local drive path without quotes, newlines or semicolons.' }
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
if ($InstallRoot.Length -le 3) { throw 'A drive root cannot be the installation directory.' }
Assert-PlainPath $InstallRoot
if ($PSCommandPath.StartsWith($InstallRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Copy this script outside InstallRoot before running it.' }
$TaskCredential = Get-WorkerTaskCredential $TaskCredential $identity.Name $identity.User.Value
$ProjectDir = Join-Path $InstallRoot 'project'
$CondaRoot = Join-Path $InstallRoot 'miniconda'
$CondaEnv = Join-Path $CondaRoot 'envs\traffic-worker'
$Python = Join-Path $CondaEnv 'python.exe'
$RunDir = Join-Path $InstallRoot 'run'
$DriverDir = Join-Path $InstallRoot 'drivers'
$BrowserDir = Join-Path $InstallRoot 'browsers'
$Runner = Join-Path $RunDir 'run_worker.py'
$ConfigFile = Join-Path $ProjectDir 'worker.yaml'
$CacheDir = Join-Path $InstallRoot 'installers'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss-fff'
$BackupDir = Join-Path $InstallRoot "backups\$stamp"
$LogDir = Join-Path $InstallRoot 'logs'
$marker = Join-Path $InstallRoot 'windows-worker-deployment.json'
$lock = New-Object Threading.Mutex($false, 'Global\TrafficCaptureWorkerDeployment')
$locked = $false
$transcript = $false
try {
    try { $locked = $lock.WaitOne(0) } catch [Threading.AbandonedMutexException] { $locked = $true }
    if (-not $locked) { throw 'Another Windows Worker deployment is running.' }
    # Do not adopt an arbitrary existing directory or change its permissions.
    if ((Test-Path -LiteralPath $InstallRoot) -and -not (Test-Path -LiteralPath $marker)) {
        if (@(Get-ChildItem -Force -LiteralPath $InstallRoot).Count) { throw 'InstallRoot is nonempty and unmanaged. Choose a new dedicated directory.' }
    }
    foreach ($path in @($ProjectDir, $CondaRoot, $CondaEnv, $RunDir, $CacheDir, $BackupDir, $LogDir, $DriverDir, $BrowserDir, $marker, $ConfigFile)) { Assert-PlainPath $path }
    if (Test-Path -LiteralPath $marker) {
        if ((Get-Content -Raw -LiteralPath $marker | ConvertFrom-Json).managed_by -ne 'deploy_worker_windows.ps1') { throw 'Unknown deployment marker.' }
    }
    New-Item -ItemType Directory -Path $InstallRoot -Force | Out-Null
    # The elevated task runs downloaded project code: only administrators and SYSTEM may write here.
    Invoke-Native "$env:SystemRoot\System32\icacls.exe" @($InstallRoot, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null
    foreach ($path in @($RunDir, $CacheDir, $BackupDir, $LogDir, $DriverDir, $BrowserDir)) { New-Item -ItemType Directory -Path $path -Force | Out-Null }
    if (-not (Test-Path -LiteralPath $marker)) { Write-Utf8 $marker '{"managed_by":"deploy_worker_windows.ps1"}' }
    Start-Transcript -Path (Join-Path $LogDir "deploy-$stamp.log") | Out-Null
    $transcript = $true
    Write-Host 'Windows Worker deployment revision: 2026-09-25.8 (simple HTTPS capture acceptance)'
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    $env:GIT_TERMINAL_PROMPT = '0'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONUNBUFFERED = '1'
    $env:PIP_CONFIG_FILE = 'NUL'
    $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
    $env:PIP_INDEX_URL = $Sources.PipIndexUrl
    foreach ($name in @('PIP_EXTRA_INDEX_URL','PIP_TRUSTED_HOST','PIP_FIND_LINKS')) { Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue }
    Write-Host "Conda mirror: $($Sources.CondaChannel) | pip mirror: $($Sources.PipIndexUrl)"
    # YAML is authoritative for this deployment, even in a previously configured shell.
    foreach ($name in @('PYTHONHOME','PYTHONPATH','PROJECT_ROOT','PYTHON_EXECUTABLE','CHROME_BINARY','EDGE_BINARY','FIREFOX_BINARY','MAX_QUEUE_SIZE','MAX_ITEMS','TASK_TIMEOUT_SECONDS','MAX_CONTENT_LENGTH') + @(Get-ChildItem Env: | Where-Object Name -like 'WORKER_*' | Select-Object -ExpandProperty Name)) {
        Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
    }
    Write-Host 'Checking managed task and active work...'
    $oldTask = Get-ScheduledTask -TaskName $TaskName -TaskPath '\' -ErrorAction SilentlyContinue
    if ($oldTask) {
        if ($oldTask.Description -ne $TaskDescription -or @($oldTask.Actions).Count -ne 1 -or
            $oldTask.Actions[0].Execute -ne $Python -or $oldTask.Actions[0].Arguments -ne "-u `"$Runner`"") {
            throw 'Existing scheduled task is not owned by this deployment directory.'
        }
        if ($oldTask.State -eq 'Running' -and -not $AllowInterrupt) {
            $health = Get-LocalHealth
            if ($health.status -ne 'ok' -or $health.busy -or $health.queue_size -gt 0) { throw 'Worker has pending work. Wait, or explicitly use -AllowInterrupt.' }
        }
        Export-ScheduledTask -TaskName $TaskName -TaskPath '\' | Set-Content -LiteralPath (Join-Path $BackupDir 'scheduled-task.xml') -Encoding UTF8
        Disable-ScheduledTask -TaskName $TaskName -TaskPath '\' | Out-Null
        Stop-ScheduledTask -TaskName $TaskName -TaskPath '\'
        for ($i = 0; $i -lt 30 -and @(Get-Listeners).Count; $i++) { Start-Sleep -Seconds 1 }
    }
    if (@(Get-Listeners).Count) { throw 'Port 5100 is occupied. Stop the manually launched Worker before deployment.' }
    if (Test-Path -LiteralPath $ConfigFile) { Copy-Item -LiteralPath $ConfigFile -Destination $BackupDir }
    foreach ($file in @('run_worker.py','runtime.json')) {
        if (Test-Path -LiteralPath (Join-Path $RunDir $file)) { Copy-Item -LiteralPath (Join-Path $RunDir $file) -Destination $BackupDir }
    }

    if ($RepairTaskOnly) {
        if (-not $oldTask) { throw 'RepairTaskOnly requires an existing managed Worker task.' }
        $RuntimeFile = Join-Path $RunDir 'runtime.json'
        $helperPath = Join-Path $RunDir 'deploy_helper.py'
        foreach ($file in @($Python, $Runner, $RuntimeFile, $helperPath, $ConfigFile)) {
            Assert-PlainPath $file
            if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "RepairTaskOnly requires existing deployment file: $file" }
        }
        $repairRuntime = Get-Content -LiteralPath $RuntimeFile -Raw | ConvertFrom-Json
        $repairRuntime.skip_smoke = [bool]$SkipSmoke
        $repairRuntime.smoke_url = $SmokeUrl
        $repairRuntime.smoke_timeout = $SmokeTimeout
        Write-Utf8 $RuntimeFile ($repairRuntime | ConvertTo-Json -Depth 5)
        Register-WorkerStartupTask $TaskCredential
        Start-ScheduledTask -TaskName $TaskName -TaskPath '\'
        Invoke-Native $Python @($helperPath, 'verify', $RuntimeFile)
        $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\'
        if ($task.State -ne 'Running' -or -not $task.Settings.Enabled) { throw 'Scheduled task is not running/enabled.' }
        Write-Host "Worker task account repaired and verification passed. Account: $($TaskCredential.UserName)"
        return
    }

    Write-Host 'Installing/reusing Git...'
    $Git = Find-MachineFile 'Git\cmd\git.exe'
    if (-not $Git) {
        $installer = Get-Installer 'git.exe' { Resolve-GitMirrorUrl }
        Install-PackageFile $installer '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP- /ALLUSERS'
        $Git = Find-MachineFile 'Git\cmd\git.exe'
    }
    if (-not $Git) { throw 'Machine-wide Git installation was not found.' }
    Invoke-Native $Git @('--version')

    Write-Host 'Checking Git checkout...'
    if (Test-Path -LiteralPath $ProjectDir) {
        if (-not (Test-Path -LiteralPath "$ProjectDir\.git" -PathType Container)) { throw 'Project directory is not a managed Git checkout; automatic migration is not supported.' }
        Assert-PlainPath "$ProjectDir\.git"
        if ((Invoke-Native $Git @('-C',$ProjectDir,'remote','get-url','origin')) -ne $RepoUrl) { throw 'Existing origin does not match Gitee repository.' }
        if ((Invoke-Native $Git @('-C',$ProjectDir,'branch','--show-current')) -ne 'main') { throw 'Checkout must be on main.' }
        if (Invoke-Native $Git @('-C',$ProjectDir,'status','--porcelain','--untracked-files=no')) { throw 'Tracked files have local changes. Commit or stash them deliberately.' }
        Invoke-Native $Git @('-C',$ProjectDir,'rev-parse','HEAD') | Set-Content -LiteralPath (Join-Path $BackupDir 'previous-commit.txt')
        Invoke-Native $Git @('-C',$ProjectDir,'fetch','origin','main')
        Invoke-Native $Git @('-C',$ProjectDir,'merge-base','--is-ancestor','HEAD','origin/main')
    } else {
        Invoke-Native $Git @('clone','--branch','main','--single-branch',$RepoUrl,$ProjectDir)
    }
    if ((Test-Path -LiteralPath "$ProjectDir\master.yaml") -or (Test-Path -LiteralPath "$ProjectDir\master.yml")) { throw 'Master configuration found: use a separate Worker checkout.' }
    $tracked = @(Get-GitTrackedPaths $Git $ProjectDir 'HEAD') + @(Get-GitTrackedPaths $Git $ProjectDir 'origin/main')
    $tree = @(Invoke-Native $Git @('-C',$ProjectDir,'ls-tree','-r','origin/main'))
    if ($tree -match '^(120000|160000) ') { throw 'Deployment source contains symlinks or submodules; inspect before updating.' }
    foreach ($file in $tracked) {
        if ($file -match '(^|/)(worker\.ya?ml|master\.ya?ml|\.env[^/]*)$|^(worker_data|master_data)/') { throw "Repository tracks runtime configuration/data: $file" }
        Assert-PlainPath (Join-Path $ProjectDir $file)
    }
    Write-Utf8 (Join-Path $RunDir 'tracked.json') (ConvertTo-Json -InputObject @($tracked))

    Write-Host 'Installing/reusing Miniconda and Python 3.12...'
    $Conda = Join-Path $CondaRoot 'Scripts\conda.exe'
    if (-not (Test-Path -LiteralPath $Conda -PathType Leaf)) {
        $installer = Get-Installer 'miniconda.exe' { $Sources.MinicondaUrl; $Sources.MinicondaFallbackUrl } -ExpectedSha256 $Sources.MinicondaSha256
        Backup-IncompleteEnvironment 'miniconda'
        Install-PackageFile $installer "/S /InstallationType=AllUsers /RegisterPython=0 /AddToPath=0 /D=$CondaRoot"
    }
    if (-not (Test-Path -LiteralPath $Conda -PathType Leaf)) { throw 'Miniconda installer completed without creating conda.exe.' }
    # A fresh PowerShell has never activated Conda: provide its DLL/search paths
    # before creating the child environment (SSL/libmamba need Library\bin).
    $env:PATH = "$CondaRoot;$CondaRoot\Scripts;$CondaRoot\Library\bin;" + $env:PATH
    Invoke-Native $Conda @('--version')
    if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
        Backup-IncompleteEnvironment 'miniconda\envs\traffic-worker'
        Invoke-Native $Conda @('create','--yes','--prefix',$CondaEnv,'--override-channels','--channel',$Sources.CondaChannel,'python=3.12','pip')
    }
    $env:PATH = "$CondaEnv;$CondaEnv\Scripts;$CondaEnv\Library\bin;" + $env:PATH
    Invoke-Native $Python @('-c','import sys; assert sys.version_info >= (3,10); print(sys.version)')
    Invoke-Native $Python @('-m','pip','install','--index-url',$Sources.PipIndexUrl,'PyYAML>=6.0,<7')

    # Embedded helper keeps this script independently uploadable to a clean server.
    $helper = @'
import json, os, secrets, sys, time, uuid
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler
import yaml

def require(condition, message):
    if not condition:
        raise RuntimeError(message)

def prepare(runtime, check_only=False):
    root = Path(runtime['project'])
    config = root / 'worker.yaml'
    d = yaml.safe_load(config.read_text(encoding='utf-8-sig')) if config.exists() else {}
    if d is None:
        d = {}
    require(isinstance(d, dict), 'worker.yaml must be a mapping')
    def section(name):
        value = d.setdefault(name, {})
        require(isinstance(value, dict), 'Invalid YAML section: ' + name)
        return value
    paths = section('paths')
    data = Path(os.path.expandvars(str(paths.get('data_dir') or './worker_data'))).expanduser()
    data = data if data.is_absolute() else root / data
    for path in (data, *data.parents):
        require(not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)(), 'Data directory must not use symlinks/junctions')
    data = data.resolve()
    for name in json.loads((Path(runtime['run']) / 'tracked.json').read_text(encoding='utf-8')):
        source = (root / name).resolve()
        require(source != data and data not in source.parents and source not in data.parents,
                'Configured data directory overlaps tracked source: ' + name)
    for protected in (root / '.git', Path(runtime['run']), Path(runtime['python']).parents[2]):
        protected = protected.resolve()
        require(data != protected and protected not in data.parents and data not in protected.parents,
                'Data directory overlaps deployment internals')
    if check_only:
        return
    w = section('worker')
    w['id'] = runtime['worker_id'] or w.get('id') or ('win-' + os.environ.get('COMPUTERNAME', 'worker'))
    w.update(host='0.0.0.0', port=5100)
    if runtime['rotate_token'] or not w.get('token') or w.get('token') in ('dev-worker-token', 'replace-with-a-long-random-token'):
        w['token'] = secrets.token_hex(32)
    paths.update(project_root=str(root), python_executable=runtime['python'], data_dir=str(data))
    for key, value in dict(max_queue_size=10, max_items=100000, task_timeout_seconds=0, max_content_length=268435456).items():
        section('limits').setdefault(key, value)
    section('browsers').update({b + '_binary': p for b, p in runtime['browsers'].items()})
    section('network').setdefault('proxy_url', None)
    tmp = config.with_suffix('.yaml.deploy-tmp')
    tmp.write_text(yaml.safe_dump(d, allow_unicode=True, sort_keys=False), encoding='utf-8')
    # Validate with the application's real schema before replacing a working configuration.
    sys.path.insert(0, str(root))
    os.environ['WORKER_CONFIG_FILE'] = str(tmp)
    from worker_agent.config import WorkerConfig
    WorkerConfig.from_env()
    data.mkdir(parents=True, exist_ok=True)
    tmp.replace(config)
    print('Configuration prepared. Token is in worker.yaml (not printed).', flush=True)

def verify(runtime):
    config = Path(runtime['project']) / 'worker.yaml'
    d = yaml.safe_load(config.read_text(encoding='utf-8'))
    opener = build_opener(ProxyHandler({}))
    def api(path, body=None, authenticated=True):
        headers = {'Content-Type': 'application/json'}
        if authenticated:
            headers['Authorization'] = 'Bearer ' + d['worker']['token']
        request = Request('http://127.0.0.1:5100/api/v1' + path,
                          data=None if body is None else json.dumps(body).encode(), headers=headers)
        with opener.open(request, timeout=15) as response:
            return json.load(response)
    for attempt in range(60):
        try:
            h = api('/health')
            if h.get('status') == 'ok':
                break
        except OSError:
            pass
        time.sleep(2)
    else:
        raise RuntimeError('Worker startup failed; inspect logs/worker.stderr.log')
    require(h['worker_id'] == d['worker']['id'], 'Unexpected Worker identity')
    try:
        api('/capabilities', authenticated=False)
    except HTTPError as exc:
        require(exc.code == 401, 'Unexpected unauthenticated API status')
    else:
        raise RuntimeError('API accepted an unauthenticated request')
    caps = api('/capabilities')
    require(Path(caps['python']['executable']).resolve() == Path(runtime['python']).resolve(), 'Wrong task Python')
    require(caps['capture']['pcap'], 'TShark not detected')
    found = {b['name']: Path(b['path']).resolve() for b in caps['browsers']}
    for browser, path in runtime['browsers'].items():
        require(found.get(browser) == Path(path).resolve(), 'Wrong browser binary: ' + browser)
    print('PASS: health, authentication, Python and browser paths.', flush=True)
    if runtime['skip_smoke']:
        print('SKIPPED: real captures NOT verified.', flush=True)
        return
    task_id = 'deploy-smoke-' + uuid.uuid4().hex
    browsers = list(runtime['browsers'])
    task_dir = Path(d['paths']['data_dir']) / 'tasks' / task_id
    api('/tasks', {'task_id': task_id, 'items': [{'id': '1', 'name': 'deployment-test', 'url': runtime['smoke_url']}],
                   'browsers': browsers, 'pcap': True, 'outputs': {'html': False, 'reports': False},
                   'analysis': {'steps': [], 'with_coframe': False, 'sni_suffixes': []}})
    print('Capture task:', task_id, '\nTask log:', task_dir / 'worker.log', flush=True)
    done = False
    try:
        deadline = time.monotonic() + runtime['smoke_timeout']
        last = None
        while time.monotonic() < deadline:
            status = api('/tasks/' + task_id + '?include_result=false')['status']
            if status != last:
                print('Capture status:', status, flush=True)
                last = status
            if status in {'SUCCEEDED', 'PARTIAL', 'FAILED', 'CANCELED', 'INTERRUPTED'}:
                done = True
                require(status == 'SUCCEEDED', 'Capture failed: ' + status)
                manifest = json.loads((task_dir / 'manifest.json').read_text(encoding='utf-8'))
                units = manifest.get('units', [])
                require(len(units) == len(browsers) and all(u.get('status') == 'SUCCEEDED' for u in units), 'Incomplete manifest units')
                require({u.get('browser') for u in units} == set(browsers), 'Missing browser units')
                for browser in browsers:
                    require(any(p.stat().st_size > 0 for p in task_dir.rglob('tls_keys_' + browser + '.log')), 'Missing TLS keylog: ' + browser)
                    require(any(p.stat().st_size > 24 for p in task_dir.rglob('capture_' + browser + '.pcap')), 'Missing PCAP: ' + browser)
                print('PASS: Chrome, Edge, Firefox captures; manifest, PCAP and TLS keylogs verified.', flush=True)
                return
            time.sleep(3)
        raise TimeoutError('Capture acceptance timed out')
    finally:
        if not done:
            try:
                api('/tasks/' + task_id + '/cancel', {})
            except Exception as exc:
                print('Could not cancel deployment test:', type(exc).__name__, flush=True)

if __name__ == '__main__':
    runtime = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    {'check': lambda r: prepare(r, check_only=True), 'prepare': prepare, 'verify': verify}[sys.argv[1]](runtime)
'@
    Write-Utf8 (Join-Path $RunDir 'deploy_helper.py') $helper

    Write-Host 'Installing/reusing browsers and Wireshark...'
    $chrome = Find-MachineFile 'Google\Chrome\Application\chrome.exe'
    if (-not $chrome) {
        if ($InstallerDirectory -and (Test-Path -LiteralPath (Join-Path $InstallerDirectory 'chrome.msi') -PathType Leaf)) {
            Install-PackageFile (Get-Installer 'chrome.msi' { throw 'Supply chrome.msi locally.' }) ''
            $chrome = Find-MachineFile 'Google\Chrome\Application\chrome.exe'
        } else {
            $chrome = Install-ManagedChrome
        }
    }
    $edge = Find-MachineFile 'Microsoft\Edge\Application\msedge.exe'
    if (-not $edge) {
        $installer = Get-Installer 'edge.msi' {
            if ($Sources.EdgeMsiUrl) { return $Sources.EdgeMsiUrl }
            Write-Host 'Edge MSI: using Microsoft official CDN (no verified mainland mirror configured).'
            $products = Get-Metadata 'https://edgeupdates.microsoft.com/api/products?view=enterprise'
            $release = @($products | Where-Object Product -eq 'Stable' | ForEach-Object { $_.Releases } |
                Where-Object { $_.Platform -eq 'Windows' -and $_.Architecture -eq 'x64' } |
                Sort-Object { [version]$_.ProductVersion } -Descending)[0]
            $artifact = @($release.Artifacts | Where-Object ArtifactName -eq 'msi')[0]
            $artifact.Location
        }
        Install-PackageFile $installer ''
        $edge = Find-MachineFile 'Microsoft\Edge\Application\msedge.exe'
    }
    $firefox = Find-MachineFile 'Mozilla Firefox\firefox.exe'
    if (-not $firefox) {
        Install-PackageFile (Get-Installer 'firefox.msi' { Resolve-FirefoxMirrorUrl }) ''
        $firefox = Find-MachineFile 'Mozilla Firefox\firefox.exe'
    }
    if (-not $chrome -or -not $edge -or -not $firefox) { throw 'A browser installation is missing.' }
    $tshark = Find-MachineFile 'Wireshark\tshark.exe'
    if (-not $tshark) {
        $installer = Get-Installer 'wireshark.exe' { $Sources.WiresharkUrl; $Sources.WiresharkFallbackUrl } -ExpectedSha256 $Sources.WiresharkSha256
        Install-PackageFile $installer '/S /desktopicon=no'
        $tshark = Find-MachineFile 'Wireshark\tshark.exe'
    }
    if (-not $tshark) { throw 'TShark installation missing.' }
    $npcap = Get-Service -Name npcap -ErrorAction SilentlyContinue
    if (-not $npcap) {
        if ($NpcapOemInstaller) {
            $npcapPath = (Resolve-Path -LiteralPath $NpcapOemInstaller).Path
            Assert-SignedInstaller $npcapPath
            Install-PackageFile $npcapPath '/S'
        } elseif ($InteractiveNpcap) {
            $npcapPath = Get-Installer 'npcap.exe' {
                if ($Sources.NpcapUrl) { return $Sources.NpcapUrl }
                Write-Host 'Npcap: using official source. Supply npcap.exe with -InstallerDirectory if unreachable.'
                $page = (Invoke-WebRequest 'https://npcap.com/' -UseBasicParsing -TimeoutSec 60).Content
                $match = [regex]::Match($page, 'href="(https://npcap\.com/)?(dist/npcap-[0-9.]+\.exe)"')
                if (-not $match.Success) { throw 'Cannot resolve Npcap installer; provide installers/npcap.exe.' }
                'https://npcap.com/' + $match.Groups[2].Value
            }
            Write-Host 'Complete the Npcap installation wizard. This is the one interactive installation step.'
            # Explicit -InteractiveNpcap authorizes a visible installation wizard.
            $process = Start-Process -FilePath $npcapPath -PassThru -Wait
            if ($process.ExitCode -ne 0) { throw "Npcap installer exited $($process.ExitCode); reboot if requested and rerun." }
        } else {
            throw 'Npcap is missing. Rerun in an RDP desktop with -InteractiveNpcap, or provide a licensed -NpcapOemInstaller. Other installed components will be reused.'
        }
    }
    $npcap = Get-Service -Name npcap -ErrorAction Stop
    if ($npcap.Status -ne 'Running') { Start-Service npcap }
    $interfaces = @(Invoke-Native $tshark @('-D'))
    if (-not ($interfaces -match '\\Device\\NPF_')) { throw 'No Npcap capture adapter detected. Reboot if the driver installer requested it.' }

    Write-Host 'Installing/reusing matching drivers from the domestic mirror...'
    $drivers = @{
        CHROMEDRIVER_PATH = Get-CompatibleDriver 'chrome' $chrome
        EDGEDRIVER_PATH = Get-CompatibleDriver 'edge' $edge
        GECKODRIVER_PATH = Get-CompatibleDriver 'firefox' $firefox
    }

    $runtime = [ordered]@{
        project=$ProjectDir; python=$Python; run=$RunDir; logs=$LogDir; tshark=$tshark
        browsers=@{chrome=$chrome; edge=$edge; firefox=$firefox}
        drivers=$drivers
        worker_id=$WorkerId; rotate_token=[bool]$RotateToken
        skip_smoke=[bool]$SkipSmoke; smoke_url=$SmokeUrl; smoke_timeout=$SmokeTimeout
    }
    $RuntimeFile = Join-Path $RunDir 'runtime.json'
    Write-Utf8 $RuntimeFile ($runtime | ConvertTo-Json -Depth 5)
    # Check configured data/source overlap before allowing Git to modify the checkout.
    Invoke-Native $Python @((Join-Path $RunDir 'deploy_helper.py'),'check',$RuntimeFile)
    Invoke-Native $Git @('-C',$ProjectDir,'merge','--ff-only','origin/main')
    Invoke-Native $Git @('-C',$ProjectDir,'rev-parse','HEAD') | Set-Content -LiteralPath (Join-Path $BackupDir 'target-commit.txt')
    Invoke-Native $Python @('-m','pip','install','--index-url',$Sources.PipIndexUrl,'-r',"$ProjectDir\requirements-worker.txt",'selenium','webdriver-manager')
    Invoke-Native $Python @('-m','pip','check')
    Invoke-Native $Python @('-c','import flask, waitress, yaml, selenium, webdriver_manager, websocket')
    Invoke-Native $Python @((Join-Path $RunDir 'deploy_helper.py'),'prepare',$RuntimeFile)

    # A kill-on-close Job Object contains the supervisor and ALL inherited descendants.
    # Stopping the scheduled task therefore also stops browser/driver/capture processes.
    $runnerCode = @'
import ctypes, json, os, subprocess, sys
from ctypes import wintypes as w
from pathlib import Path

class BasicLimits(ctypes.Structure):
    _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                ('flags', w.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
class IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ('read_ops','write_ops','other_ops','read_bytes','write_bytes','other_bytes')]
class ExtendedLimits(ctypes.Structure):
    _fields_ = [('basic', BasicLimits), ('io', IoCounters), ('process_memory', ctypes.c_size_t),
                ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]

def contain_process_tree():
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
    kernel.CreateJobObjectW.restype = w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    kernel.GetCurrentProcess.restype = w.HANDLE
    job = kernel.CreateJobObjectW(None, None)
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not kernel.AssignProcessToJobObject(job, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return job  # Deliberately kept open until this supervisor exits; never inherited.

def main():
    runtime = json.loads(Path(__file__).with_name('runtime.json').read_text(encoding='utf-8'))
    env = dict(os.environ)
    for name in list(env):
        if name.startswith('WORKER_') or name in {'PYTHONHOME','PYTHONPATH','PROJECT_ROOT','PYTHON_EXECUTABLE','CHROME_BINARY','EDGE_BINARY','FIREFOX_BINARY','MAX_QUEUE_SIZE','MAX_ITEMS','TASK_TIMEOUT_SECONDS','MAX_CONTENT_LENGTH'}:
            del env[name]
    prefix = Path(runtime['python']).parent
    env.update(WORKER_CONFIG_FILE=str(Path(runtime['project']) / 'worker.yaml'), PYTHONUTF8='1', PYTHONUNBUFFERED='1',
               PATH=os.pathsep.join(map(str, (prefix, prefix / 'Scripts', prefix / 'Library/bin', Path(runtime['tshark']).parent))) + os.pathsep + env.get('PATH', ''))
    env.update(runtime.get('drivers', {}))
    logs = Path(runtime['logs'])
    logs.mkdir(parents=True, exist_ok=True)
    for name in ('worker.stdout.log', 'worker.stderr.log'):
        path = logs / name
        if path.exists() and path.stat().st_size > 20 * 1024 * 1024:
            path.replace(path.with_suffix('.log.previous'))
    with (logs / 'worker.stdout.log').open('ab', buffering=0) as stdout, (logs / 'worker.stderr.log').open('ab', buffering=0) as stderr:
        try:
            job = contain_process_tree()
            subprocess.run([runtime['python'], '-u', '-m', 'worker_agent'], cwd=runtime['project'], env=env,
                           stdout=stdout, stderr=stderr, check=False)
        except Exception:
            import traceback
            stderr.write(traceback.format_exc().encode('utf-8'))
    # Request Task Scheduler restart even when the worker unexpectedly exits with 0.
    return 1

if __name__ == '__main__':
    sys.exit(main())
'@
    Write-Utf8 $Runner $runnerCode
    Write-Host "Registering startup task for $($TaskCredential.UserName)..."
    Register-WorkerStartupTask $TaskCredential
    Start-ScheduledTask -TaskName $TaskName -TaskPath '\'
    Invoke-Native $Python @((Join-Path $RunDir 'deploy_helper.py'),'verify',$RuntimeFile)
    $task = Get-ScheduledTask -TaskName $TaskName -TaskPath '\'
    if ($task.State -ne 'Running' -or -not $task.Settings.Enabled) { throw 'Scheduled task is not running/enabled.' }

    if ($MasterIP) {
        $ruleName = 'TrafficCaptureWorker-Master'
        if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) { Remove-NetFirewallRule -Name $ruleName }
        New-NetFirewallRule -Name $ruleName -DisplayName 'Traffic Worker from Master' -Direction Inbound -Action Allow -Protocol TCP -LocalPort 5100 -RemoteAddress $MasterIP -Profile Any | Out-Null
    }
    Write-Host "Deployment complete. Config: $ConfigFile"
    Write-Host "Logs: $LogDir | Backups: $BackupDir"
    Write-Host 'Master URL: http://WORKER-IP:5100 (without /api/v1); read the Token from worker.yaml.'
    Write-Host 'Allow TCP 5100 from the Master IP in your cloud security group. Without -MasterIP, Windows firewall rules are unchanged.'
} catch {
    Write-Warning "Deployment incomplete. Worker may be stopped/disabled or running without passing acceptance. Logs: $LogDir ; backups: $BackupDir"
    throw
} finally {
    if ($transcript) { Stop-Transcript | Out-Null }
    if ($locked) { $lock.ReleaseMutex() }
    $lock.Dispose()
}
