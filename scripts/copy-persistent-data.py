"""在停机迁移时复制持久目录，逐文件校验内容，不修改源数据。"""

import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path


def inventory(root: Path) -> dict[str, tuple[int, str]]:
    """检查目录边界并计算每个文件的大小和散列，拒绝特殊文件。"""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise RuntimeError("源数据包含符号链接，需单独处理后再迁移")
        if not path.resolve().is_relative_to(root.resolve()):
            raise RuntimeError("发现指向源目录之外的链接，停止迁移")
        if path.is_dir():
            continue
        if not path.is_file():
            raise RuntimeError("发现不支持的特殊文件，停止迁移")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        result[path.relative_to(root).as_posix()] = (path.stat().st_size, digest)
    return result


def copy(source: Path, target: Path) -> dict[str, object]:
    """仅向空目标复制文件和目录，复制后完整校验，禁止覆盖已有数据。"""
    source = source.resolve(strict=True)
    target = target.resolve(strict=True)
    if any(target.iterdir()):
        raise RuntimeError("目标必须为空，禁止覆盖已有数据")
    before = inventory(source)
    for path in sorted(source.rglob("*")):
        destination = target / path.relative_to(source)
        if path.is_dir():
            destination.mkdir(exist_ok=True)
        else:
            # Windows 挂载不保证支持 Linux 权限和属主，必须独立验证服务启动。
            shutil.copyfile(path, destination)
    # 尽可能保留 Linux 权限与属主；Windows 文件共享的最终权限仍由服务启动验证。
    metadata_warnings = 0
    for path in [*sorted(source.rglob("*"), reverse=True), source]:
        destination = target / path.relative_to(source)
        try:
            shutil.copystat(path, destination)
            if hasattr(os, "chown"):
                stat = path.stat()
                os.chown(destination, stat.st_uid, stat.st_gid)
        except OSError:
            metadata_warnings += 1
    after = inventory(target)
    if before != after:
        raise RuntimeError("源数据与目标数据校验不一致")
    return {
        "files": len(before),
        "bytes": sum(row[0] for row in before.values()),
        "manifest_sha256": hashlib.sha256(
            json.dumps(before, sort_keys=True).encode()
        ).hexdigest(),
        "verified": True,
        "metadata_warnings": metadata_warnings,
    }


def main() -> None:
    """执行受控目录复制并输出不包含文件正文的汇总证据。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("target", type=Path)
    args = parser.parse_args()
    print(json.dumps(copy(args.source, args.target)))


if __name__ == "__main__":
    main()
