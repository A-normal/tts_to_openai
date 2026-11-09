param([Parameter(ValueFromRemainingArguments = $true)][string[]]$GatewayArguments)
. (Join-Path $PSScriptRoot 'scripts\common.ps1')
$root = Get-ProjectRoot
if (-not $GatewayArguments -or $GatewayArguments.Count -eq 0) { Write-Error 'Usage: gateway.ps1 run|start|stop|restart|status|voices|doctor|check-config --config <file>'; exit 2 }
if ($GatewayArguments[0] -eq 'doctor') {
    & (Join-Path $root 'scripts\doctor.ps1') -GatewayArguments $GatewayArguments
    exit $LASTEXITCODE
}
$oldHome = $env:PYTHONHOME; $oldPath = $env:PYTHONPATH; $oldUserSite = $env:PYTHONNOUSERSITE; $location = Get-Location
try {
    $python = Get-PreparedPython $root
    Remove-Item Env:\PYTHONHOME -ErrorAction SilentlyContinue
    Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
    $env:PYTHONNOUSERSITE = '1'
    Set-Location -LiteralPath $root
    & $python -m gsv_gateway @GatewayArguments --runtime-root (Join-Path $root 'runtime')
    exit $LASTEXITCODE
} catch { Write-Error $_.Exception.Message; exit 2
} finally { $env:PYTHONHOME = $oldHome; $env:PYTHONPATH = $oldPath; $env:PYTHONNOUSERSITE = $oldUserSite; Set-Location -LiteralPath $location }
