"""声明式只读 HTTPS 工具契约；只接受有界标量参数，不接受脚本和真实密钥。"""

import ipaddress
import re
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def public_endpoint(value: str) -> str:
    """静态拒绝私网地址、特殊域名、非 HTTPS、URL 凭证及可变查询参数。"""
    url = urlsplit(value)
    host = (url.hostname or "").lower()
    if (
        url.scheme != "https"
        or not host
        or url.port not in {None, 443}
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or "\\" in value
        or any(c.isspace() for c in value)
        or host.endswith(".")
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".internal", ".test", ".invalid", ".example"))
    ):
        raise ValueError("工具地址必须是无凭证和查询串的公共 HTTPS 地址，端口为 443")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not re.fullmatch(
            r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", host
        ):
            raise ValueError("工具必须使用公共完整域名") from None
    else:
        if not address.is_global:
            raise ValueError("工具不允许访问本机、内网或元数据地址")
    return value


class ToolParameter(BaseModel):
    """声明一个受限查询参数，避免任意嵌套 Schema 或远端引用。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,39}$")
    type: Literal["string", "integer", "number", "boolean"] = "string"
    description: str = Field(default="", max_length=200)
    required: bool = True


class ToolInput(BaseModel):
    """限定工具元数据、查询端点、参数和运行策略；密钥只填写服务端引用名称。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    title: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    endpoint: str = Field(min_length=1, max_length=2048)
    method: Literal["GET", "POST"] = "GET"
    parameters: list[ToolParameter] = Field(default_factory=list, max_length=12)
    required_roles: list[Literal["owner", "admin", "member"]] = Field(
        default=["owner", "admin", "member"], min_length=1, max_length=3
    )
    secret_name: str = Field(default="", pattern=r"^[a-zA-Z0-9_]{0,64}$")
    timeout_seconds: int = Field(default=10, ge=1, le=20)
    max_response_bytes: int = Field(default=8000, ge=256, le=16000)
    enabled: bool = True

    @field_validator("endpoint")
    @classmethod
    def endpoint_is_public(cls, value: str) -> str:
        """发布前验证端点格式；真实 DNS 地址仍在每次调用时检查并固定。"""
        return public_endpoint(value)

    @field_validator("title", "description")
    @classmethod
    def nonblank(cls, value: str) -> str:
        """去除空白元数据，避免无法辨认用途的工具。"""
        if not value.strip():
            raise ValueError("工具标题和说明不能为空")
        return value.strip()

    @model_validator(mode="after")
    def unique_parameters(self) -> Self:
        """拒绝重复参数和重复角色，确保版本哈希及输入 Schema 唯一。"""
        if self.method == "POST" and (
            self.name != "web_search" or self.endpoint != "https://api.tavily.com/search"
        ):
            raise ValueError("POST 仅允许固定 Tavily 只读搜索接口")
        if len({item.name for item in self.parameters}) != len(self.parameters):
            raise ValueError("参数名称不能重复")
        if len(set(self.required_roles)) != len(self.required_roles):
            raise ValueError("角色不能重复")
        return self


class ToolToggle(BaseModel):
    """仅允许修改启停状态，版本内容必须通过发布接口变更。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
