#Requires -Version 5.1
#Requires -RunAsAdministrator
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$MacAddress,
    [Parameter(Mandatory)][string]$PublicKeyPath,
    [string]$Root = 'C:\ProgramData\ZeYuComputeFabric',
    [ValidateRange(1024,65535)][int]$RuntimePort = 9876,
    [ValidateSet('Private','Domain','Public','Any')][string[]]$FirewallProfile = @('Private'),
    [switch]$RestrictExistingDefaultFirewallRule
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
$parsedAddress = $null
if (-not [Net.IPAddress]::TryParse($MacAddress, [ref]$parsedAddress) -or [Net.IPAddress]::IsLoopback($parsedAddress) -or $MacAddress -in @('0.0.0.0','::')) {
    throw 'MacAddress must be one concrete reachable Mac/VPN IP address, never Any, a hostname, or a whole subnet.'
}
$MacAddress = $parsedAddress.ToString()
$install = Assert-FabricInstall $Root
$Root = $install.root
$workerConfig = Get-Content -LiteralPath (Join-Path $Root 'worker.json') -Raw | ConvertFrom-Json
$workerUser = ($install.worker_user -split '\\')[-1].ToLowerInvariant()
$publicKey = (Get-Content -LiteralPath $PublicKeyPath -Raw).Trim()
if ($publicKey -notmatch '^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) [A-Za-z0-9+/]+={0,3}(?: [^\r\n]*)?$') {
    throw 'Supply a single plain OpenSSH public key, not a private key or an authorized_keys option line.'
}
$sshDir = Join-Path $env:ProgramData 'ssh'
$configPath = Join-Path $sshDir 'sshd_config'
$original = $null
if (Test-Path -LiteralPath $configPath) {
    $original = [IO.File]::ReadAllText($configPath)
    $portMatch = [regex]::Match($original, '(?im)^\s*Port\s+(\d+)\s*(?:#.*)?$')
    if ($portMatch.Success -and $portMatch.Groups[1].Value -ne '22') { throw 'Existing SSH uses a custom port. Reuse that server manually; this helper only manages port 22.' }
}
$capability = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
$defaultRuleBefore = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
$managedRuleName = 'ZeYuComputeFabric-SSH'
$conflicts = @()
# Do not silently broaden or rewrite unrelated existing SSH access.
foreach ($rule in @(Get-NetFirewallRule -Enabled True -Direction Inbound -Action Allow)) {
    if ($rule.Name -eq $managedRuleName) { continue }
    $ports = $rule | Get-NetFirewallPortFilter
    if ([string]$ports.Protocol -notin @('TCP','6','Any','256')) { continue }
    $coversSsh = $false
    foreach ($portValue in @($ports.LocalPort)) {
        if ([string]$portValue -in @('22','Any')) { $coversSsh=$true }
        elseif ([string]$portValue -match '^(\d+)-(\d+)$' -and [int]$Matches[1] -le 22 -and [int]$Matches[2] -ge 22) { $coversSsh=$true }
    }
    if (-not $coversSsh) { continue }
    $program = [string](($rule | Get-NetFirewallApplicationFilter).Program)
    $service = [string](($rule | Get-NetFirewallServiceFilter).Service)
    if ($program -ne 'Any' -and $program -notmatch '(?i)[\\/]sshd\.exe$') { continue }
    if ($service -notin @('Any','sshd')) { continue }
    $addresses = @(($rule | Get-NetFirewallAddressFilter).RemoteAddress)
    if ($addresses.Count -eq 1 -and $addresses[0] -eq $MacAddress) { continue }
    if ($rule.Name -eq 'OpenSSH-Server-In-TCP' -and $RestrictExistingDefaultFirewallRule) { continue }
    $conflicts += $rule.Name
}
if ($conflicts.Count -gt 0) {
    throw ('Existing SSH allow rules have a broader source scope: ' + ($conflicts -join ', ') + '. Review them first. -RestrictExistingDefaultFirewallRule explicitly narrows only the standard OpenSSH rule; other rules are never rewritten.')
}
if ($capability.State -ne 'Installed') {
    $installed = Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
    # Windows adds an unrestricted default allow rule. Narrow this newly created rule before starting sshd.
    if (-not $defaultRuleBefore) {
        Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Set-NetFirewallRule -RemoteAddress $MacAddress -Profile $FirewallProfile
    }
    if ($installed.RestartNeeded) { throw 'Windows requires a restart to complete OpenSSH installation. Restart once, then rerun this helper.' }
}
if ($RestrictExistingDefaultFirewallRule -and $defaultRuleBefore) {
    $defaultRuleBefore | Set-NetFirewallRule -RemoteAddress $MacAddress -Profile $FirewallProfile
}
$sshd = Join-Path $env:windir 'System32\OpenSSH\sshd.exe'
$sshKeygen = Join-Path $env:windir 'System32\OpenSSH\ssh-keygen.exe'
if (-not (Test-Path -LiteralPath $sshd)) { throw 'The built-in Windows sshd.exe was not found after capability installation.' }
New-Item -ItemType Directory -Path $sshDir -Force | Out-Null
$keysDir = Join-Path $Root 'ssh'
if (Test-Path -LiteralPath $keysDir) { Assert-FabricProtectedPath $keysDir -Recurse }
Set-FabricDirectoryAcl $keysDir $install.worker_sid
$authorizedKeys = Join-Path $keysDir 'authorized_keys'
$keyLines = @()
if (Test-Path -LiteralPath $authorizedKeys) { $keyLines = @(Get-Content -LiteralPath $authorizedKeys | Where-Object { $_.Trim() }) }
if ($keyLines -notcontains $publicKey) { $keyLines += $publicKey }
[IO.File]::WriteAllText($authorizedKeys, (($keyLines -join "`n") + "`n"), [Text.UTF8Encoding]::new($false))
$keyAcl = [Security.AccessControl.FileSecurity]::new()
$keyAcl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
$keyAcl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($install.worker_sid, 'S-1-5-18', 'S-1-5-32-544')) {
    $keyRights = [Security.AccessControl.FileSystemRights]::FullControl
    if ($sid -eq $install.worker_sid) { $keyRights = [Security.AccessControl.FileSystemRights]::Read }
    $keyAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid), $keyRights, [Security.AccessControl.AccessControlType]::Allow))
}
Set-Acl -LiteralPath $authorizedKeys -AclObject $keyAcl
if ($null -eq $original) {
    $original = "Port 22`r`nPubkeyAuthentication yes`r`nPasswordAuthentication no`r`nAllowUsers $workerUser`r`nSubsystem sftp sftp-server.exe`r`n"
}
$base = [regex]::Replace($original, '(?ms)^# BEGIN ZEYU COMPUTE FABRIC\r?\n.*?^# END ZEYU COMPUTE FABRIC\r?\n?', '')
$keyConfigPath = $authorizedKeys.Replace('\','/')
$block = @"
# BEGIN ZEYU COMPUTE FABRIC
Match User $workerUser
    AuthorizedKeysFile "$keyConfigPath"
    PubkeyAuthentication yes
    PasswordAuthentication no
    AuthenticationMethods publickey
    AllowTcpForwarding local
    PermitOpen 127.0.0.1:$($workerConfig.port) 127.0.0.1:$RuntimePort
    AllowAgentForwarding no
    PermitTTY no
# END ZEYU COMPUTE FABRIC
"@
$firstMatch = [regex]::Match($base, '(?im)^\s*Match\s+')
if ($firstMatch.Success) { $candidate = $base.Insert($firstMatch.Index, $block + "`r`n") }
else { $candidate = $base.TrimEnd() + "`r`n" + $block + "`r`n" }
& $sshKeygen -A
if ($LASTEXITCODE -ne 0) { throw 'SSH host key generation failed.' }
$candidatePath = Join-Path $sshDir 'sshd_config.zeyu-candidate'
[IO.File]::WriteAllText($candidatePath, $candidate, [Text.UTF8Encoding]::new($false))
try {
    & $sshd -t -f $candidatePath
    if ($LASTEXITCODE -ne 0) { throw 'Candidate SSH config failed validation; existing sshd_config preserved.' }
    $effective = @(& $sshd -T -f $candidatePath -C "user=$workerUser,host=localhost,addr=$MacAddress")
    $effectiveText = $effective -join "`n"
    if ($LASTEXITCODE -ne 0 -or $effective -notcontains 'passwordauthentication no' -or $effective -notcontains 'authenticationmethods publickey' -or
        $effectiveText -notmatch [regex]::Escape("127.0.0.1:$($workerConfig.port)") -or $effectiveText -notmatch [regex]::Escape("127.0.0.1:$RuntimePort")) {
        throw 'Effective SSH configuration did not enforce expected public-key and forwarding restrictions; existing config preserved.'
    }
    if (Test-Path -LiteralPath $configPath) {
        Copy-Item -LiteralPath $configPath -Destination ($configPath + '.pre-zeyu-' + [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssfffZ'))
    }
    [IO.File]::WriteAllText($configPath, $candidate, [Text.UTF8Encoding]::new($false))
} finally { Remove-Item -LiteralPath $candidatePath -Force -ErrorAction SilentlyContinue }
if (Get-NetFirewallRule -Name $managedRuleName -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule -Name $managedRuleName -Enabled True -Direction Inbound -Action Allow -Profile $FirewallProfile -RemoteAddress $MacAddress
} else {
    New-NetFirewallRule -Name $managedRuleName -DisplayName 'ZeYu Compute Fabric SSH from paired Mac' -Direction Inbound -Protocol TCP -LocalPort 22 -Action Allow -Enabled True -Profile $FirewallProfile -RemoteAddress $MacAddress -Program $sshd | Out-Null
}
try {
    Set-Service -Name sshd -StartupType Automatic
    Restart-Service -Name sshd
} catch {
    if ($null -ne $original -and (Test-Path -LiteralPath $configPath)) {
        # Restore the original text, not a generated partial candidate, if service startup fails.
        [IO.File]::WriteAllText($configPath, $original, [Text.UTF8Encoding]::new($false))
        Restart-Service -Name sshd -ErrorAction SilentlyContinue
    }
    throw 'SSH service restart failed. Prior config text was restored; inspect the OpenSSH Operational event log. Firewall remains restricted.'
}
Write-Host "SSH configuration applied for $workerUser from $MacAddress on profile(s) $($FirewallProfile -join ', '). Verify an actual Mac login. API ports $($workerConfig.port) and $RuntimePort remain loopback only."
Write-Host 'Compare this host-key fingerprint with the Mac before accepting the first SSH connection:'
& $sshKeygen -lf (Join-Path $sshDir 'ssh_host_ed25519_key.pub')
if ($LASTEXITCODE -ne 0) { throw 'Could not display the host-key fingerprint.' }
