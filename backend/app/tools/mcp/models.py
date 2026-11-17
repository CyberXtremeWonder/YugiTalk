"""用户私有 MCP 服务器、不可变配置和独立加密凭证。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.entities import Base, identifier, now


class MCPServer(Base):
    __tablename__ = "mcp_servers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    account_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(80))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MCPServerVersion(Base):
    __tablename__ = "mcp_server_versions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=identifier)
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id"), index=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSON)


class MCPCredential(Base):
    __tablename__ = "mcp_credentials"
    server_id: Mapped[str] = mapped_column(ForeignKey("mcp_servers.id"), primary_key=True)
    encrypted: Mapped[str] = mapped_column(Text)
