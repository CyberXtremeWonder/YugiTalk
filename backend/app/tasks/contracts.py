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
    research_project_id: UUID | None = Field(default=None, strict=False)
    uploaded_files: list["ChatFile"] = Field(default_factory=list, max_length=3)


class ChatFile(BaseModel):
    """附件文本是用户级资料，不作为系统指令或本机文件地址。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(ge=0, le=20 * 1024 * 1024)
    text: str = Field(min_length=1, max_length=60000)
    truncated: bool = False


class SteeringInput(BaseModel):
    """运行中追加用户指令，不允许修改任务权限或预算。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=6000)
