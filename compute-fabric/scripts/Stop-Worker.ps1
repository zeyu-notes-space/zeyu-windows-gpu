#Requires -Version 5.1
[CmdletBinding()]
param([string]$Root = 'C:\ProgramData\ZeYuComputeFabric', [switch]$Force)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-FabricBootstrapScript([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'The Fabric security helper must be an existing absolute file.' }
    $trusted = @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $trusted += @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop | ForEach-Object { $_.SID.Value })
    $packageRoot = [IO.Directory]::GetParent($PSScriptRoot).FullName
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        $item = Get-Item -LiteralPath $cursor -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing to load a security helper through a reparse point: $cursor" }
        $acl = Get-Acl -LiteralPath $cursor
        if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) { throw "Security helper path has an untrusted owner: $cursor" }
        $mask = [int64][Security.AccessControl.FileSystemRights]'Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
        if ($cursor -eq $Path -or $cursor.StartsWith($packageRoot, [StringComparison]::OrdinalIgnoreCase)) { $mask = $mask -bor [int64][Security.AccessControl.FileSystemRights]::Write }
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and -not ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and $rule.IdentityReference.Value -notin $trusted -and ([int64]$rule.FileSystemRights -band $mask)) { throw "Security helper path is writable or replaceable by a non-administrator: $cursor" }
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
}

$securityScript = Join-Path $PSScriptRoot 'Worker-Security.ps1'
Assert-FabricBootstrapScript $securityScript
. $securityScript
$install = Assert-FabricInstall $Root
$Root = $install.root
$config = Get-Content -LiteralPath (Join-Path $Root 'worker.json') -Raw | ConvertFrom-Json
if (-not $Force) {
    $token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
    try {
        $jobs = @(Invoke-RestMethod -Uri "http://127.0.0.1:$($config.port)/v1/jobs" -Headers @{Authorization="Bearer $token"} -TimeoutSec 5 | Write-Output)
        $active = @($jobs | Where-Object { $_.state -in @('STARTING','RUNNING') })
        if ($active.Count -gt 0) { throw 'Active jobs exist. Cancel/wait for them first, or use -Force to test restart recovery.' }
    } finally { $token = $null }
}
# Verify process identity before stopping. A stale PID file must never kill an unrelated process.
$processFile = Join-Path $Root 'state\worker-process.json'
$expectedExecutable = Join-Path $Root 'worker-runtime\Scripts\python.exe'
$expectedConfig = Join-Path $Root 'worker.json'
$escapedExecutable = [regex]::Escape($expectedExecutable)
$escapedConfig = [regex]::Escape($expectedConfig)
$expectedCommand = '^\s*(?:"' + $escapedExecutable + '"|' + $escapedExecutable + ')\s+-I\s+-u\s+-m\s+zeyu_fabric\.server\s+--config\s+(?:"' + $escapedConfig + '"|' + $escapedConfig + ')\s*$'
$workerInfo = $null
if (Test-Path -LiteralPath $processFile) {
    Assert-FabricNoReparseAncestors $processFile
    $saved = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json
    $candidate = Get-CimInstance Win32_Process -Filter "ProcessId = $([int]$saved.pid)" -ErrorAction SilentlyContinue
    if ($candidate) {
        $actualStart = $candidate.CreationDate.ToUniversalTime()
        $savedStart = [DateTime]::Parse($saved.started_at).ToUniversalTime()
        $owner = Invoke-CimMethod -InputObject $candidate -MethodName GetOwnerSid
        # The record is untrusted writable state. Never use its executable/config fields as authority.
        if ($candidate.ExecutablePath -ieq $expectedExecutable -and $candidate.CommandLine -imatch $expectedCommand -and $owner.ReturnValue -eq 0 -and $owner.Sid -eq $install.worker_sid -and [Math]::Abs(($actualStart - $savedStart).TotalSeconds) -lt 0.01) {
            $workerInfo = $candidate
        } else { throw 'PID file no longer identifies this worker. Refusing to stop an unrelated process.' }
    }
}
# A manual stop prevents Task Scheduler's failure-restart policy from relaunching it.
Stop-ScheduledTask -TaskName $install.task_name
if ($workerInfo) {
    $stillRunning = Get-CimInstance Win32_Process -Filter "ProcessId = $($workerInfo.ProcessId)" -ErrorAction SilentlyContinue
    if ($stillRunning -and $stillRunning.CreationDate -eq $workerInfo.CreationDate) {
        & (Join-Path $env:windir 'System32\taskkill.exe') /PID $workerInfo.ProcessId /T /F | Out-Null
        if ($LASTEXITCODE -ne 0 -and (Get-Process -Id $workerInfo.ProcessId -ErrorAction SilentlyContinue)) { throw 'Worker process could not be stopped.' }
    }
}
Write-Host 'Worker stopped. Queued jobs remain queued; interrupted jobs are diagnosed during the next start.'
