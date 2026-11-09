param([string]$Config, [switch]$Json, [string[]]$GatewayArguments)
. (Join-Path $PSScriptRoot 'common.ps1')
$root = Get-ProjectRoot
$rows = New-Object Collections.ArrayList
function Add-Check($Code, $Status, $Message) { [void]$rows.Add(@{code=$Code; status=$Status; message=$Message}) }
if ($GatewayArguments) {
    for ($i=0; $i -lt $GatewayArguments.Count; $i++) {
        if ($GatewayArguments[$i] -eq '--config' -and $i+1 -lt $GatewayArguments.Count) { $Config=$GatewayArguments[$i+1] }
        if ($GatewayArguments[$i] -eq '--json') { $Json=$true }
    }
}
if (-not $Config) { throw 'An explicit -Config is required.' }
$Config = [IO.Path]::GetFullPath($Config)
Add-Check 'PLATFORM' $(if ([Environment]::Is64BitOperatingSystem -and $env:OS -eq 'Windows_NT') {'OK'} else {'ERROR'}) 'Windows x64 required.'
Add-Check 'POWERSHELL' $(if ($PSVersionTable.PSVersion -ge [Version]'5.1') {'OK'} else {'ERROR'}) ('PowerShell '+$PSVersionTable.PSVersion)
try {
    $cfg=Read-Jsonc $Config
    if ($cfg.schema_version -ne 1) { throw 'Unsupported schema version' }
    $voicePath=$cfg.voices_file
    if (-not [IO.Path]::IsPathRooted($voicePath)) { $voicePath=Join-Path ([IO.Path]::GetDirectoryName($Config)) $voicePath }
    $voices=Read-Jsonc $voicePath
    if ($voices.voices.Count -lt 1) { throw 'Empty voices' }
    Add-Check 'CONFIG_JSONC' 'OK' 'Configuration and voice registry parsed; deeper schema validation requires prepared Python.'
} catch { Add-Check 'CONFIG_JSONC' 'ERROR' 'Invalid/missing JSONC configuration or voice registry.' }
try { $python=Get-PreparedPython $root } catch { $python=$null; Add-Check 'PYTHON_NOT_PREPARED' 'ERROR' $_.Exception.Message }
if ($python) {
    $oldHome=$env:PYTHONHOME; $oldPath=$env:PYTHONPATH; $oldUserSite=$env:PYTHONNOUSERSITE; $location=Get-Location
    try {
        Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue
        Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
        $env:PYTHONNOUSERSITE='1'
        Set-Location -LiteralPath $root
        & $python -m gsv_gateway doctor --config $Config --runtime-root (Join-Path $root 'runtime') --json
        exit $LASTEXITCODE
    } finally { $env:PYTHONHOME=$oldHome; $env:PYTHONPATH=$oldPath; $env:PYTHONNOUSERSITE=$oldUserSite; Set-Location -LiteralPath $location }
}
Add-Check 'DEEP_CHECKS' 'SKIPPED' 'Dependency imports, assets, ACL, audio codec and backend checks require the project environment; readiness is not established.'
@{status='ENV_NOT_READY';checks=@($rows)} | ConvertTo-Json -Depth 8
exit 2
