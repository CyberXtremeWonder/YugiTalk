"""单任务内有界规划与检查；控制输出只保存为状态，不进入正式对话。"""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from app.agents.contracts import AgentConfig
from app.core.config import Settings
from app.guardrails.prompts import protect_messages
from app.providers.client import Provider, ProviderFailure, ToolCall

if TYPE_CHECKING:
    from app.agents.graph import AgentState, Update

ControlResult = TypeVar("ControlResult", bound=BaseModel)


class PlanStep(BaseModel):
    """公开行动步骤及可核验的完成标准。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=120)
    criterion: str = Field(min_length=1, max_length=240)


class Plan(BaseModel):
    """最多五步，不通过规划扩展工具权限。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    steps: list[PlanStep] = Field(min_length=1, max_length=5)


class Check(BaseModel):
    """每步检查引用实际成功的工具调用，纯文本任务允许无工具证据。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    step: int = Field(ge=1, le=5)
    status: Literal["completed", "incomplete"]
    requires_tool: bool
    evidence_call_ids: list[str] = Field(max_length=20)


class Reflection(BaseModel):
    """检查决策与缺口摘要，不索取或展示内部推理。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    decision: Literal["pass", "revise", "unable"]
    checks: list[Check] = Field(min_length=1, max_length=5)
    gaps: list[Annotated[str, StringConstraints(min_length=1, max_length=240)]] = Field(
        max_length=5
    )


def run_closed_loop(
    settings: Settings,
    config: AgentConfig,
    provider: Provider,
    messages: list[dict[str, Any]],
    update: "Update",
    on_text: Callable[[str], None],
    retries: int,
    resume: "AgentState | None",
    node: str,
    save: "Callable[[AgentState, str], None] | None",
    tool_executor: Callable[[ToolCall, int], dict[str, object]] | None,
) -> "AgentState":
    """复用原执行图及累计计数；每个阶段边界保存同一任务的检查点。"""
    from app.agents.graph import run_graph

    state: Any = resume or {
        "messages": protect_messages(messages),
        "text": "",
        "input_tokens": 0,
        "output_tokens": 0,
        "calls": [],
        "step": 0,
        "tool_count": 0,
        "repetitions": {},
        "tool_cursor": 0,
        "loop": {"plan": [], "revision": 0},
    }
    phase = node if resume else "plan"
    # phase 是下一步要执行的阶段；恢复任务沿用检查点，新任务从制定计划开始。
    loop = state["loop"]

    def persist(next_phase: str) -> None:
        if save:
            save(state, next_phase)

    def control(
        kind: str,
        contract: type[ControlResult],
        instruction: str,
    ) -> ControlResult:
        # 固定服务端指令附加在唯一系统消息中，计划与工具结果保持低优先级。
        context = protect_messages(state["messages"])
        prompt = (
            "\n"
            + instruction
            + "\n仅输出符合此 JSON Schema 的 JSON："
            + json.dumps(contract.model_json_schema(), ensure_ascii=False)
        )
        context.append(
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "plan": loop["plan"],
                        "reflection": loop.get("reflection"),
                        "allowed_tools": config.tools,
                        "agent_control": kind,
                    },
                    ensure_ascii=False,
                ),
            }
        )
        update("tool_status", {"stage": "agent_" + kind, "status": "running"}, 0)
        control_state: Any = {**state, "messages": context, "text": "", "calls": []}
        # 规划和检查也消耗模型步数与 Token，但禁用工具，且不把控制 JSON 发给用户。
        result = run_graph(
            settings,
            config.model_copy(update={"tools": []}),
            provider,
            context,
            update,
            lambda _: None,
            retries,
            control_state,
            "model",
            None,
            tool_executor,
            _control=kind,
            _control_prompt=prompt,
        )
        for key in ("step", "input_tokens", "output_tokens"):
            state[key] = result[key]
        try:
            return contract.model_validate_json(result["text"])
        except ValidationError as error:
            raise ProviderFailure("agent_control_invalid") from error

    while phase != "done":
        # 阶段前后均保存状态；中断恢复时能知道应该继续规划、执行还是检查。
        update(None, None, 0)
        persist(phase)
        if phase in {"plan", "revise"}:
            plan = control(
                phase,
                Plan,
                "制定最多五步的行动计划和完成标准。简单任务只需一步。"
                "仅使用授权工具；修订时针对检查缺口调整剩余步骤，复用已保存结果。"
                "不要输出内部推理。",
            )
            loop["plan"] = plan.model_dump()["steps"]
            update(
                "tool_status",
                {
                    "stage": "agent_plan",
                    "status": "ready",
                    "steps": loop["plan"],
                    "revision": loop["revision"],
                },
                0,
            )
            state["messages"].append(
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "context_kind": "agent_plan",
                            "steps": loop["plan"],
                            "instruction": "按计划完成任务，复用已有工具结果，生成候选答复。",
                        },
                        ensure_ascii=False,
                    ),
                }
            )
            state["text"] = ""
            phase = "model"
        elif phase in {"model", "tools"}:

            def save_execution(result: Any, next_node: str) -> None:
                # 普通图的 done 只代表候选答复生成完，闭环还要进入 reflect 验证。
                if save:
                    save(result, "reflect" if next_node == "done" else next_node)

            state = run_graph(
                settings,
                config,
                provider,
                state["messages"],
                update,
                lambda _: None,
                retries,
                state,
                phase,
                save_execution,
                tool_executor,
                _control="execute",
            )
            loop = state["loop"]
            phase = "reflect"
        elif phase == "reflect":
            review = control(
                "reflect",
                Reflection,
                "逐项核对计划完成标准、候选答复与实际工具结果。"
                "每个计划步骤必须有且仅有一项检查。需要实时数据或工具计算的步骤"
                "标记 requires_tool=true 并引用成功的 tool_call_id。"
                "未满足标准则 revise；缺少用户信息或无法获取证据则 unable。"
                "gaps 仅给出简短事实缺口，不输出内部推理。",
            )
            successful = set()
            # 不能只信模型宣称完成：检查引用的调用 ID 必须确实对应成功的工具结果。
            for message in state["messages"]:
                if message.get("role") == "tool":
                    result = json.loads(message["content"])
                    if result.get("ok") is not False and not result.get("error"):
                        successful.add(message["tool_call_id"])
            expected = list(range(1, len(loop["plan"]) + 1))
            # 检查必须逐一覆盖计划步骤，不能漏项、重复，也不能引用虚构的工具证据。
            if sorted(check.step for check in review.checks) != expected:
                raise ProviderFailure("agent_control_invalid")
            verified = all(
                check.status == "completed"
                and set(check.evidence_call_ids) <= successful
                and (not check.requires_tool or bool(check.evidence_call_ids))
                for check in review.checks
            )
            if not verified and review.decision == "pass":
                review.decision = "revise"
                review.gaps = ["步骤尚未完成或缺少有效的工具证据"]
            loop["reflection"] = review.model_dump()
            passed = review.decision == "pass" and verified and not review.gaps
            update(
                "tool_status",
                {
                    "stage": "agent_reflect",
                    "status": "passed" if passed else "incomplete",
                    "steps": [
                        {
                            **step,
                            "status": check.status
                            if (
                                set(check.evidence_call_ids) <= successful
                                and (not check.requires_tool or check.evidence_call_ids)
                            )
                            else "incomplete",
                        }
                        for step, check in zip(
                            loop["plan"],
                            sorted(review.checks, key=lambda item: item.step),
                            strict=True,
                        )
                    ],
                },
                0,
            )
            if passed:
                phase = "done"
            elif review.decision == "unable" or loop["revision"] >= 1:
                # 最多修订一次；仍未达标时返回明确的未完成结果，避免无限规划与执行。
                state["text"] = "任务未完成：" + "；".join(review.gaps or ["完成标准尚未满足"])
                loop["failure"] = "agent_incomplete"
                phase = "done"
            else:
                loop["revision"] += 1
                phase = "revise"
        else:
            raise ProviderFailure("agent_checkpoint_invalid")
        persist(phase)
    if loop.get("failure"):
        on_text(state["text"])
        raise ProviderFailure(loop["failure"])
    return cast("AgentState", state)
