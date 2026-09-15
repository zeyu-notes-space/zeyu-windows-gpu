#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [ValidatePattern('^[a-zA-Z0-9_.-]+$')][string]$WorkerUser = 'zeyu-worker',
    [Parameter(Mandatory)][string]$Python,
    [hashtable]$Projects = @{},
    [hashtable]$Environments = @{},
    [string]$AcceptancePython,
    [switch]$InstallCudaEnvironment,
    [switch]$CreateWorkerUser,
    [string]$Root = 'C:\ProgramData\ZeYuComputeFabric',
    [ValidateRange(1024,65535)][int]$Port = 8765,
    [string]$GpuLeasePath,
    [ValidatePattern('^[a-zA-Z0-9_.-]+$')][string]$TaskName = 'ZeYuComputeFabric',
    [string]$PackagePath = (Split-Path -Parent $PSScriptRoot),
    [PSCredential]$Credential,
    [switch]$NoStart
)
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

function Write-PrivateJson([string]$Path, $Value) {
    [IO.File]::WriteAllText($Path, ($Value | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
}
function Invoke-Checked([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable exited with $LASTEXITCODE" }
}
function Resolve-AbsoluteFile([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path) -or -not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "An existing absolute executable path is required: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

$account = Get-LocalUser -Name $WorkerUser -ErrorAction SilentlyContinue
if (-not $account -and -not $CreateWorkerUser) { throw 'Worker account does not exist. Use -CreateWorkerUser to create a regular local account.' }
if ($account -and -not $account.Enabled) { throw 'Worker account is disabled.' }
$admins = @(Get-LocalGroupMember -SID 'S-1-5-32-544')
if ($account -and $admins.SID.Value -contains $account.SID.Value) {
    throw 'Use a regular local account outside the Administrators group.'
}
$accountName = "$env:COMPUTERNAME\$WorkerUser"
$Python = Resolve-AbsoluteFile $Python
$git = (Get-Command git.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
if ($Projects.ContainsKey('fabric-acceptance') -or $Environments.ContainsKey('acceptance')) { throw 'fabric-acceptance and acceptance are reserved installer aliases.' }
if ($AcceptancePython -and $InstallCudaEnvironment) { throw 'Choose -AcceptancePython or -InstallCudaEnvironment, not both.' }
if ($AcceptancePython) { $AcceptancePython = Resolve-AbsoluteFile $AcceptancePython }
foreach ($alias in @($Projects.Keys)) {
    if ($alias -notmatch '^[a-zA-Z0-9_.-]+$') { throw "Invalid project alias: $alias" }
    if (-not [IO.Path]::IsPathRooted($Projects[$alias])) { throw "Project must be absolute: $alias" }
    $Projects[$alias] = (Resolve-Path -LiteralPath $Projects[$alias]).Path
    if (-not (Test-Path -LiteralPath (Join-Path $Projects[$alias] '.git'))) { throw "Project must be a local Git checkout: $alias" }
}
foreach ($alias in $Environments.Keys) {
    if ($alias -notmatch '^[a-zA-Z0-9_.-]+$' -or $Environments[$alias] -isnot [hashtable]) {
        throw "Environment must map an alias to @{ python = 'absolute path' }: $alias"
    }
    $Environments[$alias]['python'] = Resolve-AbsoluteFile $Environments[$alias]['python']
}
if (-not [IO.Path]::IsPathRooted($Root) -or $Root.StartsWith('\\')) { throw 'Root must be a local absolute path.' }
$Root = Assert-FabricRootLocation $Root
if (-not $GpuLeasePath) { $GpuLeasePath = Join-Path $Root 'state\gpu-exclusive.lock' }
if (-not [IO.Path]::IsPathRooted($GpuLeasePath) -or $GpuLeasePath.StartsWith('\\')) {
    throw 'GpuLeasePath must be a local absolute path.'
}
$GpuLeasePath = [IO.Path]::GetFullPath($GpuLeasePath)
$marker = Join-Path $Root 'installation.json'
$pendingMarker = Join-Path $Root 'installation.pending.json'
$newInstallation = -not (Test-Path -LiteralPath $Root)
$pendingInstallation = Test-Path -LiteralPath $pendingMarker
$pendingResume = $pendingInstallation -and -not (Test-Path -LiteralPath $marker)
if ((Test-Path -LiteralPath $Root) -and -not (Test-Path -LiteralPath $marker) -and -not $pendingInstallation) {
    throw "Refusing to change an existing unmanaged directory: $Root"
}
if ($pendingResume) {
    # A pending marker is accepted only from a protected root and only for the
    # same account/task/Git/lease identity. Mutable run data is never trusted.
    Assert-FabricProtectedPath $pendingMarker
    $pending = Get-Content -LiteralPath $pendingMarker -Raw | ConvertFrom-Json
    $pendingFields = @($pending.PSObject.Properties.Name)
    if ($pendingFields -notcontains 'schema_version' -or $pendingFields -notcontains 'state' -or
        $pendingFields -notcontains 'root' -or $pendingFields -notcontains 'task_name' -or
        $pendingFields -notcontains 'worker_user' -or $pendingFields -notcontains 'worker_sid' -or
        $pendingFields -notcontains 'git' -or $pendingFields -notcontains 'gpu_lease_path' -or
        [int]$pending.schema_version -ne 2 -or [string]$pending.state -ne 'IN_PROGRESS' -or
        [string]$pending.root -ine $Root -or [string]$pending.task_name -ne $TaskName -or
        [string]$pending.worker_user -ine $accountName -or -not $account -or
        [string]$pending.worker_sid -ne $account.SID.Value -or [string]$pending.git -ine $git -or
        [string]$pending.gpu_lease_path -ine $GpuLeasePath) {
        throw 'Protected pending installation metadata does not match this account, task, Git, lease or Root.'
    }
    Assert-FabricProtectedPath $Root
    foreach ($entry in @(Get-ChildItem -LiteralPath $Root -Force)) {
        if ($entry.Name -in @('data','worker-logs','state')) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Mutable top-level directory is a reparse point: $($entry.FullName)" }
            continue
        }
        Assert-FabricProtectedPath $entry.FullName -Recurse
    }
}
$existingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existingTask -and -not (Test-Path -LiteralPath $marker) -and -not $pendingInstallation) { throw 'Task name is already used by another installation.' }
if ($existingTask -and $existingTask.State -eq 'Running') { throw 'Stop the worker with Stop-Worker.ps1 before updating.' }
if (Test-Path -LiteralPath $marker) {
    # This runs before any Python/Git/package execution. Schema 1 paths may contain injected .pth files.
    $previous = Assert-FabricInstall $Root
    if (-not $account -or $previous.worker_sid -ne $account.SID.Value -or $previous.task_name -ne $TaskName) {
        throw 'Changing the owner or task of an existing installation requires an explicit migration.'
    }
    foreach ($entry in @(Get-ChildItem -LiteralPath $Root -Force)) {
        if ($entry.Name -in @('data','worker-logs','state')) {
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Mutable top-level directory is a reparse point: $($entry.FullName)" }
            continue
        }
        Assert-FabricProtectedPath $entry.FullName -Recurse
    }
}
# Elevated Python may execute .pth/startup code. Only a protected all-users base interpreter is accepted.
$pythonBase = Split-Path -Parent $Python
if ((Split-Path -Leaf $pythonBase) -ieq 'Scripts' -or (Test-Path -LiteralPath (Join-Path $pythonBase 'pyvenv.cfg'))) {
    throw '-Python must be a protected base Python installation, not a virtual environment. Use -AcceptancePython for workload environments.'
}
Assert-FabricSafeAncestors $pythonBase
Assert-FabricProtectedPath $pythonBase -Recurse
$gitBase = Split-Path -Parent $git
if ((Split-Path -Leaf $gitBase) -in @('cmd','bin')) { $gitBase = Split-Path -Parent $gitBase }
Assert-FabricSafeAncestors $gitBase
Assert-FabricProtectedPath $gitBase -Recurse
$PackagePath = (Resolve-Path -LiteralPath $PackagePath).Path
Assert-FabricSafeAncestors $PackagePath
Assert-FabricProtectedPath $PackagePath
foreach ($part in @('pyproject.toml','zeyu_fabric','scripts','examples')) { Assert-FabricProtectedPath (Join-Path $PackagePath $part) -Recurse }
Invoke-Checked $Python @('-I', '-c', "import sys; assert sys.version_info >= (3,9), 'Python >= 3.9 required'; assert sys.maxsize > 2**32, '64-bit Python required'")
$workerCredential = $Credential
if (-not $workerCredential) {
    $workerCredential = Get-Credential -UserName $accountName -Message 'Enter the local worker account password for boot-time Task Scheduler execution. Do not use a Windows Hello PIN.'
}
if (-not $workerCredential -or $workerCredential.UserName -ine $accountName) { throw 'Credential must match the supplied local worker account.' }
if (-not $account) {
    $account = New-LocalUser -Name $WorkerUser -Password $workerCredential.Password -PasswordNeverExpires -Description 'ZeYu Compute Fabric regular compute account'
    Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $account
}

Set-FabricDirectoryAcl $Root $account.SID.Value
if ($newInstallation -or $pendingResume) {
    if (-not (Test-Path -LiteralPath $pendingMarker)) {
        Write-PrivateJson $pendingMarker @{ schema_version=2; state='IN_PROGRESS'; root=$Root; task_name=$TaskName; worker_user=$accountName; worker_sid=$account.SID.Value; started_at=[DateTime]::UtcNow.ToString('o'); git=$git; gpu_lease_path=$GpuLeasePath }
    }
    Assert-FabricProtectedPath $pendingMarker
}

$appDir = Join-Path $Root 'app'
$scriptDir = Join-Path $Root 'scripts'
$runtimeDir = Join-Path $Root 'worker-runtime'
foreach ($directory in @($appDir, $scriptDir)) {
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
}
$dataRoot = Join-Path $Root 'data'
foreach ($directory in @($dataRoot, (Join-Path $Root 'worker-logs'), (Join-Path $Root 'state'))) { Set-FabricDirectoryAcl $directory $account.SID.Value -Mutable }
$leaseParent = Split-Path -Parent $GpuLeasePath
if (-not (Test-Path -LiteralPath $leaseParent -PathType Container)) { throw "GpuLeasePath parent does not exist: $leaseParent" }
$expectedLeaseParent = [IO.Path]::GetFullPath((Join-Path $Root 'state')).TrimEnd('\')
if ([IO.Path]::GetFullPath($leaseParent).TrimEnd('\') -ine $expectedLeaseParent) {
    throw 'GpuLeasePath must be inside this installation state directory so the worker ACL is exact.'
}
Copy-Item -LiteralPath (Join-Path $PackagePath 'pyproject.toml') -Destination $appDir -Force
if (Test-Path -LiteralPath (Join-Path $appDir 'zeyu_fabric')) { Remove-Item -LiteralPath (Join-Path $appDir 'zeyu_fabric') -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $PackagePath 'zeyu_fabric') -Destination $appDir -Recurse
Get-ChildItem -LiteralPath (Join-Path $PackagePath 'scripts') -Filter '*.ps1' | Copy-Item -Destination $scriptDir -Force
if (-not (Test-Path -LiteralPath (Join-Path $runtimeDir 'Scripts\python.exe'))) {
    Invoke-Checked $Python @('-I', '-m', 'venv', $runtimeDir)
}
$workerPython = Join-Path $runtimeDir 'Scripts\python.exe'
Assert-FabricProtectedPath $runtimeDir -Recurse
Invoke-Checked $workerPython @('-I', '-m', 'pip', 'install', '--disable-pip-version-check', $appDir)
Invoke-Checked $workerPython @('-I', '-c', 'import zeyu_fabric.server, zeyu_fabric.engine, psutil')
if ($InstallCudaEnvironment) {
    Invoke-Checked $Python @('-I', '-c', "import sys; assert sys.version_info >= (3,10), 'Pinned PyTorch requires Python >= 3.10; Python 3.11 is recommended'")
    $cudaDir = Join-Path $Root 'environments\pytorch'
    if (-not (Test-Path -LiteralPath (Join-Path $cudaDir 'Scripts\python.exe'))) { Invoke-Checked $Python @('-I', '-m', 'venv', $cudaDir) }
    $AcceptancePython = Join-Path $cudaDir 'Scripts\python.exe'
    # Fixed documented CUDA build; installation is optional and never alters another environment.
    Assert-FabricProtectedPath $cudaDir -Recurse
    Invoke-Checked $AcceptancePython @('-I', '-m', 'pip', 'install', '--disable-pip-version-check', 'torch==2.9.0', '--index-url', 'https://download.pytorch.org/whl/cu128')
}
if (-not $AcceptancePython) { $AcceptancePython = $workerPython }
$Environments['acceptance'] = @{ python=$AcceptancePython }
$fixture = Join-Path $Root 'acceptance-repo'
New-Item -ItemType Directory -Path (Join-Path $fixture 'examples') -Force | Out-Null
Get-ChildItem -LiteralPath (Join-Path $PackagePath 'examples') -Filter '*.py' | Copy-Item -Destination (Join-Path $fixture 'examples') -Force
Invoke-Checked $git @('-c', "safe.directory=$fixture", '-C', $fixture, 'init', '--quiet')
Invoke-Checked $git @('-c', "safe.directory=$fixture", '-C', $fixture, 'add', '--', 'examples')
& $git -c "safe.directory=$fixture" -C $fixture diff --cached --quiet
if ($LASTEXITCODE -eq 1) {
    Invoke-Checked $git @('-c', "safe.directory=$fixture", '-c', 'user.name=ZeYu Fabric Installer', '-c', 'user.email=local-fixture@localhost', '-C', $fixture, 'commit', '--quiet', '-m', 'Install acceptance workload fixtures')
} elseif ($LASTEXITCODE -ne 0) { throw 'Could not inspect acceptance fixture changes.' }
$fixtureCommit = (& $git -c "safe.directory=$fixture" -C $fixture rev-parse HEAD | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $fixtureCommit -notmatch '^[0-9a-f]{40,64}$') { throw 'Could not resolve acceptance fixture commit.' }
$gitConfig = Join-Path $Root 'worker-gitconfig'
[IO.File]::WriteAllText($gitConfig, '', [Text.UTF8Encoding]::new($false))
Invoke-Checked $git @('config', '--file', $gitConfig, '--add', 'safe.directory', $fixture)
$Projects['fabric-acceptance'] = $fixture
Write-PrivateJson (Join-Path $Root 'acceptance-info.json') @{ git_commit=$fixtureCommit; project='fabric-acceptance'; environment='acceptance'; task_name=$TaskName; root=$Root; port=$Port }
$specDir = Join-Path $Root 'acceptance-specs'
New-Item -ItemType Directory -Path $specDir -Force | Out-Null
foreach ($probe in @('smoke', 'cuda_smoke', 'fail', 'oom', 'timeout')) {
    $spec = @{ project='fabric-acceptance'; git_commit=$fixtureCommit; environment='acceptance'; command=@('{python}', "examples/$probe.py"); arguments=@(); timeout=180; artifact_paths=@() }
    if ($probe -eq 'timeout') { $spec.timeout=15 }
    if ($probe -in @('cuda_smoke','oom')) { $spec.resources=@{gpu=$true; min_vram_mb=10240} }
    Write-PrivateJson (Join-Path $specDir "$probe.json") $spec
}
$tokenPath = Join-Path $Root 'worker.token'
if (-not (Test-Path -LiteralPath $tokenPath)) {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    [IO.File]::WriteAllText($tokenPath, ([BitConverter]::ToString($bytes).Replace('-', '').ToLowerInvariant()), [Text.UTF8Encoding]::new($false))
}
$configPath = Join-Path $Root 'worker.json'
Write-PrivateJson $configPath @{
    root=$dataRoot; host='127.0.0.1'; port=$Port; token_file=$tokenPath;
    projects=$Projects; environments=$Environments; metrics_interval=2.0; allowed_job_env=@();
    gpu_lease_path=$GpuLeasePath; gpu_lease_wait_seconds=0
}
$launcher = Join-Path $Root 'worker-launcher.py'
$launcherCode = @'
"""Installed boot supervisor: logs and PID identity, no shell or secret arguments."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
import psutil
from zeyu_fabric.runner import spawn

root = Path(sys.argv[1])
install = json.loads((root / "installation.json").read_text(encoding="utf-8-sig"))
env = os.environ.copy()
env["PATH"] = str(Path(install["git"]).parent) + os.pathsep + env.get("PATH", "")
env["PYTHONUNBUFFERED"] = "1"
env["GIT_CONFIG_GLOBAL"] = str(root / "worker-gitconfig")
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
logs = root / "worker-logs"
logs.mkdir(exist_ok=True)
record_path = root / "state" / "worker-process.json"
config = root / "worker.json"
process = None
with (logs / (stamp + ".stdout.log")).open("ab", buffering=0) as out, (logs / (stamp + ".stderr.log")).open("ab", buffering=0) as err:
    try:
        process = spawn([sys.executable, "-I", "-u", "-m", "zeyu_fabric.server", "--config", str(config)], root, env, out, err)
        try:
            record = {"pid": process.pid, "executable": sys.executable, "config": str(config), "started_at": datetime.fromtimestamp(psutil.Process(process.pid).create_time(), timezone.utc).isoformat(), "stdout": out.name, "stderr": err.name}
            temporary = record_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record), encoding="utf-8")
            os.replace(temporary, record_path)
        except psutil.NoSuchProcess:
            pass
        # Containment wait defaults to a bounded cleanup wait, not service lifetime.
        while process.poll() is None:
            time.sleep(0.5)
        exit_code = process.poll()
    except BaseException as exc:
        err.write(("Supervisor failure: " + repr(exc) + "\n").encode("utf-8"))
        exit_code = 1
    finally:
        try:
            if process is not None:
                # runner.close() terminates and drains the contained process tree.
                process.close()
        except BaseException as exc:
            err.write(("Supervisor cleanup failure: " + repr(exc) + "\n").encode("utf-8"))
            exit_code = 1
        if record_path.exists():
            try:
                saved = json.loads(record_path.read_text(encoding="utf-8"))
                if process is not None and saved.get("pid") == process.pid:
                    record_path.unlink()
            except (OSError, ValueError):
                pass
sys.exit(exit_code)
'@
[IO.File]::WriteAllText($launcher, $launcherCode, [Text.UTF8Encoding]::new($false))
$action = New-ScheduledTaskAction -Execute $workerPython -Argument "-I -u `"$launcher`" `"$Root`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$passwordText = $null
try {
    # The Windows API needs a transient string. No password is written to a file or command line.
    # Task Scheduler stores its own protected logon credential so boot does not require login.
    $passwordText = $workerCredential.GetNetworkCredential().Password
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -User $accountName -Password $passwordText -RunLevel Limited -Description 'ZeYu private compute worker; loopback API; durable queue; regular user.' -Force | Out-Null
} finally { $passwordText = $null; $workerCredential = $null }
$scheduler = New-Object -ComObject 'Schedule.Service'
$scheduler.Connect()
$registered = $scheduler.GetFolder('\').GetTask($TaskName)
$registered.SetSecurityDescriptor("D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;GRGX;;;$($account.SID.Value))", 0)
if ($newInstallation -or $pendingResume) {
    $registryPath = Get-FabricRegistryPath $Root
    if (Test-Path -LiteralPath $registryPath) { Assert-FabricRegistryAnchor $Root $account.SID.Value }
    else { New-FabricRegistryAnchor $Root $account.SID.Value }
}
Write-PrivateJson $marker @{ schema_version=2; root=$Root; task_name=$TaskName; worker_user=$accountName; worker_sid=$account.SID.Value; installed_at=[DateTime]::UtcNow.ToString('o'); git=$git; gpu_lease_path=$GpuLeasePath }
if (Test-Path -LiteralPath $pendingMarker) { Remove-Item -LiteralPath $pendingMarker -Force }
Write-Host "Installed: $Root"
Write-Host "Account: $accountName (limited); task: $TaskName; API: 127.0.0.1:$Port"
Write-Host 'Token created privately and not printed. Copy worker.token to the Mac using authenticated SSH/SFTP.'
Write-Host 'Project repositories and environment executables must be readable by the worker account. Installation does not widen their ACLs.'
Write-Host "Acceptance specs: $specDir ; fixture commit: $fixtureCommit"
if (-not $NoStart) { & (Join-Path $scriptDir 'Start-Worker.ps1') -Root $Root }
