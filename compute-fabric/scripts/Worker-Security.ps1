# Shared by installer/maintenance commands. Never execute this helper from mutable worker data.
Set-StrictMode -Version Latest

function Get-FabricTrustedSids {
    $identities = @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464')
    $identities += @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop | ForEach-Object { $_.SID.Value })
    return $identities
}

function Assert-FabricNoReparseAncestors([string]$Path) {
    $cursor = [IO.Path]::GetFullPath($Path)
    while ($cursor) {
        if (Test-Path -LiteralPath $cursor) {
            $item = Get-Item -LiteralPath $cursor -Force
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse points are not permitted in protected install paths: $cursor" }
        }
        $parent = [IO.Directory]::GetParent($cursor)
        if ($null -eq $parent) { break }
        $cursor = $parent.FullName
    }
}

function Assert-FabricProtectedPath([string]$Path, [switch]$Recurse, [switch]$AncestorOnly) {
    if (-not (Test-Path -LiteralPath $Path)) { throw "Protected path does not exist: $Path" }
    Assert-FabricNoReparseAncestors $Path
    $trusted = @(Get-FabricTrustedSids)
    $writeMask = [int64][Security.AccessControl.FileSystemRights]'Write,Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership'
    if ($AncestorOnly) { $writeMask = [int64][Security.AccessControl.FileSystemRights]'Delete,DeleteSubdirectoriesAndFiles,ChangePermissions,TakeOwnership' }
    $pending = [Collections.Generic.Stack[string]]::new()
    $pending.Push($Path)
    while ($pending.Count -gt 0) {
        $current = $pending.Pop()
        $item = Get-Item -LiteralPath $current -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Protected path contains a reparse point: $current" }
        $acl = Get-Acl -LiteralPath $current
        $ownerSid = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if ($ownerSid -notin $trusted) { throw "Protected path has a non-administrator owner: $current ($ownerSid)" }
        foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
            if ($rule.AccessControlType -ne [Security.AccessControl.AccessControlType]::Allow) { continue }
            if ($rule.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) { continue }
            if ($rule.IdentityReference.Value -notin $trusted -and ([int64]$rule.FileSystemRights -band $writeMask)) {
                throw "Protected path grants write/replacement rights to a non-administrator: $current ($($rule.IdentityReference.Value)). Refusing elevated execution or migration."
            }
        }
        if ($Recurse -and $item.PSIsContainer) {
            foreach ($child in @(Get-ChildItem -LiteralPath $current -Force)) { $pending.Push($child.FullName) }
        }
    }
}

function Assert-FabricRootLocation([string]$Root) {
    $canonical = [IO.Path]::GetFullPath($Root).TrimEnd('\')
    $programData = [Environment]::GetFolderPath([Environment+SpecialFolder]::CommonApplicationData).TrimEnd('\')
    if ([IO.Directory]::GetParent($canonical).FullName -ine $programData) {
        throw 'Protected Root must be a direct child of the standard ProgramData directory. Worker-writable parent locations are unsupported.'
    }
    Assert-FabricNoReparseAncestors $canonical
    Assert-FabricProtectedPath $programData -AncestorOnly
    return $canonical
}

function Assert-FabricSafeAncestors([string]$Path) {
    $parent = [IO.Directory]::GetParent([IO.Path]::GetFullPath($Path))
    while ($null -ne $parent) {
        Assert-FabricProtectedPath $parent.FullName -AncestorOnly
        $parent = $parent.Parent
    }
}

function Get-FabricRegistryPath([string]$Root) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $name = [BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Root.ToLowerInvariant()))).Replace('-', '') }
    finally { $sha.Dispose() }
    return "HKLM:\SOFTWARE\ZeYuComputeFabric\Installations\$name"
}

function Assert-FabricRegistryAnchor([string]$Root, [string]$WorkerSid) {
    $path = Get-FabricRegistryPath $Root
    if (-not (Test-Path -LiteralPath $path)) { throw 'No protected schema 2 installation record exists in HKLM. Refusing legacy/unattested in-place maintenance; install to a fresh Root.' }
    $acl = Get-Acl -LiteralPath $path
    $trusted = @(Get-FabricTrustedSids)
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $trusted) { throw 'Installation registry record has an untrusted owner.' }
    $writeMask = [int64][Security.AccessControl.RegistryRights]'SetValue,CreateSubKey,Delete,ChangePermissions,TakeOwnership'
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq [Security.AccessControl.AccessControlType]::Allow -and $rule.IdentityReference.Value -notin $trusted -and ([int64]$rule.RegistryRights -band $writeMask)) {
            throw 'Installation registry record is writable by a non-administrator.'
        }
    }
    $anchor = Get-ItemProperty -LiteralPath $path
    if ($anchor.SchemaVersion -ne 2 -or $anchor.Root -ine $Root -or $anchor.WorkerSid -ne $WorkerSid) { throw 'Protected installation record does not match this Root/account/schema.' }
}

function New-FabricRegistryAnchor([string]$Root, [string]$WorkerSid) {
    $path = Get-FabricRegistryPath $Root
    if (Test-Path -LiteralPath $path) { throw 'A protected installation record already exists for this new Root. Choose a fresh Root instead of replacing an unknown installation.' }
    New-Item -Path $path -Force | Out-Null
    $acl = [Security.AccessControl.RegistrySecurity]::new()
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', $WorkerSid)) {
        $rights = [Security.AccessControl.RegistryRights]::FullControl
        if ($sid -eq $WorkerSid) { $rights = [Security.AccessControl.RegistryRights]::ReadKey }
        $acl.AddAccessRule([Security.AccessControl.RegistryAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), $rights, [Security.AccessControl.InheritanceFlags]::None, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow))
    }
    Set-Acl -LiteralPath $path -AclObject $acl
    New-ItemProperty -Path $path -Name SchemaVersion -Value 2 -PropertyType DWord -Force | Out-Null
    New-ItemProperty -Path $path -Name Root -Value $Root -PropertyType String -Force | Out-Null
    New-ItemProperty -Path $path -Name WorkerSid -Value $WorkerSid -PropertyType String -Force | Out-Null
}

function Assert-FabricInstall([string]$Root) {
    $canonical = Assert-FabricRootLocation $Root
    Assert-FabricProtectedPath $canonical
    $marker = Join-Path $canonical 'installation.json'
    Assert-FabricProtectedPath $marker
    $info = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if (-not ($info.PSObject.Properties.Name -contains 'schema_version') -or $info.schema_version -ne 2) {
        throw 'Legacy/unknown installation schema is unsafe for in-place maintenance. Use a fresh ProgramData root; migrate run data manually without executing legacy code.'
    }
    if ($info.root -ine $canonical) { throw 'Installation root does not match protected metadata.' }
    Assert-FabricRegistryAnchor $canonical $info.worker_sid
    if ($info.worker_user -notmatch '^([a-zA-Z0-9_.-]+)\\([a-zA-Z0-9_.-]+)$' -or $Matches[1] -ine $env:COMPUTERNAME) {
        throw 'Recorded worker identity must be a local account on this computer.'
    }
    $localName = $Matches[2]
    $liveUser = Get-LocalUser -Name $localName -ErrorAction Stop
    if (-not $liveUser.Enabled -or $liveUser.SID.Value -ne $info.worker_sid) { throw 'Worker account is disabled or its live SID differs from protected installation metadata.' }
    $adminMembers = @(Get-LocalGroupMember -SID 'S-1-5-32-544' -ErrorAction Stop | ForEach-Object { $_.SID.Value })
    if ($liveUser.SID.Value -in $adminMembers) { throw 'The configured worker account is now an administrator. Restore a regular compute account before maintenance.' }
    Assert-FabricOperationalPaths $canonical $info
    return $info
}

function Assert-FabricOperationalPaths([string]$Root, $Info) {
    if ($Info.task_name -notmatch '^[A-Za-z0-9_.-]+$') { throw 'Installation metadata contains an invalid scheduled task name.' }
    $configPath = Join-Path $Root 'worker.json'
    $tokenPath = Join-Path $Root 'worker.token'
    $runtimePython = Join-Path $Root 'worker-runtime\Scripts\python.exe'
    $launcher = Join-Path $Root 'worker-launcher.py'
    foreach ($path in @($configPath, $tokenPath, $runtimePython, $launcher)) { Assert-FabricProtectedPath $path }
    $config = Get-Content -LiteralPath $configPath -Raw | ConvertFrom-Json
    $fields = @($config.PSObject.Properties.Name)
    if ('host' -notin $fields -or $config.host -ne '127.0.0.1' -or
        'port' -notin $fields -or ($config.port -isnot [int] -and $config.port -isnot [long]) -or [int]$config.port -lt 1024 -or [int]$config.port -gt 65535 -or
        'root' -notin $fields -or [string]$config.root -ine (Join-Path $Root 'data') -or
        'token_file' -notin $fields -or [string]$config.token_file -ine $tokenPath -or
        'gpu_lease_path' -notin $fields -or [string]$config.gpu_lease_path -ine [string]$Info.gpu_lease_path) {
        throw 'Protected worker configuration violates the installed loopback, token, data-root or GPU-lease boundary.'
    }
    if (-not [IO.Path]::IsPathRooted([string]$Info.gpu_lease_path) -or [string]$Info.gpu_lease_path -like '\\*') {
        throw 'Installation metadata contains an invalid GPU lease path.'
    }
    $leasePath = [IO.Path]::GetFullPath([string]$Info.gpu_lease_path)
    $expectedParent = [IO.Path]::GetFullPath((Join-Path $Root 'state')).TrimEnd('\')
    $actualParent = [IO.Directory]::GetParent($leasePath)
    if ($null -eq $actualParent -or $actualParent.FullName.TrimEnd('\') -ine $expectedParent) {
        throw 'GPU lease must remain directly inside this installation state directory.'
    }
    Assert-FabricNoReparseAncestors $leasePath
    if (-not (Test-Path -LiteralPath $expectedParent -PathType Container)) { throw 'Protected worker state directory is missing.' }
    if ((Get-Item -LiteralPath $expectedParent -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw 'Worker state directory cannot be a reparse point.'
    }
}

function Set-FabricDirectoryAcl([string]$Path, [string]$WorkerSid, [switch]$Mutable) {
    if (Test-Path -LiteralPath $Path) {
        if ((Get-Item -LiteralPath $Path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Refusing to change ACLs through a reparse point: $Path" }
    } else { New-Item -ItemType Directory -Path $Path -Force | Out-Null }
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    $acl.SetAccessRuleProtection($true, $false)
    $inherit = [Security.AccessControl.InheritanceFlags]'ContainerInherit,ObjectInherit'
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', $WorkerSid)) {
        $rights = [Security.AccessControl.FileSystemRights]::FullControl
        if ($sid -eq $WorkerSid) {
            $rights = [Security.AccessControl.FileSystemRights]::ReadAndExecute
            if ($Mutable) { $rights = [Security.AccessControl.FileSystemRights]::Modify }
        }
        $rule = [Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), $rights, $inherit, [Security.AccessControl.PropagationFlags]::None, [Security.AccessControl.AccessControlType]::Allow)
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}
