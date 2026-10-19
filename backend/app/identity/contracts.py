"""登录输入契约及邮箱格式校验。"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator


class InputModel(BaseModel):
    """拒绝未声明的输入字段，作为接口契约的基础模型。"""

    model_config = ConfigDict(extra="forbid")


class EmailInput(InputModel):
    """统一规范邮箱，用于注册、验证和账号初始化。"""

    email: str = Field(min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def email_format(cls, value: str) -> str:
        """去除首尾空白并规范邮箱大小写，拒绝非法格式。"""
        result = value.strip().lower()
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", result):
            raise ValueError("邮箱格式无效")
        return result


class LoginInput(InputModel):
    """支持邮箱与管理员用户名登录，保留既有接口字段和密码长度行为。"""

    email: str = Field(min_length=3, max_length=254)
    password: str
    development_mode: bool = False

    @field_validator("email")
    @classmethod
    def login_name(cls, value: str) -> str:
        """邮箱统一规范，用户名仅允许有界字母数字和下划线。"""
        if "@" in value:
            return EmailInput(email=value).email
        result = value.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_]{2,79}", result):
            raise ValueError("账号格式无效")
        return result


class RegisterInput(EmailInput):
    """注册只允许普通用户字段，验证码必填，禁止客户端赋予管理权限。"""

    name: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1)
    code: str = Field(pattern=r"^[0-9]{6}$")

    @field_validator("name")
    @classmethod
    def name_format(cls, value: str) -> str:
        """规范显示名称并拒绝空白内容。"""
        result = value.strip()
        if not result:
            raise ValueError("名称不能为空")
        return result


class AccountStateInput(InputModel):
    """管理员只能启停账号，不能通过该接口修改密码或提升角色。"""

    active: bool
