#Requires -Version 5.1
[CmdletBinding()]
param([string]$Root = 'C:\ProgramData\ZeYuComputeFabric')
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
$task = Get-ScheduledTask -TaskName $install.task_name -ErrorAction SilentlyContinue
$taskInfo = Get-ScheduledTaskInfo -TaskName $install.task_name -ErrorAction SilentlyContinue
$report = [ordered]@{
    captured_at=[DateTime]::UtcNow.ToString('o'); computer=$env:COMPUTERNAME; root=$Root;
    worker_user=$install.worker_user; task_name=$install.task_name; task_state=$null;
    task_last_result=$null; task_last_run=$null; health=$null; health_error=$null;
    listening=@(); runtime=$null; gpu=@(); gpu_lease=$null; recent_worker_logs=@()
}
if ($task) { $report.task_state = [string]$task.State }
if ($taskInfo) { $report.task_last_result=$taskInfo.LastTaskResult; $report.task_last_run=$taskInfo.LastRunTime.ToString('o') }
try {
    $token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
    $report.health = Invoke-RestMethod -Uri "http://127.0.0.1:$($config.port)/v1/health" -Headers @{Authorization="Bearer $token"} -TimeoutSec 5
} catch { $report.health_error = $_.Exception.Message } finally { $token = $null }
$report.listening = @(Get-NetTCPConnection -LocalPort $config.port -State Listen -ErrorAction SilentlyContinue | Select-Object LocalAddress,LocalPort,OwningProcess)
$python = Join-Path $Root 'worker-runtime\Scripts\python.exe'
if (Test-Path -LiteralPath $python) {
    try {
        Assert-FabricProtectedPath (Join-Path $Root 'worker-runtime') -Recurse
        $report.runtime = (& $python -I -c 'import sys,platform; print(sys.version); print(platform.platform())' 2>&1 | Out-String).Trim()
    }
    catch { $report.runtime = 'Runtime probe failed: ' + $_.Exception.Message }
}
$nvidia = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if ($nvidia) {
    try {
        Assert-FabricSafeAncestors $nvidia.Source
        Assert-FabricProtectedPath (Split-Path -Parent $nvidia.Source)
        Assert-FabricProtectedPath $nvidia.Source
        $report.gpu = @(& $nvidia.Source --query-gpu=name,driver_version,memory.total,memory.used,utilization.gpu --format=csv 2>&1 | ForEach-Object { [string]$_ })
    }
    catch { $report.gpu = @('NVIDIA probe failed: ' + $_.Exception.Message) }
}
$leasePath = [string]$config.gpu_lease_path
$lease = [ordered]@{path=$leasePath; file_exists=(Test-Path -LiteralPath $leasePath -PathType Leaf); held=$false; holder=$null; lock_probe_error=$null}
if ($lease.file_exists) {
    try {
        $lines = @(Get-Content -LiteralPath $leasePath -ErrorAction Stop)
        if ($lines.Count -gt 1 -and $lines[1]) { $lease.holder = $lines[1] | ConvertFrom-Json }
    } catch { $lease.lock_probe_error = 'Lease metadata could not be read: ' + $_.Exception.Message }
    $probe = $null
    try {
        $probe = [IO.File]::Open($leasePath, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite)
        try { $probe.Lock(0, 1); $probe.Unlock(0, 1) }
        catch { $lease.held = $true }
    } catch { $lease.lock_probe_error = 'Lease lock state could not be probed: ' + $_.Exception.Message }
    finally { if ($probe) { $probe.Dispose() } }
}
$report.gpu_lease = $lease
if (Test-Path -LiteralPath (Join-Path $Root 'worker-logs')) {
    Assert-FabricNoReparseAncestors (Join-Path $Root 'worker-logs')
    $report.recent_worker_logs = @(Get-ChildItem -LiteralPath (Join-Path $Root 'worker-logs') -File | Sort-Object LastWriteTime -Descending | Select-Object -First 6 Name,Length,LastWriteTimeUtc)
}
# Deliberately omit token content, environment variables, and application logs.
$report | ConvertTo-Json -Depth 8
