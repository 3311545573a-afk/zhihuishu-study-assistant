import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from cleanup import (
    DEFAULT_KEEP_COUNT,
    DEFAULT_KEEP_DAYS,
    DEFAULT_KEEP_MB,
    Retention,
    clean,
    main,
    plan,
    scan,
    validate_retention,
)


def make_folder(root: Path, name: str, file_size: int = 100, age_days: float = 0.0,
                files=("page.json", "page.png")):
    folder = Path(root) / name
    folder.mkdir(parents=True, exist_ok=True)
    for filename in files:
        (folder / filename).write_bytes(b"x" * file_size)
    if age_days:
        stamp = time.time() - age_days * 86400
        os.utime(folder, (stamp, stamp))
    return folder


class ValidateRetentionTests(unittest.TestCase):
    def test_defaults_when_missing(self):
        self.assertEqual(validate_retention(None),
                         {"keep_days": DEFAULT_KEEP_DAYS, "keep_count": DEFAULT_KEEP_COUNT,
                          "keep_mb": DEFAULT_KEEP_MB})
        self.assertEqual(validate_retention({})["keep_count"], DEFAULT_KEEP_COUNT)

    def test_values_are_normalised(self):
        result = validate_retention({"keep_days": 3, "keep_count": 10, "keep_mb": 50})
        self.assertEqual(result, {"keep_days": 3.0, "keep_count": 10, "keep_mb": 50.0})
        self.assertIsInstance(result["keep_count"], int)
        self.assertIsInstance(result["keep_days"], float)

    def test_invalid_values_are_rejected(self):
        for bad in ({"keep_days": 0}, {"keep_days": "7"}, {"keep_count": 0}, {"keep_count": True},
                    {"keep_mb": -1}, {"keep_mb": "200"}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_retention(bad)

    def test_non_object_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_retention([1, 2])

    def test_retention_from_broken_config_falls_back(self):
        for config in ({}, {"diagnostics": {"keep_days": "很久"}}, {"diagnostics": 5}, None, "x"):
            with self.subTest(config=config):
                self.assertEqual(Retention.from_config(config).keep_count, DEFAULT_KEEP_COUNT)

    def test_retention_from_config_reads_values(self):
        retention = Retention.from_config({"diagnostics": {"keep_days": 1.5, "keep_count": 3,
                                                           "keep_mb": 10}})
        self.assertEqual(retention.keep_count, 3)
        self.assertEqual(retention.keep_bytes, 10 * 1024 * 1024)


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_only_timestamped_folders_are_listed_newest_first(self):
        make_folder(self.root, "20261001-010101-000001")
        make_folder(self.root, "20261003-010101-000003")
        make_folder(self.root, "20261002-010101-000002")
        (self.root / "notes.txt").write_text("不是诊断", encoding="utf-8")
        make_folder(self.root, "随手建的文件夹")
        items = scan(self.root)
        self.assertEqual([item["name"] for item in items],
                         ["20261003-010101-000003", "20261002-010101-000002",
                          "20261001-010101-000001"])

    def test_sizes_are_summed(self):
        make_folder(self.root, "20261001-010101-000001", file_size=500)
        items = scan(self.root)
        self.assertEqual(items[0]["bytes"], 1000)

    def test_missing_directory_is_empty(self):
        self.assertEqual(scan(self.root / "不存在"), [])

    def test_symlinked_folder_is_ignored(self):
        make_folder(self.root, "20261001-010101-000001")
        link = self.root / "20261002-010101-000002"
        try:
            link.symlink_to(self.root / "20261001-010101-000001", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("本机不允许创建符号链接")
        self.assertNotIn("20261002-010101-000002", [item["name"] for item in scan(self.root)])


class PlanTests(unittest.TestCase):
    def items(self, specs):
        """specs: [(名字, 字节, 几天前)]，按新到旧给出。"""
        now = time.time()
        return [{"path": Path(name), "name": name, "created": now - age * 86400, "bytes": size}
                for name, size, age in specs]

    def test_newest_is_always_kept(self):
        items = self.items([("20261003-000000-000000", 999 * 1024 * 1024, 0)])
        keep, delete = plan(items, Retention(keep_days=0.01, keep_count=1, keep_mb=0.1), now=time.time())
        self.assertEqual(len(keep), 1)
        self.assertEqual(delete, [])

    def test_age_limit(self):
        now = time.time()
        items = self.items([("20261003-000000-000000", 10, 0), ("20261001-000000-000000", 10, 30)])
        keep, delete = plan(items, Retention(keep_days=7, keep_count=50, keep_mb=100), now=now)
        self.assertEqual([i["name"] for i in keep], ["20261003-000000-000000"])
        self.assertEqual([i["name"] for i in delete], ["20261001-000000-000000"])

    def test_count_limit_deletes_oldest_first(self):
        items = self.items([(f"2026100{i}-000000-000000", 10, 0) for i in (5, 4, 3, 2, 1)])
        keep, delete = plan(items, Retention(keep_days=3650, keep_count=2, keep_mb=1000))
        self.assertEqual([i["name"] for i in keep], ["20261005-000000-000000", "20261004-000000-000000"])
        self.assertEqual([i["name"] for i in delete], ["20261003-000000-000000", "20261002-000000-000000",
                                                       "20261001-000000-000000"])

    def test_size_limit_keeps_as_many_as_fit(self):
        mb = 1024 * 1024
        items = self.items([("20261003-000000-000000", 3 * mb, 0), ("20261002-000000-000000", 3 * mb, 0),
                            ("20261001-000000-000000", 3 * mb, 0)])
        keep, delete = plan(items, Retention(keep_days=3650, keep_count=50, keep_mb=7))
        self.assertEqual([i["name"] for i in keep], ["20261003-000000-000000", "20261002-000000-000000"])
        self.assertEqual([i["name"] for i in delete], ["20261001-000000-000000"])

    def test_size_limit_deletes_everything_that_would_overflow(self):
        mb = 1024 * 1024
        items = self.items([("20261003-000000-000000", 3 * mb, 0), ("20261002-000000-000000", 3 * mb, 0)])
        keep, delete = plan(items, Retention(keep_days=3650, keep_count=50, keep_mb=5))
        self.assertEqual([i["name"] for i in keep], ["20261003-000000-000000"])
        self.assertEqual([i["name"] for i in delete], ["20261002-000000-000000"])


class CleanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build(self):
        make_folder(self.root, "20261001-000000-000001", age_days=30)
        make_folder(self.root, "20261002-000000-000002", age_days=20)
        make_folder(self.root, "20261003-000000-000003")

    def test_dry_run_does_not_delete(self):
        self.build()
        report = clean(self.root, Retention(keep_days=7), dry_run=True)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["deleted_count"], 2)
        self.assertEqual(report["kept_count"], 1)
        self.assertTrue(report["ok"])
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["20261001-000000-000001", "20261002-000000-000002", "20261003-000000-000003"])

    def test_clean_deletes_only_beyond_policy(self):
        self.build()
        report = clean(self.root, Retention(keep_days=7))
        self.assertEqual(sorted(report["deleted"]),
                         ["20261001-000000-000001", "20261002-000000-000002"])
        self.assertEqual(report["freed_bytes"], 400)
        self.assertEqual([p.name for p in self.root.iterdir()], ["20261003-000000-000003"])
        self.assertEqual(report["usage"]["count"], 3)
        self.assertEqual(report["kept_bytes"], 200)

    def test_missing_directory_reports_empty(self):
        report = clean(self.root / "没有这个目录")
        self.assertTrue(report["ok"])
        self.assertEqual(report["deleted_count"], 0)
        self.assertEqual(report["usage"], {"count": 0, "bytes": 0, "newest": "", "oldest": ""})

    def test_delete_failure_is_reported_not_raised(self):
        self.build()
        with patch("cleanup.shutil.rmtree", side_effect=OSError("被占用")):
            report = clean(self.root, Retention(keep_days=7))
        self.assertFalse(report["ok"])
        self.assertEqual(report["deleted_count"], 0)
        self.assertEqual(len(report["errors"]), 2)
        self.assertIn("被占用", report["errors"][0])

    def test_non_diagnostic_files_are_left_alone(self):
        self.build()
        (self.root / "keepme.txt").write_text("重要", encoding="utf-8")
        clean(self.root, Retention(keep_days=7))
        self.assertTrue((self.root / "keepme.txt").exists())
        self.assertEqual((self.root / "keepme.txt").read_text(encoding="utf-8"), "重要")


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.diagnostics = self.root / "diagnostics"
        make_folder(self.diagnostics, "20260101-000000-000001", age_days=100)
        make_folder(self.diagnostics, "20261003-000000-000002")

    def test_dry_run_lists_without_deleting(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = main(["--root", str(self.root), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("将删除 1 个", out.getvalue())
        self.assertIn("20260101-000000-000001", out.getvalue())
        self.assertEqual(len(list(self.diagnostics.iterdir())), 2)

    def test_real_run_deletes(self):
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = main(["--root", str(self.root), "--keep-days", "7"])
        self.assertEqual(code, 0)
        self.assertIn("已删除 1 个", out.getvalue())
        self.assertEqual([p.name for p in self.diagnostics.iterdir()], ["20261003-000000-000002"])

    def test_invalid_options_exit_2(self):
        err = io.StringIO()
        with patch("sys.stderr", err):
            code = main(["--root", str(self.root), "--keep-count", "0"])
        self.assertEqual(code, 2)
        self.assertIn("参数无效", err.getvalue())


if __name__ == "__main__":
    unittest.main()
