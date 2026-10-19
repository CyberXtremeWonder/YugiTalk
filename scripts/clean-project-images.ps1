$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    # 仅处理有本项目标签，或本阶段替换前明确记录的应用镜像。
    $candidates = @(docker image ls --filter 'label=io.yugitalk.project=yugitalk' --filter 'dangling=true' --quiet --no-trunc)
    if ($LASTEXITCODE -ne 0) { throw '无法读取项目镜像' }
    $record = Join-Path $projectRoot '.cache/stage2-old-images.txt'
    if (Test-Path -LiteralPath $record) { $candidates += Get-Content -LiteralPath $record }
    $containerIds = @(docker ps -aq)
    if ($LASTEXITCODE -ne 0) { throw '无法读取容器列表' }
    $used = @()
    if ($containerIds.Count -gt 0) {
        $used = @(docker inspect --format '{{.Image}}' @containerIds)
        if ($LASTEXITCODE -ne 0) { throw '无法确认容器镜像占用' }
    }
    # 发布记录保留上一组镜像 ID，用于兼容数据库版本内回滚。
    foreach ($dataDirectory in (Get-ChildItem -LiteralPath (Join-Path $projectRoot 'data') -Directory)) {
        if ($dataDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        $releaseRecord = Join-Path $dataDirectory.FullName 'backups/last-release.json'
        if (Test-Path -LiteralPath $releaseRecord) {
            $releaseState = Get-Content -LiteralPath $releaseRecord -Raw | ConvertFrom-Json
            foreach ($imageSet in @($releaseState.previous, $releaseState.target)) {
                if ($null -ne $imageSet) {
                    $used += @($imageSet.PSObject.Properties | ForEach-Object { $_.Value })
                }
            }
        }
    }
    # 当前开发/正式发行标签、构建上一版和完整快照均属于运行或恢复资产。
    foreach ($component in @('backend', 'frontend', 'embedding')) {
        foreach ($version in @('0.1.0', '0.2.0', 'build-previous')) {
            $versionId = @(docker image ls --filter "reference=yugitalk-${component}:${version}" --quiet --no-trunc)
            if ($LASTEXITCODE -ne 0) { throw '无法确认现用或上一版镜像，停止清理。' }
            $used += $versionId
        }
    }
    foreach ($dataDirectory in (Get-ChildItem -LiteralPath (Join-Path $projectRoot 'data') -Directory)) {
        if ($dataDirectory.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        $backupDirectory = Join-Path $dataDirectory.FullName 'backups'
        if (-not (Test-Path -LiteralPath $backupDirectory)) { continue }
        if ((Get-Item -LiteralPath $backupDirectory).Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
        foreach ($snapshot in (Get-ChildItem -LiteralPath $backupDirectory -Directory)) {
            if ($snapshot.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            $manifestPath = Join-Path $snapshot.FullName 'manifest.json'
            if (-not (Test-Path -LiteralPath $manifestPath)) { continue }
            if ((Get-Item -LiteralPath $manifestPath).Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
            if ($manifest.status -eq 'passed' -and $null -ne $manifest.runtime_images) {
                $used += @($manifest.runtime_images.PSObject.Properties | ForEach-Object { $_.Value })
            }
            if ($manifest.image -match '^sha256:[0-9a-f]{64}$') { $used += $manifest.image }
        }
    }
    foreach ($imageId in ($candidates | Sort-Object -Unique)) {
        if ($imageId -notmatch '^sha256:[0-9a-f]{64}$') { continue }
        if ($used -contains $imageId) { Write-Output "保留已被容器引用的镜像：$imageId"; continue }
        $present = docker image inspect --format '{{.Id}}' $imageId 2>$null
        if ($LASTEXITCODE -ne 0) { Write-Output "旧镜像已不存在：$imageId"; continue }
        # 不使用强制删除，也不运行全局 prune，不触碰数据卷和其他项目。
        & docker image rm $present
        if ($LASTEXITCODE -ne 0) { throw "旧项目镜像删除失败：$imageId" }
    }
    # 不存在的旧镜像属于幂等成功，不能把 inspect 的退出码误当清理失败。
    $global:LASTEXITCODE = 0
    Write-Output '项目旧镜像清理检查完成。'
} finally { Pop-Location }
