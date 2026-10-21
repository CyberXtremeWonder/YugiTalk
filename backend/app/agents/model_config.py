"""保存加密的版本密钥，并解析每个任务独立的模型连接配置。"""

import base64
import hashlib
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from app.core.config import Settings
from app.providers.client import ProviderFailure


def cipher(settings: Settings) -> Fernet:
    """用部署密钥派生存储加密密钥，API 与 worker 必须使用相同配置。"""
    key = hashlib.sha256(settings.session_secret.get_secret_value().encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    """公开模型配置只包含密钥是否已保存，不返回密文或明文。"""
    return {
        **{key: value for key, value in config.items() if key != "api_key_encrypted"},
        "has_api_key": bool(config.get("api_key_encrypted")),
    }


def model_settings(settings: Settings, config: dict[str, Any]) -> Settings:
    """显式配置按版本解密；旧版本仍检查服务器模型是否发生变化。"""
    if "base_url" not in config:
        # 旧版本没有独立连接信息，必须与当前服务器模型一致，不能无声改用其他模型执行。
        if config["provider"] != settings.model_provider or config["model"] != settings.model_id:
            raise ProviderFailure("model_configuration_changed")
        return settings
    try:
        encrypted = config.get("api_key_encrypted")
        key = cipher(settings).decrypt(encrypted.encode()).decode() if encrypted else ""
    except InvalidToken as error:
        raise ProviderFailure("model_configuration") from error
    return settings.model_copy(
        # 只覆盖本次任务的连接字段；全局 Settings 保持不变，其他助手不受影响。
        update={
            "model_provider": config["provider"],
            "model_id": config["model"],
            "model_base_url": config["base_url"],
            "model_api_key": SecretStr(key),
        }
    )
