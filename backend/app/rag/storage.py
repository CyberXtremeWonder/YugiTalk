"""S3 兼容原文存储，对象键由服务端生成，不接受用户文件路径。"""

from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import Settings


class ObjectStorage:
    """封装资料对象的持久存储操作，供索引与下载复用。"""

    def __init__(self, settings: Settings) -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.bucket = settings.storage_bucket
        self.settings_environment = settings.app_env
        self.region = settings.storage_region
        self.client: Any = boto3.client(
            "s3",
            endpoint_url=settings.storage_endpoint,
            aws_access_key_id=settings.storage_access_key.get_secret_value(),
            aws_secret_access_key=settings.storage_secret_key.get_secret_value(),
            region_name=settings.storage_region,
            verify=settings.storage_ca_file or True,
            config=Config(
                connect_timeout=5,
                read_timeout=15,
                retries={"max_attempts": 2},
                s3={"addressing_style": settings.storage_addressing_style},
            ),
        )

    def put(self, key: str, content: bytes) -> None:
        """将原始资料写入配置的对象存储位置。"""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as error:
            # 正式S3由运维预先创建专用Bucket，避免应用凭证拥有账户级建桶权限。
            if self.settings_environment == "production":
                raise
            if error.response["Error"]["Code"] not in {"404", "NoSuchBucket", "NotFound"}:
                raise
            try:
                options: dict[str, Any] = {"Bucket": self.bucket}
                if self.region != "us-east-1":
                    options["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
                self.client.create_bucket(**options)
            except ClientError as create_error:
                if create_error.response["Error"]["Code"] not in {
                    "BucketAlreadyOwnedByYou",
                    "BucketAlreadyExists",
                }:
                    raise
        self.client.put_object(
            Bucket=self.bucket, Key=key, Body=content, ContentType="application/octet-stream"
        )

    def get(self, key: str) -> bytes:
        """读取指定对象的内容，交由调用方执行权限与大小检查。"""
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        # 多读一个字节判断是否超限，且无论成功与否都关闭响应流，释放连接供后续读取。
        try:
            content: bytes = body.read(20 * 1024 * 1024 + 1)
        finally:
            body.close()
        if len(content) > 20 * 1024 * 1024:
            raise ValueError("file_too_large")
        return content

    def remove(self, key: str) -> None:
        """删除指定对象，供资料撤销与资源清理使用。"""
        self.client.delete_object(Bucket=self.bucket, Key=key)
