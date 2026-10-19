"""私有对话标题输入契约。"""

from pydantic import BaseModel, ConfigDict, Field


class Title(BaseModel):
    """限定对话标题的输入格式和长度。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(default="新对话", min_length=1, max_length=80)
