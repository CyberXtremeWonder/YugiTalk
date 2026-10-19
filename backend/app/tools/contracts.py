"""只读工具的结构化输入及长度边界。"""

from pydantic import BaseModel, ConfigDict, Field


class TextStats(BaseModel):
    """校验文本统计工具的输入内容与长度。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(max_length=2000)


class Report(BaseModel):
    """校验报告格式化工具的标题、条目与输入上限。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=4000)
