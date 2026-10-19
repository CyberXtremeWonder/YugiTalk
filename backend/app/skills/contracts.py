"""Skill 输入契约，只允许流程说明与项目已注册的只读工具。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SkillInput(BaseModel):
    """限定技能名称、描述、正文和工具上限，不接受脚本入口或凭证。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    description: str = Field(min_length=1, max_length=500)
    instructions: str = Field(min_length=1, max_length=6000)
    allowed_tools: list[str] = Field(default_factory=list, max_length=12)
    enabled: bool = True

    @field_validator("description", "instructions")
    @classmethod
    def nonblank(cls, value: str) -> str:
        """规范首尾空白，禁止保存空描述或空流程。"""
        if not value.strip():
            raise ValueError("内容不能为空")
        return value.strip()

    @field_validator("allowed_tools")
    @classmethod
    def unique_tools(cls, value: list[str]) -> list[str]:
        """工具上限不接受重复项，空集合表示不允许调用业务工具。"""
        if len(value) != len(set(value)):
            raise ValueError("工具不能重复")
        return value


class SkillToggle(BaseModel):
    """独立控制技能启停，不修改已发布的说明版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
