# 将本项目的后续依赖下载缓存放在项目磁盘，避免持续占用系统盘。
$cacheProjectRoot = Split-Path -Parent $PSScriptRoot
$env:UV_CACHE_DIR = Join-Path $cacheProjectRoot '.cache/uv'
$env:PIP_CACHE_DIR = Join-Path $cacheProjectRoot '.cache/pip'
$env:npm_config_cache = Join-Path $cacheProjectRoot '.cache/npm'
# 数据集语料缓存在项目磁盘；不改变已有的模型权重缓存位置。
$env:HF_DATASETS_CACHE = Join-Path $cacheProjectRoot '.cache/huggingface/datasets'
# 浏览器使用已安装的 Edge；模型缓存单独配置，不在依赖清理范围内。
