"""工作空间名称及成员角色的输入契约。"""

from typing import Literal

from pydantic import Field, field_validator

from app.identity.contracts import InputModel


class WorkspaceInput(InputModel):
    """限定空间名称并拒绝纯空白输入。"""

    name: str = Field(min_length=1, max_length=80)

    @field_validator("name")
    @classmethod
    def name_not_blank(cls, value: str) -> str:
        """去除名称首尾空白，并拒绝空名称。"""
        if not value.strip():
            raise ValueError("名称不能为空")
        return value.strip()


class MemberInput(InputModel):
    """限定待加入成员的邮箱和可授予角色。"""

    email: str = Field(min_length=3, max_length=254)
    role: Literal["admin", "member"] = "member"


class RoleInput(InputModel):
    """限定成员角色更新的允许值。"""

    role: Literal["owner", "admin", "member"]
