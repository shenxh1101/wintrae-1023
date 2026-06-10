#!/usr/bin/env python3
"""
Personal Efficiency Organizer
Batch-organize local files and schedule materials for knowledge workers.

Tasks: scan, rename, sort, agenda, archive
Global: --preview  --report  --lock-dir  undo
"""

import argparse
import datetime
import json
import os
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

LOCK_SENTINEL = ".lock"
UNDO_LOG = Path.home() / ".organizer_undo.json"
REPORT_FILE = "organizer_report.txt"

SCREENSHOT_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"}
DOC_EXTS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".md", ".txt", ".csv", ".json"}
ARCHIVE_EXTS = SCREENSHOT_EXTS | DOC_EXTS | {".zip", ".rar", ".7z", ".tar", ".gz"}


@dataclass
class FileRecord:
    path: str
    name: str
    ext: str
    size: int
    mtime: float
    category: str = ""
    project: str = ""

    def mtime_dt(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self.mtime)

    def mtime_date_str(self) -> str:
        return self.mtime_dt().strftime("%Y-%m-%d")


@dataclass
class UndoEntry:
    timestamp: str
    operations: List[Dict] = field(default_factory=list)


def _cat(ext: str) -> str:
    ext = ext.lower()
    if ext in SCREENSHOT_EXTS:
        return "screenshot"
    if ext in DOC_EXTS:
        return "document"
    return "other"


def _is_locked(p: Path, extra_locks: List[str]) -> bool:
    if (p / LOCK_SENTINEL).exists():
        return True
    resolved = str(p.resolve()).lower()
    for ld in extra_locks:
        if resolved == Path(ld).resolve().__str__().lower():
            return True
    return False


def _load_undo_log() -> List[dict]:
    if UNDO_LOG.exists():
        return json.loads(UNDO_LOG.read_text(encoding="utf-8"))
    return []


def _save_undo_log(log: List[dict]) -> None:
    UNDO_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_undo(ops: List[Dict]) -> None:
    log = _load_undo_log()
    entry = UndoEntry(
        timestamp=datetime.datetime.now().isoformat(),
        operations=ops,
    )
    log.append(asdict(entry))
    if len(log) > 20:
        log = log[-20:]
    _save_undo_log(log)


def _report_line(lines: List[str], msg: str) -> None:
    lines.append(msg)
    print(msg)


def _write_report(lines: List[str], report_path: Optional[str] = None) -> None:
    dst = Path(report_path) if report_path else Path(REPORT_FILE)
    dst.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n[报告] 已写入 {dst}")


# ──────────────────────────── scan ────────────────────────────

def cmd_scan(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    records: List[FileRecord] = []
    lock_dirs: List[str] = []
    report: List[str] = []
    _report_line(report, f"===== 扫描报告  {datetime.datetime.now():%Y-%m-%d %H:%M} =====")
    _report_line(report, f"目标目录: {folder}")

    for root, dirs, files in os.walk(folder):
        rp = Path(root)
        if _is_locked(rp, args.lock_dir or []):
            lock_dirs.append(str(rp))
            dirs.clear()
            continue
        for f in files:
            fp = rp / f
            if fp.name == LOCK_SENTINEL:
                continue
            st = fp.stat()
            rec = FileRecord(
                path=str(fp), name=f, ext=fp.suffix,
                size=st.st_size, mtime=st.st_mtime,
                category=_cat(fp.suffix),
            )
            records.append(rec)

    _report_line(report, f"文件总数: {len(records)}")
    cats: Dict[str, int] = {}
    for r in records:
        cats[r.category] = cats.get(r.category, 0) + 1
    for c, n in sorted(cats.items()):
        _report_line(report, f"  {c}: {n} 个")

    exts: Dict[str, int] = {}
    for r in records:
        exts[r.ext] = exts.get(r.ext, 0) + 1
    _report_line(report, "扩展名分布:")
    for e, n in sorted(exts.items(), key=lambda x: -x[1])[:15]:
        _report_line(report, f"  {e or '<无>'}: {n}")

    if lock_dirs:
        _report_line(report, f"跳过锁定目录 ({len(lock_dirs)}):")
        for d in lock_dirs:
            _report_line(report, f"  [锁定] {d}")

    size_total = sum(r.size for r in records)
    _report_line(report, f"总大小: {size_total / 1024 / 1024:.2f} MB")

    if args.json:
        out = Path(args.json)
        out.write_text(json.dumps([asdict(r) for r in records], ensure_ascii=False, indent=2), encoding="utf-8")
        _report_line(report, f"详细数据已导出: {out}")

    if args.report is not False:
        _write_report(report, args.report if isinstance(args.report, str) else None)


# ──────────────────────────── rename ────────────────────────────

def cmd_rename(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    keyword = args.keyword or ""
    date_prefix = not args.no_date
    pattern = args.pattern or ""
    report: List[str] = []
    ops: List[Dict] = []
    _report_line(report, f"===== 重命名报告  {datetime.datetime.now():%Y-%m-%d %H:%M} =====")

    count = 0
    for root, dirs, files in os.walk(folder):
        rp = Path(root)
        if _is_locked(rp, args.lock_dir or []):
            dirs.clear(); continue
        for f in files:
            fp = rp / f
            if fp.name == LOCK_SENTINEL:
                continue
            if keyword and keyword.lower() not in f.lower():
                continue

            st = fp.stat()
            dt_str = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
            stem = fp.stem
            ext = fp.suffix

            if pattern:
                new_stem = pattern.replace("{date}", dt_str).replace("{name}", stem).replace("{keyword}", keyword)
            else:
                parts = []
                if date_prefix:
                    parts.append(dt_str)
                if keyword:
                    parts.append(keyword.strip())
                parts.append(stem)
                new_stem = "_".join(parts)

            new_name = new_stem + ext
            if new_name == f:
                continue

            new_path = rp / new_name
            counter = 1
            while new_path.exists():
                new_path = rp / f"{new_stem}_{counter}{ext}"
                counter += 1

            _report_line(report, f"  {f}  ->  {new_path.name}")
            ops.append({"op": "rename", "src": str(fp), "dst": str(new_path)})
            count += 1

            if not args.preview:
                fp.rename(new_path)

    if not args.preview and ops:
        _record_undo(ops)

    _report_line(report, f"重命名: {count} 个文件" + (" (预览)" if args.preview else ""))
    if args.report is not False:
        _write_report(report, args.report if isinstance(args.report, str) else None)


# ──────────────────────────── sort ────────────────────────────

def cmd_sort(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    project_root = Path(args.project_root).resolve() if args.project_root else folder
    report: List[str] = []
    ops: List[Dict] = []
    _report_line(report, f"===== 分流报告  {datetime.datetime.now():%Y-%m-%d %H:%M} =====")

    count_ss = 0
    count_doc = 0
    for root, dirs, files in os.walk(folder):
        rp = Path(root)
        if _is_locked(rp, args.lock_dir or []):
            dirs.clear(); continue
        for f in files:
            fp = rp / f
            if fp.name == LOCK_SENTINEL:
                continue

            cat = _cat(fp.suffix)
            if cat == "screenshot":
                dest_dir = project_root / "screenshots"
                count_ss += 1
            elif cat == "document":
                dest_dir = project_root / "docs"
                count_doc += 1
            else:
                continue

            if not args.preview:
                dest_dir.mkdir(parents=True, exist_ok=True)

            dest = dest_dir / f
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{fp.stem}_{counter}{fp.suffix}"
                counter += 1

            _report_line(report, f"  [{cat}] {fp}  ->  {dest}")
            ops.append({"op": "move", "src": str(fp), "dst": str(dest)})

            if not args.preview:
                shutil.move(str(fp), str(dest))

    if not args.preview and ops:
        _record_undo(ops)

    _report_line(report, f"截图分流: {count_ss}  文档分流: {count_doc}" + (" (预览)" if args.preview else ""))
    if args.report is not False:
        _write_report(report, args.report if isinstance(args.report, str) else None)


# ──────────────────────────── agenda ────────────────────────────

def cmd_agenda(args) -> None:
    todo_file = Path(args.todo_file).resolve()
    if not todo_file.is_file():
        print(f"[错误] {todo_file} 不存在"); return

    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    weekday_cn = ["一", "二", "三", "四", "五", "六", "日"][today.weekday()]

    lines = todo_file.read_text(encoding="utf-8").splitlines()
    report: List[str] = []
    _report_line(report, f"===== 今日清单  {today_str} 星期{weekday_cn} =====")
    _report_line(report, "")

    done_items: List[str] = []
    todo_items: List[str] = []
    due_items: List[str] = []

    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        is_done = stripped.lower().startswith(("x ", "✓ ", "✔ ", "[x] ", "[done]"))
        m = date_re.search(stripped)
        if m:
            task_date = m.group(1)
            if is_done:
                done_items.append(stripped)
            elif task_date < today_str:
                due_items.append(f"[过期] {stripped}")
            elif task_date == today_str:
                todo_items.append(f"[今日] {stripped}")
            else:
                todo_items.append(f"[计划] {stripped}")
        else:
            if is_done:
                done_items.append(stripped)
            else:
                todo_items.append(f"[待办] {stripped}")

    if due_items:
        _report_line(report, "⚠ 过期事项:")
        for i, item in enumerate(due_items, 1):
            _report_line(report, f"  {i}. {item}")
        _report_line(report, "")

    _report_line(report, "📋 待办事项:")
    for i, item in enumerate(todo_items, 1):
        _report_line(report, f"  {i}. {item}")

    if done_items:
        _report_line(report, "")
        _report_line(report, "✅ 已完成:")
        for item in done_items:
            _report_line(report, f"  · {item}")

    _report_line(report, "")
    _report_line(report, f"统计: 待办 {len(todo_items)}  过期 {len(due_items)}  完成 {len(done_items)}")

    if args.report is not False:
        _write_report(report, args.report if isinstance(args.report, str) else None)


# ──────────────────────────── archive ────────────────────────────

def cmd_archive(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    days = args.days
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    report: List[str] = []
    ops: List[Dict] = []
    _report_line(report, f"===== 归档报告  {datetime.datetime.now():%Y-%m-%d %H:%M} =====")
    _report_line(report, f"过期阈值: {days} 天 (早于 {cutoff:%Y-%m-%d})")

    expired: List[Path] = []
    project_dirs: List[Path] = []

    for entry in sorted(folder.iterdir()):
        if _is_locked(entry, args.lock_dir or []):
            _report_line(report, f"  [跳过/锁定] {entry.name}")
            continue
        if entry.is_file():
            mtime = datetime.datetime.fromtimestamp(entry.stat().st_mtime)
            if mtime < cutoff:
                expired.append(entry)
        elif entry.is_dir():
            project_dirs.append(entry)

    if expired:
        _report_line(report, f"\n过期文件 ({len(expired)}):")
        for f in expired:
            mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
            _report_line(report, f"  {mtime:%Y-%m-%d}  {f.name}")

    archive_dir = folder / "_archive"
    if not args.preview:
        archive_dir.mkdir(exist_ok=True)

    if args.zip and project_dirs:
        _report_line(report, f"\n打包归档项目目录:")
        for pd in project_dirs:
            zip_name = f"{pd.name}_{datetime.datetime.now():%Y%m%d}.zip"
            zip_path = archive_dir / zip_name
            _report_line(report, f"  {pd.name}  ->  {zip_path}")
            ops.append({"op": "archive_zip", "src": str(pd), "dst": str(zip_path)})
            if not args.preview:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(pd):
                        for f in files:
                            fp = Path(root) / f
                            arcname = str(fp.relative_to(pd.parent))
                            zf.write(fp, arcname)

    if expired:
        _report_line(report, f"\n移动过期文件到 _archive/:")
        for f in expired:
            dest = archive_dir / f.name
            counter = 1
            while dest.exists():
                dest = archive_dir / f"{f.stem}_{counter}{f.suffix}"
                counter += 1
            _report_line(report, f"  {f.name}  ->  {dest}")
            ops.append({"op": "move", "src": str(f), "dst": str(dest)})
            if not args.preview:
                shutil.move(str(f), str(dest))

    if not args.preview and ops:
        _record_undo(ops)

    _report_line(report, f"\n归档完成: 过期文件 {len(expired)}  项目归档 {len(project_dirs)}" + (" (预览)" if args.preview else ""))
    if args.report is not False:
        _write_report(report, args.report if isinstance(args.report, str) else None)


# ──────────────────────────── undo ────────────────────────────

def cmd_undo(args) -> None:
    log = _load_undo_log()
    if not log:
        print("[信息] 没有可撤销的记录"); return

    entry = log[-1]
    ts = entry.get("timestamp", "?")
    ops = entry.get("operations", [])
    print(f"撤销时间点: {ts}  操作数: {len(ops)}")

    for op in reversed(ops):
        kind = op.get("op")
        src = op.get("src", "")
        dst = op.get("dst", "")

        if kind == "rename":
            if Path(dst).exists():
                Path(dst).rename(src)
                print(f"  还原重命名: {dst} -> {src}")
            else:
                print(f"  [跳过] 目标不存在: {dst}")

        elif kind == "move":
            if Path(dst).exists():
                src_dir = Path(src).parent
                src_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(dst, src)
                print(f"  还原移动: {dst} -> {src}")
            else:
                print(f"  [跳过] 目标不存在: {dst}")

        elif kind == "archive_zip":
            zp = Path(dst)
            if zp.exists():
                zp.unlink()
                print(f"  删除归档: {zp}")
            src_p = Path(src)
            if src_p.exists():
                print(f"  保留原目录: {src_p}")
            else:
                print(f"  [警告] 原目录已不存在: {src}")

    log.pop()
    _save_undo_log(log)
    print("撤销完成。")


# ──────────────────────────── CLI ────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="organizer",
        description="个人效率工具：批量整理本地文件与日程素材",
    )

    sub = p.add_subparsers(dest="command", help="可用任务")

    # scan
    s_scan = sub.add_parser("scan", help="扫描指定文件夹，收集文件元数据")
    s_scan.add_argument("folder", help="要扫描的文件夹路径")
    s_scan.add_argument("--json", metavar="FILE", help="将扫描结果导出为 JSON")
    s_scan.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录 (可多次指定)")
    s_scan.add_argument("--report", nargs="?", const=True, default=True, help="生成报告 (可指定路径)")

    # rename
    s_rename = sub.add_parser("rename", help="按日期和关键词重命名文件")
    s_rename.add_argument("folder", help="目标文件夹路径")
    s_rename.add_argument("--keyword", help="只处理文件名包含该关键词的文件")
    s_rename.add_argument("--pattern", help="重命名模板: {date} {name} {keyword}")
    s_rename.add_argument("--no-date", action="store_true", help="不在文件名前添加日期前缀")
    s_rename.add_argument("--preview", action="store_true", help="预览模式，不实际执行")
    s_rename.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录")
    s_rename.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # sort
    s_sort = sub.add_parser("sort", help="截图与文档分流到项目目录")
    s_sort.add_argument("folder", help="源文件夹路径")
    s_sort.add_argument("--project-root", help="项目根目录 (默认为源文件夹)")
    s_sort.add_argument("--preview", action="store_true", help="预览模式")
    s_sort.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录")
    s_sort.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # agenda
    s_agenda = sub.add_parser("agenda", help="读取待办文本生成今日清单")
    s_agenda.add_argument("todo_file", help="待办文本文件路径 (todo.txt)")
    s_agenda.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # archive
    s_archive = sub.add_parser("archive", help="识别过期文件并打包归档完成项目")
    s_archive.add_argument("folder", help="目标文件夹路径")
    s_archive.add_argument("--days", type=int, default=30, help="过期天数阈值 (默认 30)")
    s_archive.add_argument("--zip", action="store_true", help="将项目子目录打包为 zip")
    s_archive.add_argument("--preview", action="store_true", help="预览模式")
    s_archive.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录")
    s_archive.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # undo
    s_undo = sub.add_parser("undo", help="撤销最近一次整理操作")

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    dispatch = {
        "scan": cmd_scan,
        "rename": cmd_rename,
        "sort": cmd_sort,
        "agenda": cmd_agenda,
        "archive": cmd_archive,
        "undo": cmd_undo,
    }
    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
