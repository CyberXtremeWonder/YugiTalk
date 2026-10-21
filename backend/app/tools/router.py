"""工具控制面：空间管理员维护目录，普通成员只能查看符合角色的工具。"""

from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import func, or_, select

from app.accounts.access import AccountId, membership
from app.audit.service import audit
from app.identity.dependencies import DB, Actor, Writer
from app.identity.models import User
from app.tools.http_runtime import validate_arguments
from app.tools.management_contracts import ToolInput, ToolToggle
from app.tools.models import ManagedTool, ToolVersion
from app.tools.registry import TOOL_SCHEMAS
from app.tools.service import descriptor, digest, find_tool, latest, readiness, validate_config

router = APIRouter(prefix="/api/v1", tags=["Tool"])


def lock_workspace(db: DB, key: str) -> None:
    """与目录名称和版本配额共享空间父行锁，避免并发创建重复工具。"""
    db.scalar(select(User).where(User.id == key).with_for_update())
    db.expire_all()


@router.get("/me/tools")
def list_tools(account_id: AccountId, db: DB, actor: Actor, request: Request) -> dict[str, object]:
    """返回有界目录和就绪状态；不返回任何密钥，不因 GET 初始化数据库。"""
    key = str(account_id)
    member = membership(db, key, actor.user.id)
    names = [
        *TOOL_SCHEMAS,
        *db.scalars(
            select(ManagedTool.name)
            .where(
                ManagedTool.account_id == key,
                ManagedTool.deleted.is_(False),
                ManagedTool.builtin.is_(False),
                or_(ManagedTool.owner_id.is_(None), ManagedTool.owner_id == actor.user.id),
            )
            .order_by(ManagedTool.name)
            .limit(100)
        ),
    ]
    items = []
    # 是否启用、是否具备外部服务配置、是否允许维护分别返回，页面据此区分不可用原因。
    for name in names:
        item = descriptor(db, key, name)
        if item and (member.role in {"owner", "admin"} or member.role in item["required_roles"]):
            reason = (
                readiness(item["config"], request.app.state.settings)
                if item["kind"] == "http"
                else ""
            )
            items.append(
                {
                    **item,
                    "ready": not reason,
                    "configuration_note": reason,
                    "can_manage": item.get("owner_id") == actor.user.id
                    or (not item.get("owner_id") and member.role in {"owner", "admin"}),
                }
            )
    return {"items": items, "next_cursor": None}


def publish(db: DB, row: ManagedTool, data: ToolInput) -> None:
    """追加不可变版本，调用中的任务保持原端点，启停状态立即用于后续边界复核。"""
    number = (
        db.scalar(select(func.max(ToolVersion.number)).where(ToolVersion.tool_id == row.id)) or 0
    ) + 1
    if number > 100:
        raise HTTPException(409, detail={"code": "tool_version_limit"})
    config = data.model_dump(exclude={"enabled"})
    # 端点与参数进入不可变版本，enabled 留在工具身份上，因此停用可立即影响旧版本任务。
    row.enabled = data.enabled
    db.add(ToolVersion(tool_id=row.id, number=number, config=config, content_hash=digest(config)))
    db.flush()


def manage_existing_tool(db: DB, key: str, user: str, row: ManagedTool | None) -> None:
    """个人工具仅所有者可维护，空间工具仍由空间所有者或管理员维护。"""
    member = membership(db, key, user)
    if row and row.owner_id:
        if row.owner_id != user:
            raise HTTPException(404)
    elif member.role not in {"owner", "admin"}:
        raise HTTPException(403)


@router.post("/me/tools", status_code=201)
def create_tool(account_id: AccountId, data: ToolInput, db: DB, actor: Writer) -> dict[str, object]:
    """管理员创建声明式工具，拒绝覆盖内置名称并限制空间最多一百个自定义工具。"""
    key = str(account_id)
    if not actor.user.platform_admin:
        raise HTTPException(403, detail={"code": "tool_review_required"})
    membership(db, key, actor.user.id, manage=True)
    lock_workspace(db, key)
    if data.name in TOOL_SCHEMAS or find_tool(db, key, data.name):
        raise HTTPException(409, detail={"code": "tool_name_conflict"})
    count = (
        db.scalar(
            select(func.count())
            .select_from(ManagedTool)
            .where(
                ManagedTool.account_id == key,
                ManagedTool.builtin.is_(False),
                ManagedTool.deleted.is_(False),
            )
        )
        or 0
    )
    if count >= 100:
        raise HTTPException(409, detail={"code": "tool_catalog_limit"})
    row = ManagedTool(account_id=key, name=data.name)
    db.add(row)
    db.flush()
    publish(db, row, data)
    audit(db, actor.user, "tool.create", key, row.id)
    db.commit()
    return descriptor(db, key, row.name) or {}


@router.post("/me/tools/{name}/versions", status_code=201)
def publish_tool(
    account_id: AccountId, name: str, data: ToolInput, db: DB, actor: Writer
) -> dict[str, object]:
    """按不可变工具名称发布版本，不允许修改内置实现或伪造另一名称。"""
    key = str(account_id)
    if not actor.user.platform_admin:
        raise HTTPException(403, detail={"code": "tool_review_required"})
    membership(db, key, actor.user.id, manage=True)
    lock_workspace(db, key)
    row = find_tool(db, key, name)
    if row is None:
        raise HTTPException(404)
    if row.builtin or data.name != name:
        raise HTTPException(422, detail={"code": "tool_name_immutable"})
    if row.owner_id and row.owner_id != actor.user.id:
        raise HTTPException(403, detail={"code": "tool_not_authorized"})
    publish(db, row, data)
    audit(db, actor.user, "tool.publish", key, row.id)
    db.commit()
    return descriptor(db, key, name) or {}


@router.patch("/me/tools/{name}")
def toggle_tool(
    account_id: AccountId, name: str, data: ToolToggle, db: DB, actor: Writer
) -> dict[str, object]:
    """启停内置或自定义工具，旧任务的检查点与缓存结果不能绕过停用。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    lock_workspace(db, key)
    row = find_tool(db, key, name)
    manage_existing_tool(db, key, actor.user.id, row)
    if row is not None and row.owner_id and row.owner_id != actor.user.id:
        raise HTTPException(404)
    if row is None:
        if name not in TOOL_SCHEMAS:
            raise HTTPException(404)
        # 内置工具默认无需数据库记录，首次切换开关时才建立该用户目录下的覆盖状态。
        row = ManagedTool(account_id=key, name=name, builtin=True)
        db.add(row)
    row.enabled = data.enabled
    db.flush()
    audit(db, actor.user, "tool.toggle", key, row.id)
    db.commit()
    return descriptor(db, key, name) or {}


@router.delete("/me/tools/{name}", status_code=204)
def delete_tool(account_id: AccountId, name: str, db: DB, actor: Writer) -> Response:
    """软删除自定义工具并保留历史版本审计，内置工具只支持停用。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    lock_workspace(db, key)
    row = find_tool(db, key, name)
    manage_existing_tool(db, key, actor.user.id, row)
    if row is not None and row.owner_id and row.owner_id != actor.user.id:
        raise HTTPException(404)
    if name in TOOL_SCHEMAS:
        raise HTTPException(409, detail={"code": "builtin_tool_not_deletable"})
    if row is None:
        raise HTTPException(404)
    row.deleted, row.enabled = True, False
    audit(db, actor.user, "tool.delete", key, row.id)
    db.commit()
    return Response(status_code=204)


@router.post("/me/tools/{name}/validate")
def validate_tool_arguments(
    account_id: AccountId, name: str, arguments: dict[str, Any], db: DB, actor: Writer
) -> dict[str, object]:
    """仅在本地校验参数，不访问网络、不扣模型额度，不把测试按钮变成外部写入入口。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    row = find_tool(db, key, name)
    manage_existing_tool(db, key, actor.user.id, row)
    if row is None or row.builtin:
        raise HTTPException(404)
    if row.owner_id and row.owner_id != actor.user.id:
        raise HTTPException(404)
    try:
        validate_arguments(validate_config(latest(db, row).config), arguments)
    except ValueError:
        raise HTTPException(422, detail={"code": "tool_arguments_invalid"}) from None
    return {"valid": True, "network_called": False}
