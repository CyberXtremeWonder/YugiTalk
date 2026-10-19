"""移除空间后用户直接归属、多知识库多文件及跨账号隔离回归。"""

from fastapi.testclient import TestClient
from test_identity import ORIGIN, login

pytest_plugins = ["test_identity", "test_knowledge"]


def test_multiple_bases_and_files_are_private(client: TestClient, storage) -> None:
    """每个账号可创建多个库，每库多个文件，其他账号无法读取或修改。"""
    owner = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": owner["csrf_token"]}
    bases = []
    for name in ("产品资料", "学习资料"):
        response = client.post("/api/v1/me/knowledge-bases", json={"name": name}, headers=headers)
        assert response.status_code == 201
        key = response.json()["id"]
        bases.append(key)
        for number in range(2):
            response = client.post(
                f"/api/v1/knowledge-bases/{key}/documents?filename={number}.txt",
                content=f"{name}文件{number}".encode(),
                headers=headers,
            )
            assert response.status_code == 202
        assert len(client.get(f"/api/v1/knowledge-bases/{key}/documents").json()["items"]) == 2
    assert len(client.get("/api/v1/me/knowledge-bases").json()["items"]) == 2
    assert "workspaces" not in client.get("/api/v1/auth/me").json()
    assert client.get("/api/v1/workspaces").status_code == 404
    other = login(client, "member@example.test")
    headers = {**ORIGIN, "X-CSRF-Token": other["csrf_token"]}
    assert client.get("/api/v1/me/knowledge-bases").json()["items"] == []
    for key in bases:
        assert client.get(f"/api/v1/knowledge-bases/{key}/documents").status_code == 404
        assert client.post(
            f"/api/v1/knowledge-bases/{key}/documents?filename=attack.txt",
            content=b"untrusted", headers=headers,
        ).status_code == 404
