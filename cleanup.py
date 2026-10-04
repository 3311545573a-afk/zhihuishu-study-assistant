"""按保留策略清理 diagnostics/ 下的旧诊断文件夹。

助手每次暂停都会在 diagnostics/ 里存一份现场（page.png 截图 + page.json 控件结构），
积累起来很占地方。这里只处理该目录下形如 20261003-070803-690831 的文件夹，
永远保留最新一个，绝不碰其他文件。

默认策略：保留最近 7 天、最多 50 个、总计不超过 200 MB，超出部分从最旧的开始删。
默认只在被调用时执行，不会自己定时删东西。
"""
import argparse
from dataclasses import dataclass
from pathlib import Path
import re
import shutil
import sys
import time

DIAGNOSTICS_DIRNAME = "diagnostics"
FOLDER_NAME = re.compile(r"^\d{8}-\d{6}-\d{6}$")
DEFAULT_KEEP_DAYS = 7.0
DEFAULT_KEEP_COUNT = 50
DEFAULT_KEEP_MB = 200.0
LIMITS = {"keep_days": (0.01, 3650.0), "keep_count": (1, 100000), "keep_mb": (1.0, 1_000_000.0)}


def validate_retention(raw) -> dict:
    """校验保留策略，返回规范化字典；非法值抛 ValueError，缺省字段补默认值。"""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("diagnostics 必须是 JSON 对象")
    result = {"keep_days": DEFAULT_KEEP_DAYS, "keep_count": DEFAULT_KEEP_COUNT,
              "keep_mb": DEFAULT_KEEP_MB}
    for name, (low, high) in LIMITS.items():
        if name not in raw or raw[name] is None:
            continue
        value = raw[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"diagnostics.{name} 必须是数字")
        if not low <= value <= high:
            raise ValueError(f"diagnostics.{name} 必须在 {low} 到 {high} 之间")
        result[name] = int(value) if name == "keep_count" else float(value)
    return result


@dataclass(frozen=True)
class Retention:
    keep_days: float = DEFAULT_KEEP_DAYS
    keep_count: int = DEFAULT_KEEP_COUNT
    keep_mb: float = DEFAULT_KEEP_MB

    @classmethod
    def from_config(cls, config) -> "Retention":
        """配置缺失或非法时回落到默认值，清理本身不应该因为配置坏了而失败。"""
        try:
            data = validate_retention((config or {}).get("diagnostics"))
        except (AttributeError, ValueError, TypeError):
            data = validate_retention({})
        return cls(data["keep_days"], data["keep_count"], data["keep_mb"])

    @property
    def keep_bytes(self) -> float:
        return self.keep_mb * 1024 * 1024

    def as_dict(self) -> dict:
        return {"keep_days": self.keep_days, "keep_count": self.keep_count,
                "keep_mb": self.keep_mb}


def folder_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def scan(directory: Path) -> list[dict]:
    """列出诊断文件夹，按名字（即时间戳）从新到旧；只认固定命名格式的目录。"""
    directory = Path(directory)
    if not directory.is_dir():
        return []
    items = []
    for child in directory.iterdir():
        if not child.is_dir() or child.is_symlink() or not FOLDER_NAME.match(child.name):
            continue
        try:
            created = child.stat().st_mtime
        except OSError:
            created = 0.0
        items.append({"path": child, "name": child.name, "created": created,
                      "bytes": folder_size(child)})
    items.sort(key=lambda item: item["name"], reverse=True)
    return items


def plan(items: list, retention: Retention, now: float | None = None) -> tuple[list, list]:
    """返回（保留, 删除）。永远保留最新一个，之后按年龄、个数、体积三重上限从旧到新删。"""
    now = time.time() if now is None else now
    keep, delete = [], []
    kept_bytes = 0
    for index, item in enumerate(items):
        age_days = (now - item["created"]) / 86400 if item["created"] else 0.0
        too_old = age_days > retention.keep_days
        too_many = len(keep) >= retention.keep_count
        too_big = bool(keep) and kept_bytes + item["bytes"] > retention.keep_bytes
        if index == 0 or not (too_old or too_many or too_big):
            keep.append(item)
            kept_bytes += item["bytes"]
        else:
            delete.append(item)
    return keep, delete


def usage(items: list) -> dict:
    return {
        "count": len(items),
        "bytes": sum(item["bytes"] for item in items),
        "newest": items[0]["name"] if items else "",
        "oldest": items[-1]["name"] if items else "",
    }


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def clean(directory: Path, retention: Retention | None = None, dry_run: bool = False) -> dict:
    """按策略清理。dry_run 只报告会删什么，不动磁盘。"""
    directory = Path(directory)
    retention = retention or Retention()
    items = scan(directory)
    keep, doomed = plan(items, retention)
    errors: list[str] = []
    deleted: list[str] = []
    freed = 0
    for item in doomed:
        if not _inside(item["path"], directory):
            errors.append(f"{item['name']}：不在 diagnostics 目录内，已跳过")
            continue
        if dry_run:
            deleted.append(item["name"])
            freed += item["bytes"]
            continue
        try:
            shutil.rmtree(item["path"])
        except OSError as exc:
            errors.append(f"{item['name']}：删除失败（{exc}）")
            continue
        deleted.append(item["name"])
        freed += item["bytes"]
    return {
        "ok": not errors,
        "dry_run": dry_run,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "freed_bytes": freed,
        "kept_count": len(keep),
        "kept_bytes": sum(item["bytes"] for item in keep),
        "usage": usage(items),
        "errors": errors,
        "retention": retention.as_dict(),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="清理 diagnostics/ 下的旧诊断文件夹")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent,
                        help="项目根目录，默认本文件所在目录")
    parser.add_argument("--keep-days", type=float, default=DEFAULT_KEEP_DAYS)
    parser.add_argument("--keep-count", type=int, default=DEFAULT_KEEP_COUNT)
    parser.add_argument("--keep-mb", type=float, default=DEFAULT_KEEP_MB)
    parser.add_argument("--dry-run", action="store_true", help="只列出会删什么，不真的删")
    args = parser.parse_args(argv)
    try:
        # 命令行参数要严格校验：错了就直接报错，不能像读配置那样悄悄回落到默认值。
        values = validate_retention({"keep_days": args.keep_days, "keep_count": args.keep_count,
                                     "keep_mb": args.keep_mb})
    except ValueError as exc:
        print(f"参数无效：{exc}", file=sys.stderr)
        return 2
    retention = Retention(values["keep_days"], values["keep_count"], values["keep_mb"])
    report = clean(args.root / DIAGNOSTICS_DIRNAME, retention, dry_run=args.dry_run)
    verb = "将删除" if args.dry_run else "已删除"
    print(f"{verb} {report['deleted_count']} 个诊断文件夹，释放 "
          f"{report['freed_bytes'] / 1048576:.1f} MB；保留 {report['kept_count']} 个 / "
          f"{report['kept_bytes'] / 1048576:.1f} MB")
    for name in report["deleted"]:
        print("  - " + name)
    for error in report["errors"]:
        print("  错误：" + error, file=sys.stderr)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
