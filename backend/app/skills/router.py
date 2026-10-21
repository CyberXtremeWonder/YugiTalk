"""自定义 Skill 的空间目录、发布、启停和软删除接口。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import func, select

from app.accounts.access import AccountId, membership
from app.audit.service import audit
from app.core.pagination import Limit, page
from app.identity.dependencies import DB, Actor, Writer
from app.identity.models import User
from app.skills.contracts import SkillInput, SkillToggle
from app.skills.models import Skill, SkillVersion
from app.skills.service import content_hash, latest_version, owned_skill
from app.tools.service import validate_names

router = APIRouter(prefix="/api/v1", tags=["Skill"])


def lock_workspace(db: DB, key: str) -> None:
    """沿用空间父行加锁顺序，串行化目录配额、名称和版本发布。"""
    db.scalar(select(User).where(User.id == key).with_for_update())
    db.expire_all()


def skill_data(db: DB, skill: Skill, user: str, full: bool = False) -> dict[str, object]:
    """目录只返回摘要，编辑详情才返回完整正文，避免无选择的技能进入模型上下文。"""
    version = latest_version(db, skill)
    member = membership(db, skill.account_id, user)
    result: dict[str, object] = {
        "id": skill.id,
        "name": skill.name,
        "description": version.description,
        "version_id": version.id,
        "number": version.number,
        "allowed_tools": version.allowed_tools,
        "content_hash": version.content_hash,
        "enabled": skill.enabled,
        "can_manage": skill.owner_id == user or member.role in {"owner", "admin"},
    }
    if full:
        result["instructions"] = version.instructions
    return result


def publish(db: DB, skill: Skill, data: SkillInput, user: str | None = None) -> None:
    """发布不可变版本，当前名称去重并限制单技能最多一百个版本。"""
    validate_names(db, skill.account_id, data.allowed_tools, user)
    if db.scalar(
        select(Skill.id).where(
            Skill.account_id == skill.account_id,
            Skill.name == data.name,
            Skill.deleted.is_(False),
            Skill.id != skill.id,
        )
    ):
        raise HTTPException(409, detail={"code": "skill_name_conflict"})
    number = (
        db.scalar(select(func.max(SkillVersion.number)).where(SkillVersion.skill_id == skill.id))
        or 0
    ) + 1
    if number > 100:
        raise HTTPException(409, detail={"code": "skill_version_limit"})
    skill.name, skill.enabled = data.name, data.enabled
    # 更新目录上的名称/开关，同时追加版本记录；旧正文和工具许可不被覆盖。
    db.add(
        SkillVersion(
            skill_id=skill.id,
            number=number,
            name=data.name,
            description=data.description,
            instructions=data.instructions,
            allowed_tools=list(data.allowed_tools),
            content_hash=content_hash(
                data.name, data.description, data.instructions, list(data.allowed_tools)
            ),
        )
    )
    db.flush()


@router.get("/me/skills")
def list_skills(
    account_id: AccountId, db: DB, actor: Actor, limit: Limit = 20, cursor: UUID | None = None
) -> dict[str, object]:
    """所有当前空间成员可分页查看摘要，删除技能不再出现在目录。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    rows = db.scalars(
        select(Skill)
        .where(
            Skill.account_id == key,
            Skill.deleted.is_(False),
            Skill.id > (str(cursor) if cursor else ""),
        )
        .order_by(Skill.id)
        .limit(limit + 1)
    )
    return page([skill_data(db, row, actor.user.id) for row in rows], limit)


@router.get("/skills/{skill_id}")
def read_skill(skill_id: UUID, db: DB, actor: Actor) -> dict[str, object]:
    """经成员校验后读取正文，不将技能作为未经授权的公开文件提供。"""
    return skill_data(db, owned_skill(db, str(skill_id), actor.user.id), actor.user.id, True)


@router.post("/me/skills", status_code=201)
def create_skill(
    account_id: AccountId, data: SkillInput, db: DB, actor: Writer
) -> dict[str, object]:
    """允许普通成员创建空间共享技能，名称、目录配额和审计与发布共同提交。"""
    key = str(account_id)
    membership(db, key, actor.user.id)
    lock_workspace(db, key)
    # 同一用户目录上的配额检查、重名检查和插入串行执行，避免并发越过限制。
    validate_names(db, key, data.allowed_tools, actor.user.id)
    count = db.scalar(
        select(func.count())
        .select_from(Skill)
        .where(Skill.account_id == key, Skill.deleted.is_(False))
    )
    if count is not None and count >= 100:
        raise HTTPException(409, detail={"code": "skill_catalog_limit"})
    if db.scalar(
        select(Skill.id).where(
            Skill.account_id == key, Skill.name == data.name, Skill.deleted.is_(False)
        )
    ):
        raise HTTPException(409, detail={"code": "skill_name_conflict"})
    skill = Skill(account_id=key, owner_id=actor.user.id, name=data.name)
    db.add(skill)
    db.flush()
    publish(db, skill, data, actor.user.id)
    audit(db, actor.user, "skill.create", key, skill.id)
    result = skill_data(db, skill, actor.user.id, True)
    db.commit()
    return result


@router.post("/skills/{skill_id}/versions", status_code=201)
def publish_skill(skill_id: UUID, data: SkillInput, db: DB, actor: Writer) -> dict[str, object]:
    """创建者或管理员发布新说明，已有任务保留原版本与校验值。"""
    skill = owned_skill(db, str(skill_id), actor.user.id, manage=True)
    lock_workspace(db, skill.account_id)
    skill = owned_skill(db, str(skill_id), actor.user.id, manage=True)
    publish(db, skill, data, actor.user.id)
    audit(db, actor.user, "skill.publish", skill.account_id, skill.id)
    result = skill_data(db, skill, actor.user.id, True)
    db.commit()
    return result


@router.patch("/skills/{skill_id}")
def toggle_skill(skill_id: UUID, data: SkillToggle, db: DB, actor: Writer) -> dict[str, object]:
    """实时启停整个技能身份，停止后所有旧版本均不能启动或恢复执行。"""
    skill = owned_skill(db, str(skill_id), actor.user.id, manage=True)
    lock_workspace(db, skill.account_id)
    skill = owned_skill(db, str(skill_id), actor.user.id, manage=True)
    skill.enabled = data.enabled
    audit(db, actor.user, "skill.toggle", skill.account_id, skill.id)
    result = skill_data(db, skill, actor.user.id)
    db.commit()
    return result


@router.delete("/skills/{skill_id}", status_code=204)
def delete_skill(skill_id: UUID, db: DB, actor: Writer) -> Response:
    """软删除后移出目录并撤销任务使用资格，保留历史版本以供审计。"""
    skill = owned_skill(db, str(skill_id), actor.user.id, manage=True)
    lock_workspace(db, skill.account_id)
    skill = owned_skill(db, str(skill_id), actor.user.id, manage=True)
    skill.deleted, skill.enabled = True, False
    audit(db, actor.user, "skill.delete", skill.account_id, skill.id)
    db.commit()
    return Response(status_code=204)
