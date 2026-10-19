"""验证 FastAPI 与 Pydantic 工具契约兼容，不提供正式业务接口。"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
from probes.contracts import ToolProposal


def make_contract_client() -> TestClient:
    """只在测试进程中建立契约入口，不对外监听端口。"""
    app = FastAPI()

    @app.post("/probe/proposals")
    def accept_proposal(proposal: ToolProposal) -> ToolProposal:
        """接受通过输入契约校验的工具提案，供接口测试使用。"""
        return proposal

    return TestClient(app)


def test_valid_proposal_round_trip() -> None:
    """验证合法工具提案通过接口校验并保持返回内容一致。"""
    payload = {
        "name": "format_report",
        "arguments": {"title": "兼容性验证", "body": "仅校验，不执行"},
    }
    with make_contract_client() as client:
        response = client.post("/probe/proposals", json=payload)
        assert response.status_code == 200
        assert response.json() == payload


def test_invalid_tool_proposal_returns_422() -> None:
    """验证不符合契约的工具提案返回参数校验错误。"""
    with make_contract_client() as client:
        response = client.post(
            "/probe/proposals",
            json={"name": "delete_database", "arguments": {"title": "x", "body": "x"}},
        )
        assert response.status_code == 422
        schema = client.get("/openapi.json").json()
        arguments = schema["components"]["schemas"]["ReportArguments"]
        assert arguments["additionalProperties"] is False
