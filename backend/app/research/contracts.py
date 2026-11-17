"""研究输入和结构化模型输出；所有引用由服务端验证。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=6000)
    web_enabled: bool = True
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=5)
    max_rounds: int = Field(default=2, ge=1, le=3)
    timeout_seconds: int = Field(default=600, ge=60, le=1800)

    @field_validator("title", "question")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("研究目标不能为空")
        return value.strip()


class Question(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=500)
    criterion: str = Field(min_length=1, max_length=400)


class Plan(BaseModel):
    questions: list[Question] = Field(min_length=1, max_length=4)


class Assessment(BaseModel):
    status: str = Field(pattern="^(sufficient|partial|none|conflict)$")
    gaps: list[str] = Field(max_length=6)
    next_query: str = Field(default="", max_length=500)


class Citation(BaseModel):
    evidence_id: str
    quote: str = Field(min_length=1, max_length=1000)


class Finding(BaseModel):
    text: str = Field(min_length=1, max_length=3000)
    kind: str = Field(pattern="^(fact|inference|conflict)$")
    evidence: list[Citation] = Field(min_length=1, max_length=8)


class Report(BaseModel):
    findings: list[Finding] = Field(max_length=30)
    limitations: list[str] = Field(min_length=1, max_length=12)
