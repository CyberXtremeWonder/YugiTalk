"""管理员模型用量与独立账户余额，只展示已记录或供应商返回的数据。"""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, DecimalException
from typing import cast
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import SecretStr
from sqlalchemy import case, exists, func, select
from sqlalchemy.orm import Session

from app.agents.model_config import model_settings
from app.agents.models import Agent, AgentVersion
from app.core.config import Settings
from app.core.entities import now
from app.identity.dependencies import DB, Actor
from app.observability.trace_models import TraceStep
from app.providers.client import ProviderFailure
from app.tasks.models import Run

router = APIRouter(prefix="/api/v1/admin/usage", tags=["模型用量"])
LOCAL_TIME = timezone(timedelta(hours=8))
MAX_CONNECTIONS = 12
MAX_AGENTS = 50
BALANCE_LIMIT = 32 * 1024


def usage_snapshot(db: Session, sampled: datetime, days: int) -> dict[str, object]:
    """按真实请求时间及实际模型聚合，未知用量保留独立计数。"""
    first = sampled.astimezone(LOCAL_TIME).replace(hour=0, minute=0, second=0, microsecond=0)
    first -= timedelta(days=days - 1)
    boundaries = [(first + timedelta(days=index)).astimezone(UTC) for index in range(days)]
    scope = (TraceStep.kind == "llm", TraceStep.started_at >= boundaries[0],
             TraceStep.started_at <= sampled)
    provider = func.coalesce(TraceStep.details["provider"].as_string(), "unknown")
    unknown = case((TraceStep.input_tokens.is_(None) | TraceStep.output_tokens.is_(None), 1),
                   else_=0)
    models = []
    for name, model, calls, inputs, outputs, missing in db.execute(
        select(provider, TraceStep.name, func.count(), func.sum(TraceStep.input_tokens),
               func.sum(TraceStep.output_tokens), func.sum(unknown))
        .where(*scope).group_by(provider, TraceStep.name)
        .order_by(func.count().desc(), provider, TraceStep.name)
    ):
        inputs, outputs = int(cast(int, inputs or 0)), int(cast(int, outputs or 0))
        models.append({
            "provider": name, "model": model, "model_calls": calls,
            "input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs,
            "unknown_usage_calls": int(cast(int, missing or 0)), "cost": None,
            "cost_status": "not_connected",
        })
    bucket = case(*[(TraceStep.started_at < boundary, index - 1)
                    for index, boundary in enumerate(boundaries) if index], else_=days - 1)
    daily = {int(cast(int, index)): (int(cast(int, inputs or 0)),
                                  int(cast(int, outputs or 0)), int(cast(int, calls)))
             for index, inputs, outputs, calls in db.execute(
                 select(bucket, func.sum(TraceStep.input_tokens),
                        func.sum(TraceStep.output_tokens), func.count())
                 .where(*scope).group_by(bucket)
             )}
    unrecorded = db.scalar(select(func.count()).select_from(Run).where(
        Run.created_at >= boundaries[0], Run.created_at <= sampled,
        Run.status.in_(["succeeded", "failed", "cancelled"]),
        ~exists(select(TraceStep.id).where(TraceStep.run_id == Run.id, TraceStep.kind == "llm")),
    )) or 0
    summary = {key: sum(int(cast(int, row[key])) for row in models) for key in (
        "model_calls", "input_tokens", "output_tokens", "total_tokens", "unknown_usage_calls",
    )}
    summary["unrecorded_runs"] = unrecorded
    return {
        "sampled_at": sampled.isoformat(), "days": days, "timezone": "Asia/Shanghai",
        "summary": summary, "models": models, "balances": [], "cost_status": "not_connected",
        "trend": [{"date": (first + timedelta(days=index)).date().isoformat(),
                   "input_tokens": daily.get(index, (0, 0, 0))[0],
                   "output_tokens": daily.get(index, (0, 0, 0))[1],
                   "model_calls": daily.get(index, (0, 0, 0))[2]} for index in range(days)],
    }


@router.get("")
def usage(actor: Actor, db: DB, days: int = 30) -> dict[str, object]:
    """统计请求不调用供应商，余额通过独立接口按需刷新。"""
    if not actor.user.platform_admin:
        raise HTTPException(403)
    if days not in {7, 30, 90}:
        raise HTTPException(422, detail={"code": "invalid_usage_window"})
    return usage_snapshot(db, now(), days)


def official_deepseek(base_url: str) -> bool:
    """仅识别固定官方 HTTPS 地址，不把密钥发送到自定义代理。"""
    try:
        url = urlsplit(base_url)
        return (url.scheme == "https" and url.hostname == "api.deepseek.com"
                and url.port in {None, 443} and not url.username and not url.password
                and not url.query and not url.fragment
                and url.path.rstrip("/") in {"", "/v1", "/anthropic"})
    except ValueError:
        return False


@dataclass
class Connection:
    """仅在本次请求内持有凭证，不把密钥或其指纹加入公开响应。"""

    provider: str
    base_url: str
    key: str = field(repr=False)
    labels: list[str] = field(default_factory=list)
    models: set[str] = field(default_factory=set)
    invalid: bool = False


def connections(db: Session, settings: Settings, user: str) -> tuple[list[Connection], bool]:
    """限定服务器配置及当前管理员自己的最新助手，不读取其他用户凭证。"""
    result: dict[tuple[str, str], Connection] = {}
    truncated = False

    def add(config: Settings, label: str, invalid: bool = False) -> None:
        nonlocal truncated
        key = config.model_api_key.get_secret_value()
        endpoint = "deepseek" if official_deepseek(config.model_base_url) else config.model_base_url
        identity = (endpoint, key) if key else (endpoint, label)
        if identity not in result:
            if len(result) >= MAX_CONNECTIONS:
                truncated = True
                return
            result[identity] = Connection(config.model_provider, config.model_base_url, key,
                                          invalid=invalid)
        item = result[identity]
        item.labels.append(label)
        item.models.add(config.model_id)

    add(settings, "服务端默认连接")
    for label, model, key in (
        ("RAG 规划与改写", settings.rag_fast_model, settings.rag_fast_api_key),
        ("RAG 证据评分", settings.rag_grader_model, settings.rag_grader_api_key),
    ):
        if model or key.get_secret_value():
            add(settings.model_copy(update={"model_id": model or settings.model_id,
                                            "model_api_key": key if key.get_secret_value()
                                            else settings.model_api_key}), label)
    latest = select(AgentVersion.agent_id, func.max(AgentVersion.number).label("number"))\
        .group_by(AgentVersion.agent_id).subquery()
    rows = db.execute(select(Agent.name, AgentVersion.config).join(
        AgentVersion, AgentVersion.agent_id == Agent.id,
    ).join(latest, (latest.c.agent_id == Agent.id) & (latest.c.number == AgentVersion.number))
        .where(Agent.account_id == user, Agent.deleted.is_(False))
        .order_by(Agent.id).limit(MAX_AGENTS + 1)).all()
    truncated = truncated or len(rows) > MAX_AGENTS
    for name, config in rows[:MAX_AGENTS]:
        try:
            add(model_settings(settings, config), "助手：" + name)
        except ProviderFailure:
            add(settings.model_copy(update={"model_provider": str(config.get("provider", "")),
                                            "model_id": str(config.get("model", "")),
                                            "model_base_url": str(config.get("base_url", "")),
                                            "model_api_key": SecretStr("")}),
                "助手：" + name, invalid=True)
    return list(result.values()), truncated


def money(value: object) -> str:
    """金额只接受有限非负十进制字符串，限制异常指数避免无界格式化。"""
    if not isinstance(value, str) or len(value) > 80:
        raise ValueError("invalid balance")
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0 or amount > Decimal("1e18"):
        raise ValueError("invalid balance")
    if amount.is_zero():
        return "0"
    if cast(int, amount.as_tuple().exponent) < -12:
        raise ValueError("invalid balance precision")
    return format(amount, "f")


def deepseek_balance(key: str) -> tuple[bool, list[dict[str, str]]]:
    """固定官方只读接口、短超时及响应上限，错误正文不会进入 API 响应。"""
    deadline = time.monotonic() + 4
    with httpx.Client(timeout=4, follow_redirects=False, trust_env=False) as client:
        with client.stream("GET", "https://api.deepseek.com/user/balance", headers={
            "Authorization": "Bearer " + key, "Accept-Encoding": "identity",
        }) as response:
            if response.status_code != 200 or response.headers.get(
                "content-encoding", "identity"
            ).lower() != "identity":
                raise ValueError("balance unavailable")
            content = bytearray()
            # 不按固定块大小缓冲，及时检查慢速小块响应的总截止时间。
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise ValueError("balance deadline")
                content.extend(chunk)
                if len(content) > BALANCE_LIMIT:
                    raise ValueError("balance too large")
    data = json.loads(content)
    if not isinstance(data, dict) or type(data.get("is_available")) is not bool:
        raise ValueError("invalid balance")
    rows = data.get("balance_infos")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 2:
        raise ValueError("invalid balance")
    amounts = []
    currencies = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("currency") not in {"CNY", "USD"}:
            raise ValueError("invalid currency")
        currency = row["currency"]
        if currency in currencies:
            raise ValueError("duplicate currency")
        currencies.add(currency)
        amounts.append({"currency": currency,
                        **{name: money(row.get(name)) for name in (
                            "total_balance", "granted_balance", "topped_up_balance",
                        )}})
    return data["is_available"], amounts


def balance_entry(item: Connection, index: int) -> dict[str, object]:
    """账户余额独立展示；相同账户关联多个模型时仍只查询和展示一次。"""
    official = (item.provider in {"openai_compatible", "anthropic"}
                and official_deepseek(item.base_url))
    entry: dict[str, object] = {
        "id": f"account-{index}", "label": "、".join(item.labels),
        "provider": "deepseek" if official else item.provider, "models": sorted(item.models),
        "status": "not_connected", "checked_at": None, "is_available": None, "amounts": [],
    }
    if item.invalid:
        entry["status"] = "unavailable"
    elif official and not item.key:
        entry["status"] = "not_configured"
    elif official:
        try:
            available, amounts = deepseek_balance(item.key)
            entry.update(status="available", is_available=available, amounts=amounts)
        except (httpx.HTTPError, ValueError, TypeError, KeyError, DecimalException):
            entry["status"] = "unavailable"
        entry["checked_at"] = now().isoformat()
    return entry


@router.get("/balances")
def balances(request: Request, actor: Actor, db: DB) -> dict[str, object]:
    """显式刷新当前管理员可用的账户余额，并限制扫描与网络并发。"""
    if not actor.user.platform_admin:
        raise HTTPException(403)
    items, truncated = connections(db, request.app.state.settings, actor.user.id)
    with ThreadPoolExecutor(max_workers=4) as pool:
        entries = list(pool.map(balance_entry, items, range(1, len(items) + 1)))
    return {"sampled_at": now().isoformat(), "balances": entries, "truncated": truncated,
            "connection_limit": MAX_CONNECTIONS, "agent_limit": MAX_AGENTS}
