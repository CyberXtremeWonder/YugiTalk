"""技能管理、内容冻结和运行时权限收窄，避免任务临时加载可变说明。"""

import hashlib
import json
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.accounts.access import membership
from app.guardrails.prompts import data_message
from app.providers.client import ProviderFailure
from app.skills.models import Skill, SkillVersion


def content_hash(name: str, description: str, instructions: str, tools: list[str]) -> str:
    """对规范化元数据和完整正文计算校验值，不使用 Python 的随机哈希。"""
    manifest = {"name": name, "description": description, "allowed_tools": sorted(tools)}
    raw = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode() + b"\0"
    return hashlib.sha256(raw + instructions.encode()).hexdigest()


def owned_skill(db: Session, key: str, user: str, manage: bool = False) -> Skill:
    """所有成员可使用空间技能，编辑仅限创建者及当前空间管理员。"""
    skill = db.get(Skill, key)
    if skill is None or skill.deleted:
        raise HTTPException(404)
    member = membership(db, skill.account_id, user)
    if manage and skill.owner_id != user and member.role not in {"owner", "admin"}:
        raise HTTPException(403)
    return skill


def latest_version(db: Session, skill: Skill) -> SkillVersion:
    """读取最新发布版本，数据库未完成有效发布时拒绝返回空技能。"""
    version = db.scalar(
        select(SkillVersion)
        .where(SkillVersion.skill_id == skill.id)
        .order_by(SkillVersion.number.desc())
        .limit(1)
    )
    if version is None:
        raise HTTPException(409, detail={"code": "skill_unavailable"})
    return version


def freeze_skill(db: Session, version_id: str, workspace: str, user: str) -> dict[str, Any]:
    """创建任务时冻结一个版本的完整内容，拒绝跨空间、已删除和已停用技能。"""
    version = db.get(SkillVersion, version_id)
    skill = owned_skill(db, version.skill_id, user) if version else None
    if skill is None or skill.account_id != workspace:
        raise HTTPException(404)
    if not skill.enabled:
        raise HTTPException(409, detail={"code": "skill_unavailable"})
    assert version is not None
    snapshot = {
        # 保存完整正文和版本指纹，而非执行时重新取最新版本，确保任务可复现。
        "id": skill.id,
        "version_id": version.id,
        "number": version.number,
        "name": version.name,
        "description": version.description,
        "instructions": version.instructions,
        "allowed_tools": list(version.allowed_tools),
        "content_hash": version.content_hash,
    }
    check_resume_skill(db, {"skill": snapshot}, workspace)
    return snapshot


def validate_pin(db: Session, pin: dict[str, Any], workspace: str) -> None:
    """每个任务边界复核启停、归属和原版本哈希；内容漂移或撤销后立即拒绝。"""
    skill = db.get(Skill, pin.get("id"))
    version = db.get(SkillVersion, pin.get("version_id"))
    if not skill or skill.account_id != workspace or skill.deleted or not skill.enabled:
        raise ProviderFailure("skill_unavailable")
    if (
        not version
        # 同时比较快照字段与重算哈希，既发现任务快照漂移，也发现原版本被改写。
        or version.skill_id != skill.id
        or version.name != pin.get("name")
        or version.number != pin.get("number")
        or version.description != pin.get("description")
        or version.instructions != pin.get("instructions")
        or version.allowed_tools != pin.get("allowed_tools")
        or version.content_hash != pin.get("content_hash")
        or version.content_hash
        != content_hash(
            version.name, version.description, version.instructions, version.allowed_tools
        )
    ):
        raise ProviderFailure("skill_content_changed")


def check_resume_skill(db: Session, snapshot: dict[str, Any], workspace: str) -> None:
    """在 HTTP 创建与恢复边界返回稳定的业务错误，避免先扣额再发现技能撤销。"""
    if not snapshot.get("skill"):
        return
    try:
        validate_pin(db, snapshot["skill"], workspace)
    except ProviderFailure as error:
        raise HTTPException(409, detail={"code": error.code}) from None


def prompt_block(pin: dict[str, Any]) -> dict[str, object]:
    """完整加载所选流程但不授予系统角色；JSON 编码只保护结构，不保证模型理解安全。"""
    return data_message(
        "selected_skill",
        {"name": pin["name"], "version": pin["number"], "instructions": pin["instructions"]},
    )
