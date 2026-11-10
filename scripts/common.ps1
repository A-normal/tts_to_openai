Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-ProjectRoot { return [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')) }

function Open-RuntimeRepairLease([string]$Runtime) {
    [IO.Directory]::CreateDirectory($Runtime) | Out-Null
    try {
        return [IO.File]::Open((Join-Path $Runtime '.runtime.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    } catch [IO.IOException] {
        throw 'Cannot lock the shared runtime for setup. Stop its gateway instances and retry.'
    }
}

function Read-Jsonc([string]$Path) {
    $source = [IO.File]::ReadAllText([IO.Path]::GetFullPath($Path), [Text.Encoding]::UTF8)
    $output = $source.ToCharArray()
    $quoted = $false
    $i = 0
    while ($i -lt $source.Length) {
        $c = $source[$i]
        if ($quoted) {
            if ($c -eq '\') { $i += 2; continue }
            if ($c -eq '"') { $quoted = $false }
        } elseif ($c -eq '"') { $quoted = $true
        } elseif ($i + 1 -lt $source.Length -and $source.Substring($i, 2) -eq '//') {
            while ($i -lt $source.Length -and $source[$i] -ne "`r" -and $source[$i] -ne "`n") { $output[$i] = ' '; $i++ }
            continue
        } elseif ($i + 1 -lt $source.Length -and $source.Substring($i, 2) -eq '/*') {
            $output[$i] = ' '; $output[$i+1] = ' '; $i += 2
            while ($i + 1 -lt $source.Length -and $source.Substring($i, 2) -ne '*/') {
                if ($source[$i] -ne "`r" -and $source[$i] -ne "`n") { $output[$i] = ' ' }; $i++
            }
            if ($i + 1 -ge $source.Length) { throw 'Unterminated JSONC comment' }
            $output[$i] = ' '; $output[$i+1] = ' '; $i += 2; continue
        }
        $i++
    }
    return (-join $output) | ConvertFrom-Json
}

function Set-PrivateAcl([string]$Path, [bool]$Directory = $false) {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
    if ($Directory) { $acl = New-Object Security.AccessControl.DirectorySecurity }
    else { $acl = New-Object Security.AccessControl.FileSecurity }
    $acl.SetOwner($sid)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($id in @($sid.Value, 'S-1-5-18', 'S-1-5-32-544')) {
        $principal = New-Object Security.Principal.SecurityIdentifier($id)
        if ($Directory) {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($principal, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
        } else {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($principal, 'FullControl', 'Allow')
        }
        $acl.AddAccessRule($rule)
    }
    # Set only DACL/protection. Set-Acl may also attempt SACL/owner writes on an
    # existing object and unnecessarily require SeSecurityPrivilege.
    if (-not ('GSVGateway.NativeAcl' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace GSVGateway {
    public static class NativeAcl {
        [DllImport("advapi32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
        public static extern bool SetFileSecurity(string path, uint information, byte[] descriptor);
    }
}
'@
    }
    $descriptor = $acl.GetSecurityDescriptorBinaryForm()
    if (-not [GSVGateway.NativeAcl]::SetFileSecurity([IO.Path]::GetFullPath($Path), [uint32]2147483652, $descriptor)) {
        throw (New-Object ComponentModel.Win32Exception([Runtime.InteropServices.Marshal]::GetLastWin32Error()))
    }
}

function Initialize-GatewayTokens([string]$SecretsDirectory) {
    [IO.Directory]::CreateDirectory($SecretsDirectory) | Out-Null
    Set-PrivateAcl $SecretsDirectory $true
    $target = Join-Path $SecretsDirectory 'api-tokens.jsonc'
    if (Test-Path -LiteralPath $target) { return }

    $random = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($random) } finally { $rng.Dispose() }
    $token = [Convert]::ToBase64String($random).TrimEnd('=').Replace('+','-').Replace('/','_')
    $contents = @{ tokens = @(@{ id = 'default'; token = $token; enabled = $true }) } | ConvertTo-Json -Depth 4
    $pending = Join-Path $SecretsDirectory ('.pending-' + [Guid]::NewGuid().ToString('N'))
    try {
        [IO.File]::WriteAllText($pending, $contents, (New-Object Text.UTF8Encoding($false)))
        Set-PrivateAcl $pending
        [IO.File]::Move($pending, $target)
    } finally {
        if (Test-Path -LiteralPath $pending) { Remove-Item -LiteralPath $pending -Force }
    }
}

function Assert-ChildPath([string]$Root, [string]$Target) {
    $base = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $resolved = [IO.Path]::GetFullPath($Target)
    if (-not $resolved.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) { throw 'Target is outside managed directory' }
    return $resolved
}

function Get-PathPython {
    Get-Command python.exe -All -CommandType Application -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Source } |
        Where-Object { $_ -notmatch '[\\/]Microsoft[\\/]WindowsApps[\\/]' } |
        Select-Object -Unique
}

function Test-GatewayPython([string]$Root, [string]$Executable, [switch]$BaseOnly) {
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return }
    $probe = Join-Path $Root 'gsv_gateway\python_probe.py'
    $process = New-Object Diagnostics.Process
    try {
        $process.StartInfo.FileName = $Executable
        $process.StartInfo.Arguments = '-I "' + $probe + '" --project-root "' + $Root + '"'
        if ($BaseOnly) { $process.StartInfo.Arguments += ' --base-only' }
        $process.StartInfo.UseShellExecute = $false
        $process.StartInfo.RedirectStandardOutput = $true
        $process.StartInfo.RedirectStandardError = $true
        [void]$process.Start()
        $output = $process.StandardOutput.ReadToEndAsync()
        $errors = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(10000)) {
            $process.Kill()
            $process.WaitForExit()
            return
        }
        if ($process.ExitCode -eq 0 -and $output.Wait(1000) -and $errors.Wait(1000)) {
            return ($output.Result | ConvertFrom-Json)
        }
    } catch {
        # A candidate that cannot be probed is not usable.
    } finally { $process.Dispose() }
}

function Get-PreparedPython([string]$Root) {
    $runtime = Join-Path $Root 'runtime'
    if (Test-Path -LiteralPath (Join-Path $runtime 'installing.json')) { throw 'Previous setup is incomplete. Run setup again explicitly.' }
    $pathVenv = Join-Path $runtime 'venv-path\Scripts\python.exe'
    $pathBases = @()
    foreach ($exe in (Get-PathPython)) {
        $candidate = Test-GatewayPython $Root $exe
        if ($candidate) {
            if ($candidate.ready) { return $exe }
            $pathBases += $candidate.base
        }
    }
    if ($pathBases.Count) {
        $candidate = Test-GatewayPython $Root $pathVenv
        if ($candidate -and $candidate.ready -and $candidate.base -in $pathBases) { return $pathVenv }
    }
    $managedVenv = Join-Path $runtime 'venv\Scripts\python.exe'
    $candidate = Test-GatewayPython $Root $managedVenv
    if ($candidate -and $candidate.ready) {
        [void](Assert-ChildPath (Join-Path $runtime 'python') $candidate.base)
        return $managedVenv
    }
    throw 'No compatible Python with the locked gateway dependencies. Run scripts\setup.ps1 -Online.'
}

function Expand-SafeZip([string]$Archive, [string]$Target) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [IO.Directory]::CreateDirectory($Target) | Out-Null
    $zip = [IO.Compression.ZipFile]::OpenRead($Archive)
    try {
        foreach ($entry in $zip.Entries) {
            if ([IO.Path]::IsPathRooted($entry.FullName)) { throw 'Unsafe archive entry' }
            $destination = Assert-ChildPath $Target (Join-Path $Target $entry.FullName)
            if (($entry.ExternalAttributes -shr 16 -band 0xF000) -eq 0xA000) { throw 'Archive symlink rejected' }
            if ($entry.FullName.EndsWith('/')) { [IO.Directory]::CreateDirectory($destination) | Out-Null; continue }
            [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($destination)) | Out-Null
            [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination, $true)
        }
    } finally { $zip.Dispose() }
}

function Get-VerifiedArchive($Entry, [string]$Runtime, [bool]$Online) {
    if (-not $Entry.sha256 -or $Entry.sha256 -notmatch '^[0-9a-fA-F]{64}$') { throw 'Missing locked archive SHA-256' }
    $target = Assert-ChildPath $Runtime (Join-Path $Runtime $Entry.archive)
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
    if (-not (Test-Path -LiteralPath $target)) {
        if (-not $Online) { throw 'Archive missing from verified local cache; rerun with -Online or supply the locked archive.' }
        $pending = $target + '.partial'
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $Entry.url -OutFile $pending -UseBasicParsing -TimeoutSec 120
        if ((Get-FileHash -LiteralPath $pending -Algorithm SHA256).Hash -ne $Entry.sha256) {
            Remove-Item -LiteralPath $pending -Force
            throw 'Archive SHA-256 mismatch'
        }
        Move-Item -LiteralPath $pending -Destination $target
    }
    if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne $Entry.sha256) { throw 'Cached archive SHA-256 mismatch' }
    return $target
}
