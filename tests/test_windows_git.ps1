#requires -Version 5.1
# No software installs or real registry changes.
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
$scriptPath=Join-Path (Split-Path $PSScriptRoot -Parent) 'deployment\deploy_worker_windows.ps1'
$tokens=$null; $errors=$null
$ast=[System.Management.Automation.Language.Parser]::ParseFile($scriptPath,[ref]$tokens,[ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
foreach ($name in @('Get-InstalledGitRoots','Find-ExistingGit','Assert-GitInstallation')) {
    $node=$ast.Find({param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name},$true)
    . ([scriptblock]::Create($node.Extent.Text))
}
$script:root='C:\Users\Administrator\AppData\Local\Programs\Git'
$script:exists=$true
$script:versionCalls=@()
function Get-ItemProperty {
    param($LiteralPath,$ErrorAction)
    # Duplicate machine/user registrations must produce one candidate.
    if ($LiteralPath -in @('HKLM:\SOFTWARE\GitForWindows','HKCU:\SOFTWARE\GitForWindows')) {
        [pscustomobject]@{InstallPath=$script:root}
    }
}
function Test-Path {
    param($LiteralPath,$PathType)
    return ($script:exists -and $LiteralPath -eq (Join-Path $script:root 'cmd\git.exe'))
}
function Assert-PlainPath { param($Path) }
function Invoke-Native {
    param($File,$Arguments)
    if ($Arguments.Count -ne 1 -or $Arguments[0] -ne '--version') { throw 'Unexpected executable call.' }
    $script:versionCalls += $File
}
$roots=@(Get-InstalledGitRoots)
if ($roots.Count -ne 1 -or $roots[0] -ne $script:root) { throw 'AppData registry path was missed or duplicated.' }
$binary=Find-ExistingGit
if ($binary -ne (Join-Path $script:root 'cmd\git.exe')) { throw 'Existing C: AppData Git was not selected.' }
Assert-GitInstallation $binary 'D:\TrafficWorker\logs\git-install-test.log'
if ($script:versionCalls.Count -ne 1 -or $script:versionCalls[0] -ne $binary) { throw 'Git verification did not execute the discovered binary.' }
try {
    Assert-GitInstallation 'D:\TrafficWorker\tools\Git\cmd\git.exe' 'D:\TrafficWorker\logs\git-install-test.log'
    throw 'Expected missing destination failure.'
} catch {
    if (-not $_.Exception.Message.Contains($script:root) -or -not $_.Exception.Message.Contains('git-install-test.log')) { throw }
}
$script:exists=$false
if (Find-ExistingGit) { throw 'Stale registry entry was accepted without git.exe.' }
$script:exists=$true; $script:root='D:\other Git'
if (Find-ExistingGit) { throw 'D: Git was selected against the C: deployment requirement.' }
$script:root='C:\'
try { Get-InstalledGitRoots; throw 'Expected drive-root rejection.' } catch { if ($_.Exception.Message -notmatch 'Invalid registered Git installation root') { throw } }
Write-Host 'PASS: C: AppData Git discovery, deduplication, real-path version check, missing binary diagnostics, stale entry and drive-root rejection.'
