"""通过最小 LangGraph 流程验证审批中断与持久化恢复。"""

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import interrupt


class ProbeState(TypedDict, total=False):
    """只保存验证数据，不包含密钥或正式业务状态。"""

    prompt: str
    decision: str
    status: str


def request_approval(state: ProbeState) -> ProbeState:
    """暂停模拟报告预览，仅用于框架兼容性验证。"""
    decision = interrupt({"tool": "format_report", "summary": state["prompt"]})
    return {"decision": str(decision)}


def resolve_approval(state: ProbeState) -> ProbeState:
    """兼容性验证只记录审批结果，不执行外部写操作。"""
    return {"status": "approved" if state["decision"] == "approve" else "rejected"}


def build_graph(checkpointer: Any) -> CompiledStateGraph[Any, Any, Any, Any]:
    """注入检查点存储器，让不同 Worker 进程能够恢复同一任务状态。"""
    graph = StateGraph(ProbeState)
    graph.add_node("approval", request_approval)
    graph.add_node("resolve", resolve_approval)
    graph.add_edge(START, "approval")
    graph.add_edge("approval", "resolve")
    graph.add_edge("resolve", END)
    return graph.compile(checkpointer=checkpointer)
