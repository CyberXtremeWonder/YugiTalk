"""验证迁移复制的边界，防止覆盖已有数据或通过链接越界。"""

import runpy
import tempfile
import unittest
from pathlib import Path

COPY = runpy.run_path(str(Path(__file__).with_name("copy-persistent-data.py")))["copy"]


class PersistentCopyTests(unittest.TestCase):
    """覆盖复制一致性、已有数据保护和源路径越界拒绝。"""

    def test_nested_binary_and_empty_directory(self) -> None:
        """二进制内容与空目录必须保留，校验结果记录真实字节数。"""
        with tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1] / ".cache"
        ) as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir()
            target.mkdir()
            (source / "nested").mkdir()
            (source / "empty").mkdir()
            content = bytes(range(256))
            (source / "nested" / "binary").write_bytes(content)
            result = COPY(source, target)
            self.assertEqual((target / "nested" / "binary").read_bytes(), content)
            self.assertTrue((target / "empty").is_dir())
            self.assertEqual(result["bytes"], len(content))
            self.assertTrue(result["verified"])

    def test_existing_destination_is_not_overwritten(self) -> None:
        """目标已有任何文件时拒绝复制，并保留原有内容。"""
        with tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1] / ".cache"
        ) as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir()
            target.mkdir()
            (target / "existing").write_text("不能覆盖", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                COPY(source, target)
            self.assertEqual(
                (target / "existing").read_text(encoding="utf-8"), "不能覆盖"
            )

    def test_symbolic_link_is_rejected(self) -> None:
        """源目录链接指向其他数据时必须拒绝迁移，不读取或复制链接内容。"""
        with tempfile.TemporaryDirectory(
            dir=Path(__file__).resolve().parents[1] / ".cache"
        ) as directory:
            root = Path(directory)
            source, target = root / "source", root / "target"
            source.mkdir()
            target.mkdir()
            outside = root / "outside"
            outside.write_text("其他数据", encoding="utf-8")
            try:
                (source / "linked").symlink_to(outside)
            except OSError:
                self.skipTest("本机不允许创建符号链接，须在 Linux 容器执行此项")
            with self.assertRaises(RuntimeError):
                COPY(source, target)
            self.assertFalse(any(target.iterdir()))


if __name__ == "__main__":
    (Path(__file__).resolve().parents[1] / ".cache").mkdir(exist_ok=True)
    unittest.main()
