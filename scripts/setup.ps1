param([switch]$Online, [switch]$Dev, [switch]$InitConfig)
. (Join-Path $PSScriptRoot 'common.ps1')
$root=Get-ProjectRoot
$runtime=Join-Path $root 'runtime'
$manifestPath=Join-Path $root 'runtime-manifest.json'
# During upgrades, older running GSV gateways still hold only this mutex.
$created=$false
$mutex=New-Object Threading.Mutex($true, 'Global\GSVGateway-backend-gpt-sovits-127.0.0.1-9880', [ref]$created)
if (-not $created) { $mutex.Dispose(); throw 'Stop all gateway instances before repairing the shared runtime.' }
$names=@('UV_PYTHON_INSTALL_DIR','UV_CACHE_DIR','UV_PROJECT_ENVIRONMENT','UV_PYTHON_INSTALL_BIN','UV_PYTHON_INSTALL_REGISTRY','UV_OFFLINE','UV_NO_CONFIG','UV_PYTHON_INSTALL_MIRROR','UV_PYTHON_DOWNLOADS_JSON_URL','PYTHONHOME','PYTHONPATH','PYTHONNOUSERSITE')
$prior=@{};foreach($name in $names){$prior[$name]=[Environment]::GetEnvironmentVariable($name,'Process')}
$location=Get-Location
$runtimeLease=$null
try {
    if (-not (Test-Path -LiteralPath $manifestPath)) { throw 'Locked runtime-manifest.json is required.' }
    $manifest=Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    $runtimeLease=Open-RuntimeRepairLease $runtime
    [IO.File]::WriteAllText((Join-Path $runtime 'installing.json'), '{"status":"incomplete"}')
    $env:UV_PYTHON_INSTALL_DIR=Join-Path $runtime 'python'
    $env:UV_CACHE_DIR=Join-Path $runtime 'cache\uv'
    $env:UV_PYTHON_INSTALL_BIN='false'; $env:UV_PYTHON_INSTALL_REGISTRY='false'; $env:UV_NO_CONFIG='true'
    $env:UV_OFFLINE=$(if($Online){'false'}else{'true'})
    Remove-Item Env:\UV_PYTHON_INSTALL_MIRROR,Env:\UV_PYTHON_DOWNLOADS_JSON_URL,Env:\PYTHONHOME,Env:\PYTHONPATH -ErrorAction SilentlyContinue
    $env:PYTHONNOUSERSITE='1'
    Set-Location -LiteralPath $root
    $pathBase=$null; $python=$null
    foreach ($exe in (Get-PathPython)) {
        $candidate=Test-GatewayPython $root $exe
        if ($candidate) {
            if (-not $pathBase) { $pathBase=$candidate.base }
            if ($candidate.ready -and -not $Dev) { $python=$exe; break }
        }
    }
    if (-not $python) {
        $uvArchive=Get-VerifiedArchive $manifest.uv $runtime $Online.IsPresent
        Expand-SafeZip $uvArchive (Join-Path $runtime 'tools')
        $uv=Join-Path $runtime 'tools\uv.exe'
        if ($pathBase) {
            $base=$pathBase
            $env:UV_PROJECT_ENVIRONMENT=Join-Path $runtime 'venv-path'
        } else {
            $base=Assert-ChildPath $runtime (Join-Path $runtime $manifest.python.executable)
            if (-not (Test-GatewayPython $root $base -BaseOnly)) {
                & $uv python install $manifest.python.version --no-config
                if ($LASTEXITCODE -ne 0) { throw 'Managed Python installation failed.' }
            }
            $env:UV_PROJECT_ENVIRONMENT=Join-Path $runtime 'venv'
        }
        $sync=@('sync','--locked','--python',$base,'--no-config','--link-mode','copy')
        if (-not $Dev) { $sync+='--no-dev' }
        if (-not $Online) { $sync+='--offline' }
        & $uv @sync
        if ($LASTEXITCODE -ne 0) { throw 'Locked dependency synchronization failed.' }
        $python=Join-Path $env:UV_PROJECT_ENVIRONMENT 'Scripts\python.exe'
    }
    & $python -m gsv_gateway.bootstrap --runtime-root $runtime
    if ($LASTEXITCODE -eq 2) {
        $ffmpeg=Assert-ChildPath $runtime (Join-Path $runtime $manifest.ffmpeg.ffmpeg)
        $ffprobe=Assert-ChildPath $runtime (Join-Path $runtime $manifest.ffmpeg.ffprobe)
        if ((Test-Path -LiteralPath $ffmpeg -PathType Leaf) -and (Test-Path -LiteralPath $ffprobe -PathType Leaf)) {
            & $python -m gsv_gateway.bootstrap --runtime-root $runtime --ffmpeg $ffmpeg --ffprobe $ffprobe
        }
        if ($LASTEXITCODE -ne 0) {
            $ffArchive=Get-VerifiedArchive $manifest.ffmpeg $runtime $Online.IsPresent
            Expand-SafeZip $ffArchive (Join-Path $runtime 'ffmpeg')
            & $python -m gsv_gateway.bootstrap --runtime-root $runtime --ffmpeg $ffmpeg --ffprobe $ffprobe
        }
    }
    if ($LASTEXITCODE -ne 0) { throw 'Runtime/audio validation failed; environment is incomplete.' }
    if ($InitConfig) {
        $configRoot=Join-Path $root 'config'
        foreach ($name in @('gateway','voices')) {
            $target=Join-Path $configRoot ($name+'.jsonc')
            if (-not (Test-Path -LiteralPath $target)) { Copy-Item -LiteralPath (Join-Path $configRoot ($name+'.example.jsonc')) -Destination $target }
        }
        Initialize-GatewayTokens (Join-Path $configRoot 'secrets')
    }
    Remove-Item -LiteralPath (Join-Path $runtime 'installing.json') -Force
    Write-Output 'ENV_READY / APP_CONFIG_PENDING: tools prepared. Configure assets and run doctor; no gateway/TTS process was started.'
    exit 0
} catch { Write-Error $_.Exception.Message; exit 1
} finally {
    foreach($name in $names){[Environment]::SetEnvironmentVariable($name,$prior[$name],'Process')}
    Set-Location -LiteralPath $location
    if ($runtimeLease) { $runtimeLease.Dispose() }
    $mutex.ReleaseMutex();$mutex.Dispose()
}
