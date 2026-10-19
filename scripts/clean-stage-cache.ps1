param(
    [Parameter(Mandatory = $true)][ValidateRange(0, 7)][int]$Stage,
    [string[]]$UvPackages = @()
)

$ErrorActionPreference = 'Stop'
$cacheProjectRoot = Split-Path -Parent $PSScriptRoot
# 未指定新增包时，定向清理本项目锁定依赖的下载缓存，保留已安装环境和模型。
# 使用参数数组交给包管理器，不自行递归删除系统目录。
if ($UvPackages.Count -eq 0) {
    $cachePython = Join-Path $cacheProjectRoot 'backend/.venv/Scripts/python.exe'
    $cacheLock = Join-Path $cacheProjectRoot 'backend/uv.lock'
    $embeddingCacheLock = Join-Path $cacheProjectRoot 'embedding/uv.lock'
    if ((Test-Path -LiteralPath $cachePython) -and (Test-Path -LiteralPath $cacheLock)) {
        $cacheLocks = @($cacheLock)
        if (Test-Path -LiteralPath $embeddingCacheLock) { $cacheLocks += $embeddingCacheLock }
        $UvPackages = @(& $cachePython -c 'import sys,tomllib; from pathlib import Path; packages={p["name"] for name in sys.argv[1:] for p in tomllib.loads(Path(name).read_text(encoding="utf-8"))["package"]}; print("\n".join(sorted(packages)))' @cacheLocks)
        if ($LASTEXITCODE -ne 0) { throw '无法读取项目锁定依赖，停止缓存清理。' }
    }
}
$systemUvCache = [IO.Path]::GetFullPath((Join-Path $env:LOCALAPPDATA 'uv/cache'))
if ([IO.Path]::GetPathRoot($systemUvCache) -ne 'C:\') {
    throw '预期的系统盘缓存目录不在 C 盘，停止清理。'
}
foreach ($package in $UvPackages) {
    if ($package -notmatch '^[a-zA-Z0-9][a-zA-Z0-9._-]*$') {
        throw '依赖名称格式无效，停止清理。'
    }
}
function Get-CacheBytes {
    <# .SYNOPSIS
    统计已验证的系统盘依赖缓存占用，拒绝重解析路径。
    #>
    if (-not (Test-Path -LiteralPath $systemUvCache)) { return [long]0 }
    # 只统计已验证的 uv 缓存；不遍历用户下载目录或模型目录。
    $cacheItem = Get-Item -LiteralPath $systemUvCache
    if ($cacheItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw '缓存根目录是重解析点，停止清理。'
    }
    $sum = Get-ChildItem -LiteralPath $systemUvCache -File -Recurse -ErrorAction Stop |
        Measure-Object -Property Length -Sum
    return [long]$sum.Sum
}
$beforeBytes = Get-CacheBytes
if ($UvPackages.Count -gt 0 -and (Test-Path -LiteralPath $systemUvCache)) {
    # 显式指定 C 盘路径，避免环境变量将清理误指向 E 盘项目缓存。
    # 使用包管理器清理本阶段新增包，不删除虚拟环境、模型或其他项目依赖。
    & uv cache clean --cache-dir $systemUvCache @UvPackages
    if ($LASTEXITCODE -ne 0) { throw "阶段 $Stage 的依赖缓存清理失败。" }
}
$afterBytes = Get-CacheBytes
$report = [ordered]@{
    stage = $Stage
    checked_at = (Get-Date).ToString('o')
    cache_path = $systemUvCache
    packages = @($UvPackages)
    before_bytes = $beforeBytes
    after_bytes = $afterBytes
    released_bytes = [Math]::Max(0, $beforeBytes - $afterBytes)
    future_cache_root = (Join-Path $cacheProjectRoot '.cache')
    status = 'completed'
}
$reportPath = Join-Path $cacheProjectRoot "docs/stages/artifacts/stage$Stage-cache-cleanup.json"
$historyPath = Join-Path $cacheProjectRoot "docs/stages/artifacts/stage$Stage-cache-cleanup.jsonl"
# 追加完整记录，避免重复验收时的零释放结果覆盖首次清理证据。
$report | ConvertTo-Json -Depth 4 -Compress | Add-Content -LiteralPath $historyPath -Encoding UTF8
$report | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $reportPath -Encoding UTF8
Write-Output "阶段 $Stage 缓存检查完成，释放 $([Math]::Round($report.released_bytes / 1MB, 2)) MiB；后续下载缓存位于项目磁盘。"
