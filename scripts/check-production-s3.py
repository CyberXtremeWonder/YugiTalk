"""验收现有S3的两个专用Bucket，默认只读，可选小对象写入后立即删除。"""

import argparse
import json
import runpy
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]


def check_storage(
    env_file: Path, write_probe: bool = False, local_test: bool = False
) -> dict[str, object]:
    """验证端点及Bucket权限，不建桶、不修改业务对象、不输出凭证或服务异常正文。"""
    values = dict(dotenv_values(env_file, interpolate=False))
    initialize = runpy.run_path(str(ROOT / "scripts/init-production.py"))
    initialize["validate_storage"](values, local_test)
    if not values.get("STORAGE_ACCESS_KEY") or not values.get("STORAGE_SECRET_KEY"):
        raise ValueError(
            "S3接通检查必须显式配置服务凭证，禁止回退到其他账户的默认凭证链"
        )
    source = values.get("S3_CA_SOURCE") or ""
    client = boto3.client(
        "s3",
        endpoint_url=values["STORAGE_ENDPOINT"],
        aws_access_key_id=values.get("STORAGE_ACCESS_KEY"),
        aws_secret_access_key=values.get("STORAGE_SECRET_KEY"),
        region_name=values.get("STORAGE_REGION") or "us-east-1",
        verify=str((ROOT / source).resolve()) if source else True,
        config=Config(
            connect_timeout=5,
            read_timeout=15,
            retries={"max_attempts": 2},
            s3={"addressing_style": values.get("STORAGE_ADDRESSING_STYLE") or "path"},
        ),
    )
    results = []
    for label, setting in (
        ("原文", "STORAGE_BUCKET"),
        ("Milvus", "MILVUS_STORAGE_BUCKET"),
    ):
        bucket = values[setting]
        key = f"{values.get('MILVUS_STORAGE_PREFIX') or 'yugitalk-milvus'}/yugitalk-preflight/{uuid4()}"
        row = {
            "scope": label,
            "status": "passed",
            "write_verified": False,
            "cleanup_verified": None,
        }
        written = False
        version = None
        try:
            client.head_bucket(Bucket=bucket)
            # 只读验证ListBucket；仅返回一条，不记录业务键。
            client.list_objects_v2(Bucket=bucket, MaxKeys=1, Prefix=key)
            if write_probe:
                # put超时也可能已在服务方写入，发送请求前标记以确保尝试清理。
                written = True
                upload = client.put_object(
                    Bucket=bucket, Key=key, Body=b"yugitalk-s3-preflight-v1"
                )
                version = upload.get("VersionId")
                body = client.get_object(Bucket=bucket, Key=key)["Body"]
                try:
                    assert body.read(128) == b"yugitalk-s3-preflight-v1"
                finally:
                    body.close()
                row["write_verified"] = True
        except (
            BotoCoreError,
            ClientError,
            OSError,
            ValueError,
            AssertionError,
        ) as error:
            row["status"] = "failed"
            row["error_type"] = type(error).__name__
        finally:
            if written:
                try:
                    options = {"Bucket": bucket, "Key": key}
                    if version:
                        # 删除本次诊断版本，避免版本化Bucket只生成删除标记。
                        options["VersionId"] = version
                    client.delete_object(**options)
                    try:
                        client.head_object(Bucket=bucket, Key=key)
                    except ClientError as error:
                        if error.response["Error"]["Code"] not in {
                            "404",
                            "NoSuchKey",
                            "NotFound",
                        }:
                            raise
                    else:
                        raise AssertionError("探测对象未删除")
                    row["cleanup_verified"] = True
                except (
                    BotoCoreError,
                    ClientError,
                    OSError,
                    ValueError,
                    AssertionError,
                ) as error:
                    row.update(
                        status="failed",
                        cleanup_verified=False,
                        cleanup_key=key,
                        cleanup_error_type=type(error).__name__,
                    )
        results.append(row)
    client.close()
    return {
        "status": "passed"
        if all(row["status"] == "passed" for row in results)
        else "failed",
        "scope": "local_fixture" if local_test else "external_s3",
        "write_probe": write_probe,
        "buckets": results,
        "backup_restore_verified": False,
    }


def main() -> None:
    """接收私有配置路径，写入脱敏报告；缺少配置时给出明确提示。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--write-probe", action="store_true")
    parser.add_argument("--local-test", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "docs/stages/artifacts/stage6-s3-preflight.json",
    )
    args = parser.parse_args()
    try:
        report = check_storage(args.env_file, args.write_probe, args.local_test)
    except (BotoCoreError, ClientError, OSError, ValueError, AssertionError) as error:
        report = {
            "status": "failed",
            "error_type": type(error).__name__,
            "reason": "S3配置不完整、无效或未能建立客户端；请检查私有配置，不输出其内容",
        }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
