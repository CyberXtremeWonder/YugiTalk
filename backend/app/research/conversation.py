"""让助手根据对话要求研究并交付文件；研究状态绑定原任务和工具调用。"""

import base64
import hashlib
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.chat.models import Message
from app.core.config import Settings
from app.memory.models import ConversationSummary
from app.observability.trace_models import TraceStep
from app.providers.client import Provider, ProviderFailure
from app.rag.models import KnowledgeBase
from app.research.files import MEDIA_TYPES, FileFormat, export_report, filename
from app.research.models import ResearchProject, ResearchReport, ResearchSource
from app.research.router import validate_sources
from app.research.runner import ResearchRunner
from app.tasks.models import Run

if TYPE_CHECKING:
    from app.tasks.execution import Execution


class ResearchFileRequest(BaseModel):
    """模型从用户要求中提取目标、来源与文件格式，不接受路径或代码。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=160)
    question: str = Field(min_length=1, max_length=6000)
    formats: list[FileFormat] = Field(default=["md"], min_length=1, max_length=3)
    web_enabled: bool = True
    knowledge_base_ids: list[str] = Field(default=[], max_length=5)
    user_material: str = Field(default="", max_length=20000)


FILE_TOOL = {
    "name": "research_report",
    "description": (
        "仅在用户明确要求补充研究、搜索或检索新资料时开展研究并生成文件。"
        "单纯生成 PDF/Word 或导出已有对话必须调用 export_document，禁止使用本工具额外检索。"
        "用户要求交付文件时调用；仅咨询无需生成文件。格式遵循用户要求，未指定默认 md。"
        "内部资料从授权目录选择 knowledge_base_ids；只整理用户提供内容时放入 user_material"
        "并关闭 web_enabled，不添加外部事实。"
        "user_material 必须原样摘自已保存的用户消息，不能改写或编造。"
        "公开研究需已授权且配置的 web_search。"
        "返回的附件由聊天界面展示，不编造文件路径或下载链接。"
    ),
    "parameters": ResearchFileRequest.model_json_schema(),
}


class ExportRequest(BaseModel):
    """仅排版和导出；已有内容从当前对话读取，不接受检索参数。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1, max_length=160)
    formats: list[FileFormat] = Field(default=["md"], min_length=1, max_length=3)
    source: Literal["latest_answer", "user_message", "content"] = "latest_answer"
    content: str = Field(default="", max_length=50000)


EXPORT_TOOL = {
    "name": "export_document",
    "description": (
        "直接生成 PDF、Word 或 Markdown 文件，不联网、不检索、不研究。"
        "已有答复导出时 source=latest_answer，服务端原样读取当前对话最近的完整答复；"
        "整理用户给出的文字时 source=user_message；明确需要新写文档时 source=content，"
        "content 放入完整 Markdown 正文，不得添加未经核验的事实。"
        "用户只说要 PDF/Word 时默认导出最近答复，缺少材料则澄清，不检索知识库。"
    ),
    "parameters": ExportRequest.model_json_schema(),
}


def export_only_request(prompt: str) -> bool:
    """明显的文件交付请求仅开放排版工具；明确补查资料时仍允许研究工具。"""
    if re.search(r"\bzip\b|压缩包|打包|fetch_picture", prompt, re.IGNORECASE):
        return False
    if not re.search(r"pdf|word|docx|markdown|\.md\b", prompt, re.IGNORECASE):
        return False
    intent = re.sub(r"不(?:要|用|必|需)?(?:联网|检索|搜索)|无需(?:联网|检索|搜索)", "", prompt)
    if re.search(
        r"检索|搜索|调研|查找|查询|联网|搜集|收集|最新|深度研究|深入研究|"
        r"研究(?!结果|报告|内容|材料|文字|结论|成果)",
        intent,
    ):
        return False
    if re.search(r"是什么|怎么打开|如何打开|有什么区别|读取|阅读|读一下|读出|文件内容", intent):
        return False
    return len(prompt.strip()) <= 20 or bool(
        re.search(
            r"生成|导出|保存|转换|转为|转成|制作|整理|给我|提供|发我|发给|我要|做成|版本|格式|文件",
            intent,
        )
    )


def request_history(db: Session, run: Run) -> list[dict[str, Any]]:
    """保留任务之前的多轮用户要求，格式补充不能替代最初目标。"""
    query = select(Message).where(
        Message.conversation_id == run.conversation_id, Message.role == "user"
    )
    boundary = run.snapshot.get("conversation_upto_sequence")
    if boundary is not None:
        query = query.where(Message.sequence <= int(str(boundary)))
    rows = db.scalars(query.order_by(Message.sequence.desc()).limit(24))
    result: list[dict[str, Any]] = []
    size = 0
    for row in rows:
        length = len(row.content.encode())
        if result and size + length > 10000:
            break
        result.append({"sequence": row.sequence, "content": row.content})
        size += length
    return list(reversed(result))


def conversation_export_only(db: Session, run: Run) -> bool:
    """前面的研究未交付时，PDF 补充沿用完整工具；已有结果才仅导出。"""
    if not export_only_request(run.prompt):
        return False
    history = request_history(db, run)
    goals = [
        m
        for m in history[:-1]
        if not export_only_request(m["content"])
        and re.search(r"调研|研究|检索|搜索|搜集|收集|近期|最近|最新", m["content"])
    ]
    if not goals:
        return True
    produced_report = select(ResearchReport.id).where(ResearchReport.run_id == Message.id).exists()
    used_sources = (
        select(Run.id)
        .where(
            Run.id == Message.id,
            Run.status == "succeeded",
            select(TraceStep.id)
            .where(
                TraceStep.run_id == Run.id,
                TraceStep.kind == "tool",
                TraceStep.status == "succeeded",
                TraceStep.name.in_(["web_search", "knowledge_search", "research_report"]),
            )
            .exists(),
        )
        .exists()
    )
    completed = db.scalar(
        select(Message.id)
        .where(
            Message.conversation_id == run.conversation_id,
            Message.role == "assistant",
            Message.id != run.id,
            Message.sequence > goals[-1]["sequence"],
            Message.sequence <= history[-1]["sequence"],
            produced_report | used_sources,
        )
        .limit(1)
    )
    return completed is not None


def create_files(
    execution: "Execution",
    settings: Settings,
    snapshot: dict[str, Any],
    factory: Callable[[Settings, str, str], Provider],
    key: str,
    arguments: dict[str, Any],
) -> dict[str, object]:
    """复用已完成工具的研究与文件；冻结调用参数并持续检查当前权限。"""
    args = ResearchFileRequest.model_validate(arguments)
    fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
    execution.update()
    with Session(execution.engine) as db:
        run = db.scalar(select(Run).where(Run.id == execution.key).with_for_update())
        assert run is not None
        execution.check(db, run)
        if args.user_material.strip() and not any(
            args.user_material in content
            for content in db.scalars(
                select(Message.content).where(
                    Message.conversation_id == run.conversation_id, Message.role == "user",
                    Message.sequence <= int(str(run.snapshot.get(
                        "conversation_upto_sequence", 2**63 - 1))),
                )
            )
        ):
            return {
                "ok": False,
                "error_code": "research_material_unverified",
                "hint": "资料必须原样摘自当前对话的用户消息，不能将模型补写内容冒充原始证据。",
            }
        for base_id in args.knowledge_base_ids:
            base = db.get(KnowledgeBase, str(UUID(base_id)))
            if not base or base.deleted or base.account_id != run.owner_id:
                raise ProviderFailure("knowledge_permission_revoked")
        if args.web_enabled and "web_search" not in snapshot["tools"]:
            return {
                "ok": False,
                "error_code": "tool_not_authorized",
                "hint": "网页研究需要授权并配置网页搜索；可改用内部资料或用户提供的内容。",
            }
        if not args.web_enabled and not args.knowledge_base_ids and not args.user_material.strip():
            return {
                "ok": False,
                "error_code": "research_sources_required",
                "hint": "请补充要整理的资料，或选择可用的研究来源。",
            }
        tools = dict(cast(dict[str, Any], run.checkpoint.get("research_tools", {})))
        saved = tools.get(key)
        if saved and saved["fingerprint"] != fingerprint:
            raise ProviderFailure("tool_arguments_changed")
        if saved:
            project = db.get(ResearchProject, saved["project_id"])
            assert project is not None
        else:
            history = request_history(db, run)
            summary = db.get(ConversationSummary, run.conversation_id)
            question = (
                args.question
                + "\n\n用户多轮要求，按时间顺序，后续修改优先：\n"
                + json.dumps(history, ensure_ascii=False)
            )
            if summary and (
                run.snapshot.get("conversation_upto_sequence") is None
                or summary.upto_sequence <= int(str(run.snapshot["conversation_upto_sequence"]))
            ):
                question += "\n较早对话摘要（背景资料）：\n" + summary.content
            project = ResearchProject(
                owner_id=run.owner_id,
                title=args.title,
                question=question,
                options={
                    "web_enabled": args.web_enabled,
                    "knowledge_base_ids": args.knowledge_base_ids,
                    "max_rounds": 2,
                    "timeout_seconds": 600,
                    "formats": args.formats,
                },
                state={"run_id": run.id, "phase": "researching"},
            )
            db.add(project)
            db.flush()
            tools[key] = {"project_id": project.id, "fingerprint": fingerprint}
            run.checkpoint = {**run.checkpoint, "research_tools": tools}
            run.snapshot = {
                **run.snapshot,
                "timeout_seconds": max(int(str(run.snapshot["timeout_seconds"])), 600),
            }
            db.commit()
        project_id = project.id
    provider = factory(settings, str(snapshot["provider"]), str(snapshot["model"]))
    runner = ResearchRunner(execution, provider, {**snapshot, "research_project_id": project_id})
    if args.user_material.strip():
        runner.add_source(
            "user_material",
            {
                "kind": "user",
                "title": "用户提供的资料",
                "read_status": "user_provided",
                "content": args.user_material,
            },
        )
    runner.run()
    return save_files(execution, project_id, args.title, args.formats)


def save_files(
    execution: "Execution",
    project_id: str,
    title: str,
    formats: list[FileFormat],
) -> dict[str, object]:
    """研究与直接导出共用持久文件保存，导出本身不需要模型或资料检索。"""
    with Session(execution.engine) as db:
        report = db.scalar(select(ResearchReport).where(ResearchReport.project_id == project_id))
        assert report is not None
        markdown = report.markdown
        files = dict(report.data.get("files", {}))
    for format in dict.fromkeys(formats):
        execution.update()
        if format not in files:
            content = export_report(markdown, format)
            files[format] = {
                "filename": filename(title, format),
                "media_type": MEDIA_TYPES[format],
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content": base64.b64encode(content).decode("ascii"),
            }
    with Session(execution.engine) as db:
        run = db.scalar(select(Run).where(Run.id == execution.key).with_for_update())
        assert run is not None
        execution.check(db, run)
        project = db.get(ResearchProject, project_id)
        assert project is not None
        validate_sources(db, project)
        report = db.scalar(select(ResearchReport).where(ResearchReport.project_id == project_id))
        assert report is not None
        report.data = {**report.data, "files": files}
        project.state = {**project.state, "phase": "completed", "current_step": "文件已生成"}
        db.commit()
    return {
        "ok": True,
        "summary": "文件已生成，下载附件由聊天界面展示。",
        "files": [{"filename": f["filename"], "size": f["size"]} for f in files.values()],
    }


def export_files(execution: "Execution", key: str, arguments: dict[str, Any]) -> dict[str, object]:
    """读取本对话已有答复或待排版正文，直接导出并沿用原资料权限。"""
    args = ExportRequest.model_validate(arguments)
    digest = hashlib.sha256(json.dumps(arguments, sort_keys=True).encode()).hexdigest()
    execution.update()
    with Session(execution.engine) as db:
        run = db.scalar(select(Run).where(Run.id == execution.key).with_for_update())
        assert run is not None
        execution.check(db, run)
        tools = dict(cast(dict[str, Any], run.checkpoint.get("research_tools", {})))
        saved = tools.get(key)
        if saved:
            if saved["fingerprint"] != digest:
                raise ProviderFailure("tool_arguments_changed")
            project_id = saved["project_id"]
        else:
            sources: list[dict[str, Any]] = []
            if args.source == "content":
                markdown = args.content.strip()
            else:
                message = db.scalar(
                    select(Message)
                    .where(
                        Message.conversation_id == run.conversation_id,
                        Message.sequence <= int(str(run.snapshot.get(
                            "conversation_upto_sequence", 2**63 - 1))),
                        Message.role == ("assistant" if args.source == "latest_answer" else "user"),
                        Message.id != run.id,
                        ~select(Run.id)
                        .where(Run.id == Message.id, Run.status != "succeeded")
                        .exists(),
                    )
                    .order_by(Message.sequence.desc())
                    .limit(1)
                )
                if message is None:
                    return {
                        "ok": False,
                        "error_code": "export_content_required",
                        "hint": "当前对话没有可导出的内容，请补充正文；不要为导出而检索。",
                    }
                markdown = message.content
                previous = db.scalar(
                    select(ResearchReport)
                    .join(ResearchProject, ResearchProject.id == ResearchReport.project_id)
                    .where(
                        ResearchReport.run_id == message.id,
                        ResearchProject.owner_id == run.owner_id,
                    )
                    .order_by(ResearchReport.created_at.desc())
                    .limit(1)
                )
                if previous:
                    old = db.get(ResearchProject, previous.project_id)
                    assert old is not None
                    validate_sources(db, old)
                    markdown = previous.markdown
                    sources = [
                        s.data
                        for s in db.scalars(
                            select(ResearchSource).where(ResearchSource.project_id == old.id)
                        )
                    ]
                else:
                    sources = [{**s, "kind": "knowledge", "content": ""} for s in message.sources]
            if not markdown:
                return {
                    "ok": False,
                    "error_code": "export_content_required",
                    "hint": "请提供待导出的正文。",
                }
            project = ResearchProject(
                owner_id=run.owner_id,
                title=args.title,
                question=run.prompt,
                options={"export_only": True, "web_enabled": False, "formats": args.formats},
                state={"run_id": run.id, "phase": "exporting", "current_step": "正在生成文件"},
            )
            db.add(project)
            db.flush()
            project_id = project.id
            db.add(
                ResearchReport(
                    project_id=project_id,
                    run_id=run.id,
                    version=1,
                    markdown=markdown,
                    data={"source": args.source},
                )
            )
            for index, source in enumerate(sources):
                db.add(ResearchSource(project_id=project_id, identity=str(index), data=source))
            db.flush()
            validate_sources(db, project)
            tools[key] = {"project_id": project_id, "fingerprint": digest}
            run.checkpoint = {**run.checkpoint, "research_tools": tools}
            db.commit()
    return save_files(execution, project_id, args.title, args.formats)
