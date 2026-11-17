"""ZIP 工具输入与内存打包，不访问服务器文件系统。"""

import io
import re
import zipfile

from pydantic import BaseModel, ConfigDict, Field


class TextFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    filename: str = Field(min_length=1, max_length=160)
    content: str = Field(max_length=100_000)


class CreateZip(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="文件", min_length=1, max_length=100)
    files: list[TextFile] = Field(default_factory=list, max_length=20)
    image_urls: list[str] = Field(
        default_factory=list, max_length=6, description="需要一起打包的 HTTPS 图片直链"
    )


def archive(entries: list[tuple[str, bytes]]) -> bytes:
    """拒绝重复和危险路径；只写调用者已提供的有界内容。"""
    if not entries or sum(len(content) for _, content in entries) > 15 * 1024 * 1024:
        raise ValueError("压缩包不能为空，原始内容最多 15 MiB")
    names: set[str] = set()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, content in entries:
            parts = name.split("/")
            if (
                any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts)
                or re.search(r'[\\:*?"<>|\x00-\x1f\x7f]', name)
                or name.casefold() in names
            ):
                raise ValueError("压缩包文件名不合法或重复")
            names.add(name.casefold())
            zipped.writestr(name, content)
    return output.getvalue()
