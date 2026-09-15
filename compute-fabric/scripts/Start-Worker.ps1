#Requires -Version 5.1
[CmdletBinding()]
param([string]$Root = 'C:\ProgramData\ZeYuComputeFabric', [switch]$Run)
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
$configPath = Join-Path $Root 'worker.json'
if (-not $Run) {
    $task = Get-ScheduledTask -TaskName $install.task_name -ErrorAction Stop
    $action = @($task.Actions)
    $expectedPython = Join-Path $Root 'worker-runtime\Scripts\python.exe'
    $expectedLauncher = Join-Path $Root 'worker-launcher.py'
    if ($action.Count -ne 1 -or [string]$action[0].Execute -ine $expectedPython -or
        [string]$action[0].WorkingDirectory -ine $Root -or
        [string]$action[0].Arguments -notmatch ('(?i)^-I\s+-u\s+"?' + [regex]::Escape($expectedLauncher) + '"?\s+"?' + [regex]::Escape($Root) + '"?\s*$')) {
        throw 'The scheduled task does not use the protected worker supervisor. Rerun Install-Worker.ps1 before starting.'
    }
    Start-ScheduledTask -TaskName $install.task_name
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $token = (Get-Content -LiteralPath $config.token_file -Raw).Trim()
    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($config.port)/v1/health" -Headers @{Authorization="Bearer $token"} -TimeoutSec 2
            if ($health.status -eq 'ok') { $ready = $true; break }
        } catch { Start-Sleep -Seconds 1 }
    }
    $token = $null
    if (-not $ready) { throw "Task did not become healthy. Run Get-WorkerDiagnostics.ps1 -Root `"$Root`"." }
    $health | ConvertTo-Json
    return
}

if ([Security.Principal.WindowsIdentity]::GetCurrent().User.Value -ne $install.worker_sid) {
    throw 'The worker must run as its configured regular user. Start the registered task without -Run.'
}
$python = Join-Path $Root 'worker-runtime\Scripts\python.exe'
& $python -I -u (Join-Path $Root 'worker-launcher.py') $Root
exit $LASTEXITCODE
