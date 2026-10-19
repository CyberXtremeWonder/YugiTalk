"""在允许任何副作用之前，校验模型提出的工具调用。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportArguments(BaseModel):
    """严格校验工具参数，禁止通过额外字段扩大工具权限。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=2000)


class ToolProposal(BaseModel):
    """与模型供应商 SDK 无关的应用层工具调用契约。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: Literal["format_report"]
    arguments: ReportArguments


def validate_proposal(payload: object) -> ToolProposal:
    """拒绝不符合契约的模型输出；校验过程不会执行工具。"""
    return ToolProposal.model_validate(payload)
