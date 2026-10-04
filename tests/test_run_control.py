"""命令／状态文件的读写契约：坏文件绝不能把助手弄挂。"""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import run_control


class RunControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name) / "run"
        patcher = patch.object(run_control, "RUN_DIR", self.run_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        # patch 没生效的话测试会写进真实 logs/：这里当场把它钉住，避免假通过
        self.assertEqual(run_control.state_path().parent, self.run_dir)
        self.assertEqual(run_control.control_path().parent, self.run_dir)

    def test_state_round_trip_stamps_time(self):
        self.assertTrue(run_control.write_state({"rate": 1.5, "manual_paused": True}, now=1000.0))
        state = run_control.read_state(max_age=10, now=1005.0)
        self.assertEqual(state["rate"], 1.5)
        self.assertTrue(state["manual_paused"])
        self.assertEqual(state["updated_at"], 1000.0)
        self.assertTrue((self.run_dir / "run_state.json").exists())

    def test_stale_state_reads_as_none_but_waiting_is_exempt(self):
        run_control.write_state({"rate": 1.0}, now=1000.0)
        self.assertIsNone(run_control.read_state(max_age=10, now=1020.0))
        run_control.write_state({"rate": 1.0, "waiting": True}, now=1000.0)
        self.assertIsNotNone(run_control.read_state(max_age=10, now=9999.0),
                             "助手停在终端等回车时不能算没数据")

    def test_broken_state_file_reads_as_none(self):
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "run_state.json").write_text("{不是 JSON", encoding="utf-8")
        self.assertIsNone(run_control.read_state())

    def test_command_seq_continues_from_file(self):
        """序号取自文件：控制台重启后不会从 1 重新发号让助手永久忽略。"""
        first = run_control.write_command("pause")
        second = run_control.write_command("rate", 1.5)
        self.assertEqual((first["seq"], second["seq"]), (1, 2))
        (self.run_dir / "run_control.json").write_text(
            json.dumps({"seq": 7, "action": "pause", "value": None}), encoding="utf-8")
        third = run_control.write_command("resume")
        self.assertEqual(third["seq"], 8)
        # 命令文件被手删/改坏时序号不能倒退：否则助手会把之后的命令全部静默忽略
        (self.run_dir / "run_control.json").unlink()
        run_control.write_state({"applied_seq": 7})
        self.assertEqual(run_control.write_command("resume")["seq"], 8)

    def test_read_command_ignores_seen_and_bad_files(self):
        run_control.write_command("pause")
        self.assertEqual(run_control.read_command(0)["action"], "pause")
        self.assertIsNone(run_control.read_command(1), "同一个序号不能被执行两次")
        (self.run_dir / "run_control.json").write_text("[]", encoding="utf-8")
        self.assertIsNone(run_control.read_command(0))
        (self.run_dir / "run_control.json").write_text(
            json.dumps({"seq": 9, "action": "关机"}), encoding="utf-8")
        self.assertIsNone(run_control.read_command(0), "未知 action 必须忽略")
        (self.run_dir / "run_control.json").write_text(
            json.dumps({"seq": 9, "action": "rate", "value": 99}), encoding="utf-8")
        self.assertIsNone(run_control.read_command(0), "非法倍速必须忽略而不是当成不干预")

    def test_rate_null_means_no_intervention(self):
        run_control.write_command("rate", None)
        command = run_control.read_command(0)
        self.assertEqual(command["action"], "rate")
        self.assertIsNone(command["value"])

    def test_validate_rate_range(self):
        for good in (0.5, 1, 1.5, 2.0, 4):
            self.assertEqual(run_control.validate_rate(good), float(good))
        for bad in (0, 0.1, 4.5, "1.5", True, None, 1.234):
            self.assertIsNone(run_control.validate_rate(bad))

    def test_write_command_rejects_bad_input(self):
        self.assertIsNone(run_control.write_command("关机"))
        self.assertIsNone(run_control.write_command("rate", 99))
        self.assertFalse((self.run_dir / "run_control.json").exists())

    def test_pending_tracks_applied_seq(self):
        # 没有命令时永远不算待办
        self.assertFalse(run_control.pending(None))
        run_control.write_command("pause")
        # 有命令、但助手还没写过状态：算待办，页面要显示"等待助手响应"
        self.assertTrue(run_control.pending(None))
        self.assertTrue(run_control.pending({"applied_seq": 0}))
        self.assertFalse(run_control.pending({"applied_seq": 1}))
        # 序号不可用时不能当成已应用，否则页面会假装命令生效了
        self.assertTrue(run_control.pending({"applied_seq": "坏值"}))

    def test_invalid_command_never_keeps_pending_true(self):
        """坏命令文件不能让页面一直显示"等待助手响应"，也不能弄崩调用方。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        cases = [{"seq": True, "action": "pause"},          # isinstance(True, int) 是坑
                 {"seq": 5, "action": "关机"},               # action 不认识
                 {"seq": "7", "action": "pause"},           # 序号是字符串
                 {"seq": 3, "action": "rate", "value": 99}]  # 倍速越界
        for payload in cases:
            (self.run_dir / "run_control.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.subTest(payload=payload):
                self.assertEqual(run_control.current_seq(), 0)
                self.assertFalse(run_control.pending(None))
                self.assertIsNone(run_control.read_command(0))

    def test_read_command_tolerates_bad_seen_seq(self):
        """坏参数只当"没有命令"，不能让助手轮询抛异常。"""
        run_control.write_command("pause")
        self.assertEqual(run_control.read_command(None)["action"], "pause")
        self.assertEqual(run_control.read_command("0")["action"], "pause")
        self.assertIsNone(run_control.read_command("坏值"))

    def test_non_rate_command_drops_value(self):
        run_control.write_command("pause", 123)
        self.assertIsNone(run_control.read_command(0)["value"])

    def test_read_side_drops_value_for_non_rate_command(self):
        """手工改过的命令文件里，非 rate 命令带的 value 也要丢掉。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "run_control.json").write_text(
            json.dumps({"seq": 4, "action": "pause", "value": 123}), encoding="utf-8")
        self.assertIsNone(run_control.read_command(0)["value"])

    def test_read_command_rejects_bool_seen_seq(self):
        run_control.write_command("pause")
        self.assertIsNone(run_control.read_command(True), "bool 不是有效进度，不能退回 0 重放老命令")

    def test_unencodable_state_returns_false_and_leaves_no_tmp(self):
        """页面文本里的孤立代理项不能把异常抛出去，也不能留下 0 字节 .tmp。"""
        self.assertTrue(run_control.write_state({"lesson": "正常"}))  # 先确保运行目录真的存在
        self.assertFalse(run_control.write_state({"lesson": "\ud800"}))
        self.assertEqual(list(self.run_dir.glob("*.tmp")), [])
        self.assertEqual(run_control.read_state()["lesson"], "正常", "坏写入不能动上一次的好内容")

    def test_repeated_bad_command_warns_only_once(self):
        """坏命令被每 2 秒读一次时不能把 study.log 刷屏。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "run_control.json").write_text(
            json.dumps({"seq": 5, "action": "只此一份的坏命令"}), encoding="utf-8")
        with self.assertLogs("run_control", level="WARNING") as logs:
            for _ in range(4):
                run_control.current_seq()
        self.assertEqual(len(logs.output), 1)

    def test_waiting_exempts_missing_timestamp(self):
        """助手刚标上 waiting、还没盖上时间戳时也不能被当成没数据。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        state_file = self.run_dir / "run_state.json"
        state_file.write_text(json.dumps({"waiting": True, "rate": 1.5}), encoding="utf-8")
        state = run_control.read_state(max_age=10, now=9999.0)
        self.assertIsNotNone(state)
        self.assertEqual(state["rate"], 1.5)
        state_file.write_text(json.dumps({"waiting": "false"}), encoding="utf-8")
        self.assertIsNone(run_control.read_state(max_age=10, now=9999.0), "waiting 只认真正的 true")

    def test_concurrent_writes_all_succeed_with_unique_seq(self):
        """控制台是多线程 HTTP 服务：并发写不能互相抢掉，也不能取到同一个号。"""
        results = []
        results_lock = threading.Lock()

        def writer():
            ok = run_control.write_command("pause") is not None
            with results_lock:
                results.append(ok)

        threads = [threading.Thread(target=writer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [True] * 4)
        self.assertEqual(run_control.current_seq(), 4, "四次写入应当是连续的 1..4")

    def test_write_while_target_is_open_retries(self):
        """目标文件正被读方打开时（Windows 上会挡住替换）也要写成功。"""
        run_control.write_command("pause")
        held = run_control.control_path().open("r", encoding="utf-8")
        closer = threading.Timer(0.02, held.close)
        closer.start()
        self.addCleanup(closer.cancel)
        try:
            self.assertIsNotNone(run_control.write_command("resume"))
        finally:
            if not held.closed:
                held.close()

    def test_failed_write_leaves_no_tmp_behind(self):
        """替换失败时不能留下 .tmp 垃圾。"""
        self.run_dir.mkdir(parents=True, exist_ok=True)
        with patch.object(run_control.os, "replace", side_effect=OSError("被占用")):
            self.assertFalse(run_control.write_command("pause"))
        self.assertEqual(list(self.run_dir.glob("*.tmp")), [])

    def test_write_failure_is_reported_not_raised(self):
        """运行目录被同名文件占住时，写失败只返回 False，不抛异常、不弄崩调用方。"""
        blocked = Path(self.temp.name) / "blocked"
        blocked.write_text("占位文件", encoding="utf-8")
        with patch.object(run_control, "RUN_DIR", blocked):
            self.assertFalse(run_control.write_state({"rate": 1}))
            self.assertIsNone(run_control.read_state())


if __name__ == "__main__":
    unittest.main()
