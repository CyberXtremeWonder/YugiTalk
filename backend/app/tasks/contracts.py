"""任务创建输入契约。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class RunInput(BaseModel):
    """限定任务的对话、助手版本、消息、知识库及可选技能版本。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    conversation_id: UUID = Field(strict=False)
    agent_version_id: UUID = Field(strict=False)
    message: str = Field(min_length=1, max_length=6000)
    knowledge_base_id: UUID | None = Field(default=None, strict=False)
    skill_version_id: UUID | None = Field(default=None, strict=False)
