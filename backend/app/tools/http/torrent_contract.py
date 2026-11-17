"""后台 BT 下载参数；下载目录及 RPC 凭证不由模型指定。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DownloadTorrent(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["start", "status"]
    source: str | None = Field(
        default=None, min_length=1, max_length=8000,
        description="start 时提供 magnet:?xt=urn:btih:… 或 HTTPS .torrent 文件直链",
    )
    download_id: str | None = Field(
        default=None, min_length=1, max_length=81,
        description="status 时提供本工具返回的完整下载 ID",
    )

    @model_validator(mode="after")
    def check_action(self) -> Self:
        if self.action == "start":
            valid = self.source is not None and self.download_id is None
        else:
            valid = self.download_id is not None and self.source is None
        if not valid:
            raise ValueError("start 仅接受 source；status 仅接受 download_id")
        return self
