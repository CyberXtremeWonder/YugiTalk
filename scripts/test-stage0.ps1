param([switch]$IncludeContainers, [switch]$IncludeUI)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'set-project-cache.ps1')
# 在全新检出中先创建绑定挂载目录，不覆盖已有数据。
& (Join-Path $PSScriptRoot 'init-project-data.ps1')

function Invoke-Checked {
    <# .SYNOPSIS
    执行验收命令并检查退出码，失败时立即终止后续检查。
    #>
    param([string]$Executable, [string[]]$TaskArguments)
    & $Executable @TaskArguments
    if ($LASTEXITCODE -ne 0) {
        throw "命令执行失败：$Executable（退出码 $LASTEXITCODE）"
    }
}

Push-Location $projectRoot
try {
    # 临时文件限定在项目目录；任何失败立即停止，不能覆盖成通过。
    Invoke-Checked 'backend/.venv/Scripts/ruff.exe' @('check', 'backend/probes', 'backend/tests')
    Invoke-Checked 'backend/.venv/Scripts/ruff.exe' @('format', '--check', 'backend/probes', 'backend/tests')
    Invoke-Checked 'backend/.venv/Scripts/mypy.exe' @('--config-file', 'backend/pyproject.toml', 'backend/probes')
    Invoke-Checked 'backend/.venv/Scripts/python.exe' @(
        '-m', 'pytest', 'backend/tests', '-m', 'not integration', '-q',
        '-p', 'no:cacheprovider', '--basetemp=.cache/pytest-stage0-script',
        '--junitxml=docs/stages/artifacts/backend-tests.xml'
    )
    Invoke-Checked 'docker' @('compose', '--project-directory', '.', '-f', 'deploy/archive/stages/docker-compose.stage0.yml', 'config', '--quiet')
    Invoke-Checked 'npm.cmd' @('--prefix', 'frontend', 'run', 'check:toolchain')

    if ($IncludeContainers) {
        # 仅启动当前项目的隔离服务，不操作其他项目的容器或数据卷。
        Invoke-Checked 'docker' @('compose', '--project-directory', '.', '-f', 'deploy/archive/stages/docker-compose.stage0.yml', 'up', '-d', '--build', '--wait', 'worker')
        Invoke-Checked 'docker' @('compose', '--project-directory', '.', '-f', 'deploy/archive/stages/docker-compose.stage0.yml', '--profile', 'test', 'run', '--rm', '--no-deps', 'tests')
    } else {
        Write-Output '未执行容器集成测试；可添加 -IncludeContainers。'
    }

    if ($IncludeUI) {
        # 浏览器测试需先启动 README 中的静态预览服务，并安装 Edge。
        Push-Location 'frontend'
        try {
            Invoke-Checked 'npx.cmd' @('playwright', 'test', '--config', 'playwright.stage0.config.ts')
        } finally { Pop-Location }
    } else {
        Write-Output '未执行浏览器测试；可添加 -IncludeUI。'
    }
} finally {
    Pop-Location
    & (Join-Path $PSScriptRoot 'clean-stage-cache.ps1') -Stage 0
}
