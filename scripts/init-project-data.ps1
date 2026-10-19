# 初始化项目自身的持久目录；已存在的目录与数据保持不变。
$ErrorActionPreference = 'Stop'
$dataProjectRoot = [IO.Path]::GetFullPath((Split-Path -Parent $PSScriptRoot))
$dataPaths = @('data/postgres', 'data/redis', 'data/minio', 'data/etcd', 'data/milvus',
    'data/stage0/postgres', 'data/stage0/redis', '.cache/models', 'data/backups')
foreach ($dataRelativePath in $dataPaths) {
    $dataTarget = [IO.Path]::GetFullPath((Join-Path $dataProjectRoot $dataRelativePath))
    if (-not $dataTarget.StartsWith($dataProjectRoot + [IO.Path]::DirectorySeparatorChar)) {
        throw '持久目录超出项目边界，停止初始化。'
    }
    # 拒绝目录重解析点，避免写入项目之外。
    $dataParent = $dataTarget
    while ($dataParent -ne $dataProjectRoot) {
        if ((Test-Path -LiteralPath $dataParent) -and
            ((Get-Item -LiteralPath $dataParent).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw '持久目录包含重解析点，停止初始化。'
        }
        $dataParent = Split-Path -Parent $dataParent
    }
    [IO.Directory]::CreateDirectory($dataTarget) | Out-Null
}
Write-Output '项目持久目录已初始化。'
