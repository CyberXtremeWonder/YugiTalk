"""工作空间设置只接受当前用户的目录与启用状态。"""

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class WorkspaceInput(BaseModel):
    """未启用的绝对路径可作为草稿保存，实际读取仍由服务端检查。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    directory: str = Field(default="", max_length=2048)
    enabled: bool = False

    @field_validator("directory")
    @classmethod
    def safe_directory(cls, value: str) -> str:
        """校验配置文本；路由按已存快照名称或服务器绝对路径区分语义。"""
        if any(ord(character) < 32 for character in value):
            raise ValueError("目录不能包含控制字符")
        value = value.strip()
        return value

    @model_validator(mode="after")
    def configured_when_enabled(self) -> Self:
        """空目录只能保持关闭，避免把空值解释为服务端当前目录。"""
        if self.enabled and not self.directory:
            raise ValueError("启用前请先选择目录")
        return self
