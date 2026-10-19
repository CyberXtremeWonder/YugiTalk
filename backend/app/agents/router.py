"""Agent 能力查询与不可变版本发布。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.accounts.access import AccountId, membership
from app.agents.contracts import AgentConfig, effective_config
from app.agents.models import Agent, AgentVersion
from app.audit.service import audit
from app.core.config import Settings
from app.core.pagination import Limit, page
from app.identity.dependencies import DB, Actor, Writer
from app.tools.registry import TOOL_SCHEMAS
from app.tools.service import validate_names

router = APIRouter(prefix="/api/v1", tags=["Agent"])


@router.get("/model/capabilities")
def capabilities(request: Request, actor: Actor) -> dict[str, object]:
    """返回当前模型和已注册工具的公开能力说明。"""
    settings: Settings = request.app.state.settings
    return {
        "provider": settings.model_provider,
        "model": settings.model_id,
        "configured": settings.model_provider == "mock"
        or bool(settings.model_api_key.get_secret_value() and settings.model_base_url),
        "streaming": True,
        "tools": list(TOOL_SCHEMAS),
        "note": "Mock 离线演示"
        if settings.model_provider == "mock"
        else "真实模型须通过联调验证工具与流式能力",
    }


@router.get("/me/agents")
def list_agents(
    account_id: AccountId, db: DB, actor: Actor, limit: Limit = 20, cursor: UUID | None = None
) -> dict[str, object]:
    """校验空间成员身份，分页返回助手及其最新已发布版本。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    agents = db.scalars(
        select(Agent)
        .where(
            Agent.account_id == key,
            Agent.deleted.is_(False),
            Agent.id > (str(cursor) if cursor else ""),
        )
        .order_by(Agent.id)
        .limit(limit + 1)
    )
    items = []
    for agent in agents:
        version = db.scalar(
            select(AgentVersion)
            .where(AgentVersion.agent_id == agent.id)
            .order_by(AgentVersion.number.desc())
            .limit(1)
        )
        if version:
            items.append(
                {
                    "id": agent.id,
                    "name": agent.name,
                    "version_id": version.id,
                    "number": version.number,
                    "config": effective_config(version.config),
                }
            )
    return page(items, limit)


def publish(
    request: Request, db: Session, agent: Agent, data: AgentConfig, user: str | None = None
) -> AgentVersion:
    """分配递增版本号并固定当前模型配置，保持历史版本不可变。"""
    settings: Settings = request.app.state.settings
    validate_names(db, agent.account_id, data.tools, user)
    number = (
        db.scalar(select(func.max(AgentVersion.number)).where(AgentVersion.agent_id == agent.id))
        or 0
    ) + 1
    config = data.model_dump()
    config.update({"provider": settings.model_provider, "model": settings.model_id})
    version = AgentVersion(agent_id=agent.id, number=number, config=config)
    agent.name = data.name
    db.add(version)
    db.flush()
    return version


@router.post("/me/agents", status_code=201)
def create_agent(
    account_id: AccountId, data: AgentConfig, request: Request, db: DB, actor: Writer
) -> dict[str, object]:
    """校验空间管理权限，创建助手并发布初始版本。"""
    key = str(account_id)
    membership(db, key, actor.user.id, manage=True)
    agent = Agent(account_id=key, name=data.name)
    db.add(agent)
    db.flush()
    version = publish(request, db, agent, data, actor.user.id)
    audit(db, actor.user, "agent.create", key, agent.id)
    db.commit()
    return {"id": agent.id, "version_id": version.id, "number": version.number}


@router.post("/agents/{agent_id}/versions", status_code=201)
def publish_version(
    agent_id: UUID, data: AgentConfig, request: Request, db: DB, actor: Writer
) -> dict[str, object]:
    """锁定助手后发布新版本，避免并发生成重复序号。"""
    agent = db.scalar(select(Agent).where(Agent.id == str(agent_id)).with_for_update())
    if agent is None or agent.deleted:
        raise HTTPException(404)
    membership(db, agent.account_id, actor.user.id, manage=True)
    version = publish(request, db, agent, data, actor.user.id)
    audit(db, actor.user, "agent.publish", agent.account_id, version.id)
    db.commit()
    return {"id": version.id, "number": version.number, "config": version.config}


@router.delete("/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: UUID, db: DB, actor: Writer) -> Response:
    """删除本人助手的目录入口并禁止新任务，保留版本与已有任务的冻结配置。"""
    agent = db.scalar(
        select(Agent).where(
            Agent.id == str(agent_id), Agent.account_id == actor.user.id,
            Agent.deleted.is_(False),
        ).with_for_update()
    )
    if agent is None:
        raise HTTPException(404)
    membership(db, agent.account_id, actor.user.id, manage=True)
    agent.deleted = True
    audit(db, actor.user, "agent.delete", agent.account_id, agent.id)
    db.commit()
    return Response(status_code=204)
