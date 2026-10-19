"""规划、评判、重写和澄清契约，拒绝未知字段和越界模型输出。"""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.config import Settings


class QueryPlan(BaseModel):
    """限定简单问题单查、复杂问题二到四个不重复子问题。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    complexity: Literal["simple", "complex"]
    sub_questions: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def bounded_questions(self) -> Self:
        """同时校验分支数量、空白和长度，防止无界并行或重复调用。"""
        if self.complexity == "simple" and self.sub_questions:
            raise ValueError("rag_plan_invalid")
        if self.complexity == "complex" and (
            not 2 <= len(self.sub_questions) <= 4
            or len(set(self.sub_questions)) != len(self.sub_questions)
            or any(not question.strip() or len(question) > 1000 for question in self.sub_questions)
        ):
            raise ValueError("rag_plan_invalid")
        return self


class EvidenceGrade(BaseModel):
    """结构化判断证据质量与歧义，不允许不充分证据被直接标为可回答。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    relevance: Literal["none", "weak", "strong"]
    answerability: Literal["none", "partial", "sufficient"]
    ambiguity: Literal["none", "missing_slot", "multiple_candidates"]
    route: Literal["answer", "rewrite", "clarify", "no_knowledge"]
    hitl_prompt: str = Field(default="", max_length=240)
    hitl_options: list[str] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def safe_route(self) -> Self:
        """约束答案路由和澄清选项，模型输出不能绕过证据与交互边界。"""
        if self.route == "answer" and (
            self.relevance != "strong"
            or self.answerability != "sufficient"
            or self.ambiguity != "none"
        ):
            raise ValueError("rag_grade_invalid")
        if any(not option.strip() or len(option) > 120 for option in self.hitl_options):
            raise ValueError("rag_grade_invalid")
        return self


class Rewrite(BaseModel):
    """一次调用只能选择一种改写方式；HyDE 内容只能作为检索查询。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    method: Literal["step_back", "hyde"]
    query: str = Field(min_length=1, max_length=1600)


class ClarificationInput(BaseModel):
    """使用状态版本与幂等键恢复同一任务，限制补充文本长度。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    answer: str = Field(min_length=1, max_length=1000)
    state_version: int = Field(ge=0)


def freeze_rag_config(settings: Settings) -> dict[str, object]:
    """冻结非敏感 RAG 配置，任务恢复不受后续角色和算法参数变更影响。"""
    return {
        key: getattr(settings, key)
        for key in type(settings).model_fields
        if key.startswith("rag_")
        and key not in {"rag_fast_api_key", "rag_grader_api_key", "rag_rerank_api_key"}
    }
