"""S3 大文件传输只执行有界读取，并在结束和取消时关闭连接。"""

import io

import pytest

from app.rag.storage import ObjectStorage


class Body(io.BytesIO):
    """拒绝一次性全量读取。"""

    def read(self, size=-1):
        """每次读取上限为 1 MB。"""
        assert 0 < size <= 1024 * 1024
        return super().read(size)


def storage_for(body):
    """仅构造 S3 传输边界，不联系外部服务或读取凭证。"""
    class Client:
        """保存 Range 请求及分段上传参数。"""

        request = None
        config = None

        def get_object(self, **kwargs):
            """返回受限读取流。"""
            self.request = kwargs
            return {"Body": body}

        def head_bucket(self, **kwargs):
            """模拟已存在的桶。"""
            pass

        def upload_fileobj(self, source, bucket, key, **kwargs):
            """SDK 获取原文件句柄，应用层不预先读取完整内容。"""
            self.config = kwargs["Config"]
            assert source.tell() == 0

    storage = ObjectStorage.__new__(ObjectStorage)
    storage.client = Client()
    storage.bucket = "test"
    return storage


def test_streaming_read_uses_range_and_closes():
    """续传从请求的字节位置开始且正常结束关闭响应流。"""
    body = Body(b"remaining")
    storage = storage_for(body)
    assert b"".join(storage.chunks("key", 100)) == b"remaining"
    assert storage.client.request["Range"] == "bytes=100-"
    assert body.closed


def test_streaming_cancel_closes_connection():
    """下载或索引中途退出时不遗留 S3 连接。"""
    body = Body(b"x" * (2 * 1024 * 1024))
    blocks = storage_for(body).chunks("key")
    assert len(next(blocks)) == 1024 * 1024
    blocks.close()
    assert body.closed


def test_streaming_size_limit_closes_connection(monkeypatch):
    """对象读取超出文件边界时关闭连接并明确失败。"""
    monkeypatch.setattr("app.rag.storage.MAX_JSONL_FILE_BYTES", 4)
    body = Body(b"12345")
    with pytest.raises(ValueError, match="file_too_large"):
        list(storage_for(body).chunks("key"))
    assert body.closed


def test_file_upload_passes_handle_and_bounded_transfer_config():
    """S3 multipart 使用有限分段并关闭并行预读线程。"""
    storage = storage_for(Body())
    source = io.BytesIO(b"content")
    storage.put_file("key", source)
    assert source.tell() == 0
    assert storage.client.config.multipart_chunksize == 8 * 1024 * 1024
    assert not storage.client.config.use_threads
