$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RepoUrl = "https://github.com/LEFTX1/antigravity-token-heatmap.git"
$PluginRoot = Join-Path $HOME ".gemini\config\plugins"
$TargetDir = Join-Path $PluginRoot "token-heatmap"

Write-Host ">>> Installing Antigravity Token Heatmap Cockpit..." -ForegroundColor Cyan

New-Item -ItemType Directory -Force -Path $PluginRoot | Out-Null

$hasAgy = $null -ne (Get-Command agy -ErrorAction SilentlyContinue)
$hasGit = $null -ne (Get-Command git -ErrorAction SilentlyContinue)

if (Test-Path (Join-Path $TargetDir ".git")) {
    Write-Host ">>> Updating existing git installation in $TargetDir" -ForegroundColor Gray
    git -C $TargetDir pull --ff-only
} elseif (Test-Path $TargetDir) {
    Write-Host ">>> Backing up existing non-git plugin folder..." -ForegroundColor Yellow
    $bak = "$TargetDir.bak.$(Get-Date -Format 'yyyyMMddHHmmss')"
    Move-Item -Path $TargetDir -Destination $bak
    if ($hasGit) {
        git clone --depth 1 $RepoUrl $TargetDir
    }
    $oldData = Join-Path $bak "data"
    if (Test-Path $oldData) {
        Copy-Item -Recurse -Force $oldData (Join-Path $TargetDir "data")
    }
} elseif ($hasAgy) {
    Write-Host ">>> Installing via Antigravity CLI (agy)..." -ForegroundColor Gray
    agy plugin install https://github.com/LEFTX1/antigravity-token-heatmap
} elseif ($hasGit) {
    Write-Host ">>> Cloning plugin into $TargetDir ..." -ForegroundColor Gray
    git clone --depth 1 $RepoUrl $TargetDir
} else {
    Write-Host ">>> Downloading zip archive..." -ForegroundColor Gray
    $zipPath = Join-Path $env:TEMP "antigravity-token-heatmap.zip"
    $extractPath = Join-Path $env:TEMP "antigravity-token-heatmap-extract"
    Invoke-WebRequest -Uri "https://github.com/LEFTX1/antigravity-token-heatmap/archive/refs/heads/main.zip" -OutFile $zipPath
    if (Test-Path $extractPath) { Remove-Item -Recurse -Force $extractPath }
    Expand-Archive -Path $zipPath -DestinationPath $extractPath -Force
    $inner = Get-ChildItem $extractPath -Directory | Select-Object -First 1
    Move-Item -Path $inner.FullName -Destination $TargetDir
    Remove-Item -Force $zipPath -ErrorAction SilentlyContinue
}

$pyCmd = Get-Command python -ErrorAction SilentlyContinue
if (-not $pyCmd) { $pyCmd = Get-Command py -ErrorAction SilentlyContinue }
if ($pyCmd) {
    Write-Host ">>> Checking Python MCP SDK dependency..." -ForegroundColor Gray
    & $pyCmd.Source -c "import mcp" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host ">>> Installing mcp>=1.2.0 via pip..." -ForegroundColor Yellow
        & $pyCmd.Source -m pip install --upgrade "mcp>=1.2.0"
    } else {
        Write-Host ">>> Python mcp package already installed." -ForegroundColor Green
    }
} else {
    Write-Warning "Python 3.10+ not found on PATH. Please install Python 3.10+ and run: pip install mcp>=1.2.0"
}

Write-Host ""
Write-Host "Installation complete!" -ForegroundColor Green
Write-Host "Installed at: $TargetDir" -ForegroundColor Cyan
Write-Host "Next step: Reload plugins in Antigravity or ask Antigravity: '打开我的 Token 热力图'" -ForegroundColor White
