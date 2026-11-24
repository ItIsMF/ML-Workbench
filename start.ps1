param([int]$Port = 0, [string[]]$WatchDir = @())
$ErrorActionPreference = 'Stop'
$interpreter = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $interpreter)) {
    throw '请先使用 Python 3.11+ 创建独立环境：python -m venv --without-pip .venv。详见 README。'
}
$pyArgs = @('-u', (Join-Path $PSScriptRoot 'workbench.py'), '--port', "$Port")
foreach ($dir in $WatchDir) {
    if (-not [string]::IsNullOrWhiteSpace($dir)) {
        $pyArgs += @('--watch-dir', $dir)
    }
}
& $interpreter @pyArgs
