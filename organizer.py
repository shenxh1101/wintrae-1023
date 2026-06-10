#!/usr/bin/env python3
"""
Personal Efficiency Organizer — Workspace Steward
Batch-organize local files and schedule materials for knowledge workers.

Tasks: scan, rename, sort, agenda, archive
Global: --preview  --report  --lock-dir  undo
"""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

LOCK_SENTINEL = ".lock"
UNDO_LOG = Path.home() / ".organizer_undo.json"
SCHEMES_FILE = Path.home() / ".organizer_schemes.json"
REPORT_FILE = "organizer_report.txt"
MARKER_DONE = {".done", ".completed", "DONE.md", "COMPLETED.md", "done.txt", "completed.txt"}
SORT_DIR_NAMES = {"screenshots", "docs", "_archive"}

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
    md5: str = ""

    def mtime_dt(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(self.mtime)

    def mtime_date_str(self) -> str:
        return self.mtime_dt().strftime("%Y-%m-%d")


@dataclass
class UndoEntry:
    timestamp: str
    task: str
    scheme: str = ""
    summary: str = ""
    operations: List[Dict] = field(default_factory=list)


@dataclass
class ProjectSummary:
    name: str
    path: str
    screenshots: int = 0
    docs: int = 0
    other: int = 0
    expired: int = 0
    total_size: int = 0
    completed: bool = False
    locked: bool = False


# ──────────────────────────── helpers ────────────────────────────

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
        try:
            if resolved == str(Path(ld).resolve()).lower():
                return True
        except Exception:
            pass
    return False


def _is_inside_sort_targets(p: Path, project_root: Path) -> bool:
    try:
        rel = p.resolve().relative_to(project_root.resolve())
        first = rel.parts[0] if rel.parts else ""
        return first in SORT_DIR_NAMES
    except ValueError:
        return False


def _md5_of(path: Path, chunk: int = 65536) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _files_identical(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return _md5_of(a) == _md5_of(b)
    except Exception:
        return False


def _find_duplicate_in_dir(src: Path, dest_dir: Path) -> Optional[Path]:
    if not dest_dir.is_dir():
        return None
    for candidate in dest_dir.iterdir():
        if candidate.is_file() and candidate.name == src.name:
            if _files_identical(src, candidate):
                return candidate
    return None


def _project_is_completed(p: Path) -> Tuple[bool, str]:
    for marker in MARKER_DONE:
        if (p / marker).exists():
            return True, f"检测到完成标记: {marker}"
    for todo_name in ("todo.txt", "TODO.md", "todo.md"):
        todo = p / todo_name
        if todo.exists():
            try:
                text = todo.read_text(encoding="utf-8", errors="ignore")
                lines = [l for l in text.splitlines() if l.strip()]
                if not lines:
                    continue
                done_lines = [
                    l for l in lines
                    if l.strip().lower().startswith(("x ", "✓ ", "✔ ", "[x] ", "[done]", "- [x]"))
                ]
                pending_lines = [
                    l for l in lines
                    if l.strip().startswith("-") or l.strip().startswith("*") or l.strip().startswith("[")
                    and not l.strip().lower().startswith(("x ", "✓ ", "✔ ", "[x] ", "[done]", "- [x]"))
                ]
                if pending_lines and len(done_lines) >= len(pending_lines):
                    return True, f"清单完成度高 ({len(done_lines)}/{len(done_lines) + len(pending_lines)})"
            except Exception:
                pass
    return False, ""


# ──────────────────────────── undo / schemes log ────────────────────────────

def _load_undo_log() -> List[dict]:
    if UNDO_LOG.exists():
        return json.loads(UNDO_LOG.read_text(encoding="utf-8"))
    return []


def _save_undo_log(log: List[dict]) -> None:
    UNDO_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_undo(task: str, ops: List[Dict], scheme: str = "", summary: str = "") -> None:
    log = _load_undo_log()
    entry = UndoEntry(
        timestamp=datetime.datetime.now().isoformat(),
        task=task,
        scheme=scheme,
        summary=summary,
        operations=ops,
    )
    log.append(asdict(entry))
    if len(log) > 50:
        log = log[-50:]
    _save_undo_log(log)


def _load_schemes() -> Dict[str, dict]:
    if SCHEMES_FILE.exists():
        return json.loads(SCHEMES_FILE.read_text(encoding="utf-8"))
    return {}


def _save_schemes(schemes: Dict[str, dict]) -> None:
    SCHEMES_FILE.write_text(json.dumps(schemes, ensure_ascii=False, indent=2), encoding="utf-8")


# ──────────────────────────── reporting ────────────────────────────

def _report_line(lines: List[str], msg: str) -> None:
    lines.append(msg)
    print(msg)


def _write_report(lines: List[str], report_path: Optional[str] = None, markdown: bool = False) -> None:
    if markdown:
        dst = Path(report_path) if report_path else Path("organizer_report.md")
        md_lines = _convert_to_markdown(lines)
        dst.write_text("\n".join(md_lines), encoding="utf-8")
        print(f"\n[报告] 已写入 Markdown {dst}")
    else:
        dst = Path(report_path) if report_path else Path(REPORT_FILE)
        dst.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n[报告] 已写入 {dst}")


def _convert_to_markdown(lines: List[str]) -> List[str]:
    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("====="):
            title = stripped.strip("= ").strip()
            out.append(f"# {title}")
        elif stripped.startswith("⚠") or stripped.startswith("📋") or stripped.startswith("✅"):
            out.append(f"## {stripped}")
        elif re.match(r"^\d+\.\s", stripped):
            out.append(f"- {stripped}")
        elif stripped.startswith("  ·"):
            out.append(f"- {stripped[3:].strip()}")
        elif stripped.startswith("  [") or stripped.startswith("  ·"):
            out.append(f"- {stripped[2:].strip()}")
        elif stripped == "":
            out.append("")
        else:
            if ":" in stripped and not stripped.startswith(" "):
                out.append(f"**{stripped}**")
            else:
                out.append(stripped)
    return out


# ──────────────────────────── scan ────────────────────────────

def cmd_scan(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    records: List[FileRecord] = []
    lock_dirs: List[str] = []
    project_summaries: Dict[str, ProjectSummary] = {}
    report: List[str] = []
    _report_line(report, f"===== 扫描报告  {datetime.datetime.now():%Y-%m-%d %H:%M} =====")
    _report_line(report, f"目标目录: {folder}")

    days_expired = getattr(args, "expire_days", 30)
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days_expired)

    for root, dirs, files in os.walk(folder):
        rp = Path(root)
        if _is_locked(rp, args.lock_dir or []):
            lock_dirs.append(str(rp))
            dirs.clear()
            continue

        rel_to_base = rp.relative_to(folder) if rp != folder else Path("")
        first_level = rel_to_base.parts[0] if rel_to_base.parts else "(根目录)"

        if first_level not in project_summaries:
            proj_path = folder / first_level if first_level != "(根目录)" else folder
            completed, _ = _project_is_completed(proj_path)
            project_summaries[first_level] = ProjectSummary(
                name=first_level,
                path=str(proj_path),
                completed=completed,
                locked=_is_locked(proj_path, args.lock_dir or []),
            )
        ps = project_summaries[first_level]

        for f in files:
            fp = rp / f
            if fp.name == LOCK_SENTINEL:
                continue
            st = fp.stat()
            cat = _cat(fp.suffix)
            is_expired = datetime.datetime.fromtimestamp(st.st_mtime) < cutoff

            rec = FileRecord(
                path=str(fp), name=f, ext=fp.suffix,
                size=st.st_size, mtime=st.st_mtime,
                category=cat,
                project=first_level,
            )
            records.append(rec)

            ps.total_size += st.st_size
            if cat == "screenshot":
                ps.screenshots += 1
            elif cat == "document":
                ps.docs += 1
            else:
                ps.other += 1
            if is_expired:
                ps.expired += 1

    _report_line(report, "")
    _report_line(report, f"文件总数: {len(records)}")
    cats: Dict[str, int] = {}
    for r in records:
        cats[r.category] = cats.get(r.category, 0) + 1
    for c, n in sorted(cats.items()):
        _report_line(report, f"  {c}: {n} 个")

    _report_line(report, "")
    _report_line(report, f"按项目目录汇总 (过期阈值 {days_expired} 天):")
    _report_line(report, f"  {'项目':<20} {'截图':>4} {'文档':>4} {'其他':>4} {'过期':>4} {'大小':>10} 状态")
    _report_line(report, f"  {'-' * 20} {'-' * 4} {'-' * 4} {'-' * 4} {'-' * 4} {'-' * 10} {'-' * 8}")
    for name, ps in sorted(project_summaries.items()):
        status_parts = []
        if ps.locked:
            status_parts.append("锁定")
        if ps.completed:
            status_parts.append("可归档")
        status = " ".join(status_parts) if status_parts else "进行中"
        size_mb = ps.total_size / 1024 / 1024
        _report_line(
            report,
            f"  {name:<20} {ps.screenshots:>4} {ps.docs:>4} {ps.other:>4} {ps.expired:>4} {size_mb:>9.2f}MB {status}",
        )

    total_expired = sum(ps.expired for ps in project_summaries.values())
    total_completed = sum(1 for ps in project_summaries.values() if ps.completed)
    total_locked = sum(1 for ps in project_summaries.values() if ps.locked)
    _report_line(report, "")
    _report_line(report, f"待处理: 过期文件 {total_expired}  可归档项目 {total_completed}  锁定项目 {total_locked}")

    if lock_dirs:
        _report_line(report, "")
        _report_line(report, f"跳过锁定目录 ({len(lock_dirs)}):")
        for d in lock_dirs:
            _report_line(report, f"  [锁定] {d}")

    size_total = sum(r.size for r in records)
    _report_line(report, f"总大小: {size_total / 1024 / 1024:.2f} MB")

    if args.json:
        out = Path(args.json)
        data = {
            "records": [asdict(r) for r in records],
            "projects": {k: asdict(v) for k, v in project_summaries.items()},
        }
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        _report_line(report, f"详细数据已导出: {out}")

    if args.report is not False:
        md = getattr(args, "markdown", False)
        _write_report(report, args.report if isinstance(args.report, str) else None, markdown=md)


# ──────────────────────────── rename ────────────────────────────

def _apply_rename_scheme(fp: Path, scheme: dict) -> Optional[Tuple[str, str]]:
    keyword = scheme.get("keyword", "") or ""
    date_prefix = not scheme.get("no_date", False)
    pattern = scheme.get("pattern", "") or ""

    if keyword and keyword.lower() not in fp.name.lower():
        return None

    st = fp.stat()
    dt_str = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d")
    stem = fp.stem
    ext = fp.suffix

    if pattern:
        new_stem = (
            pattern
            .replace("{date}", dt_str)
            .replace("{name}", stem)
            .replace("{keyword}", keyword)
        )
    else:
        parts = []
        if date_prefix:
            parts.append(dt_str)
        if keyword:
            parts.append(keyword.strip())
        parts.append(stem)
        new_stem = "_".join(parts)

    new_name = new_stem + ext
    if new_name == fp.name:
        return None
    return new_name, new_stem


def cmd_rename(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    schemes = _load_schemes()

    if args.list_schemes:
        if not schemes:
            print("暂无已保存的命名方案。")
        else:
            print("已保存的命名方案:")
            for name, s in schemes.items():
                parts = []
                if s.get("keyword"):
                    parts.append(f"关键词={s['keyword']}")
                if s.get("pattern"):
                    parts.append(f"模板={s['pattern']}")
                if s.get("no_date"):
                    parts.append("无日期前缀")
                print(f"  {name}: {'  '.join(parts)}")
        return

    scheme: dict = {}
    if args.load_scheme:
        if args.load_scheme not in schemes:
            print(f"[错误] 方案 '{args.load_scheme}' 不存在，可用: {', '.join(schemes.keys())}")
            return
        scheme = dict(schemes[args.load_scheme])
        scheme_name = args.load_scheme
    else:
        scheme = {
            "keyword": args.keyword or "",
            "pattern": args.pattern or "",
            "no_date": args.no_date,
        }
        scheme_name = ""

    if args.save_scheme:
        schemes[args.save_scheme] = scheme
        _save_schemes(schemes)
        print(f"[方案] 已保存命名方案 '{args.save_scheme}'")
        scheme_name = args.save_scheme

    report: List[str] = []
    ops: List[Dict] = []
    _report_line(report, f"===== 重命名报告  {datetime.datetime.now():%Y-%m-%d %H:%M} =====")
    if scheme_name:
        _report_line(report, f"使用命名方案: {scheme_name}")
        _report_line(report, f"  关键词: {scheme.get('keyword') or '(无)'}")
        _report_line(report, f"  模板: {scheme.get('pattern') or '(默认 日期_关键词_原名)'}")

    preview_candidates: List[Tuple[Path, str]] = []
    conflicts: List[Tuple[Path, str, str]] = []

    for root, dirs, files in os.walk(folder):
        rp = Path(root)
        if _is_locked(rp, args.lock_dir or []):
            dirs.clear(); continue
        if _is_inside_sort_targets(rp, folder):
            dirs.clear(); continue
        for f in files:
            fp = rp / f
            if fp.name == LOCK_SENTINEL:
                continue
            result = _apply_rename_scheme(fp, scheme)
            if result is None:
                continue
            new_name, new_stem = result
            preview_candidates.append((fp, new_name))

    planned_targets: Dict[str, List[Path]] = {}
    for fp, new_name in preview_candidates:
        key = str(fp.parent / new_name).lower()
        planned_targets.setdefault(key, []).append(fp)

    for fp, new_name in preview_candidates:
        key = str(fp.parent / new_name).lower()
        if len(planned_targets[key]) > 1:
            conflicts.append((fp, new_name, "与其他文件重名冲突"))
        elif (fp.parent / new_name).exists() and (fp.parent / new_name).resolve() != fp.resolve():
            conflicts.append((fp, new_name, "目标文件已存在"))

    if conflicts:
        _report_line(report, "")
        _report_line(report, f"⚠ 检测到 {len(conflicts)} 处命名冲突 (将自动加后缀):")
        for fp, new_name, reason in conflicts[:20]:
            _report_line(report, f"  {fp.name} -> {new_name}  ({reason})")
        if len(conflicts) > 20:
            _report_line(report, f"  ... 其余 {len(conflicts) - 20} 处省略")

    _report_line(report, "")
    _report_line(report, f"计划重命名 ({len(preview_candidates)} 个):")
    for fp, new_name in preview_candidates[:50]:
        _report_line(report, f"  {fp.name}  ->  {new_name}")
    if len(preview_candidates) > 50:
        _report_line(report, f"  ... 其余 {len(preview_candidates) - 50} 个省略")

    if not preview_candidates:
        _report_line(report, "  (没有需要重命名的文件)")

    if args.preview:
        _report_line(report, f"\n重命名: 0 个文件 (预览模式，未执行)")
        if args.report is not False:
            _write_report(report, args.report if isinstance(args.report, str) else None)
        return

    if args.confirm and preview_candidates:
        try:
            answer = input(f"\n确认对以上 {len(preview_candidates)} 个文件执行重命名? [y/N]: ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            _report_line(report, "\n用户取消，未执行任何操作。")
            if args.report is not False:
                _write_report(report, args.report if isinstance(args.report, str) else None)
            return

    count = 0
    for fp, new_name in preview_candidates:
        new_path = fp.parent / new_name
        counter = 1
        while new_path.exists() and new_path.resolve() != fp.resolve():
            new_path = fp.parent / f"{new_path.stem.rsplit('_', 1)[0] if '_' in new_path.stem else new_path.stem}_{counter}{fp.suffix}"
            counter += 1
        if new_path.resolve() == fp.resolve():
            continue
        if new_path.name != new_name:
            _report_line(report, f"  {fp.name}  ->  {new_name}  (冲突，已改为 {new_path.name})")
        ops.append({"op": "rename", "src": str(fp), "dst": str(new_path)})
        count += 1
        fp.rename(new_path)

    if ops:
        summary = f"重命名 {count} 个文件"
        _record_undo("rename", ops, scheme=scheme_name, summary=summary)

    _report_line(report, f"\n重命名: {count} 个文件")
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
    _report_line(report, f"源目录: {folder}")
    _report_line(report, f"项目根: {project_root}")

    screenshot_dir = project_root / "screenshots"
    docs_dir = project_root / "docs"

    count_ss = 0
    count_doc = 0
    count_skip_duplicate = 0
    count_skip_already = 0

    for root, dirs, files in os.walk(folder):
        rp = Path(root)
        if _is_locked(rp, args.lock_dir or []):
            dirs.clear(); continue
        if _is_inside_sort_targets(rp, project_root):
            dirs.clear(); continue
        for f in files:
            fp = rp / f
            if fp.name == LOCK_SENTINEL:
                continue

            cat = _cat(fp.suffix)
            if cat == "screenshot":
                dest_dir = screenshot_dir
            elif cat == "document":
                dest_dir = docs_dir
            else:
                continue

            if not args.preview:
                dest_dir.mkdir(parents=True, exist_ok=True)

            try:
                if fp.resolve().parent == dest_dir.resolve():
                    count_skip_already += 1
                    continue
            except Exception:
                pass

            dup = _find_duplicate_in_dir(fp, dest_dir)
            if dup is not None:
                count_skip_duplicate += 1
                _report_line(report, f"  [跳过/相同] {fp} 已在 {dup}")
                continue

            dest = dest_dir / f
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{fp.stem}_{counter}{fp.suffix}"
                counter += 1

            _report_line(report, f"  [{cat}] {fp}  ->  {dest}")
            ops.append({"op": "move", "src": str(fp), "dst": str(dest)})
            if cat == "screenshot":
                count_ss += 1
            else:
                count_doc += 1

            if not args.preview:
                shutil.move(str(fp), str(dest))

    if not args.preview and ops:
        summary = f"分流: 截图 {count_ss} 文档 {count_doc}"
        _record_undo("sort", ops, summary=summary)

    _report_line(report, "")
    _report_line(report, f"截图分流: {count_ss}  文档分流: {count_doc}" + (" (预览)" if args.preview else ""))
    if count_skip_duplicate:
        _report_line(report, f"跳过重复文件: {count_skip_duplicate}")
    if count_skip_already:
        _report_line(report, f"跳过已在目标目录: {count_skip_already}")
    if not ops and not count_skip_duplicate and not count_skip_already:
        _report_line(report, "  (没有需要分流的文件，工作区已整洁)")
    if args.report is not False:
        _write_report(report, args.report if isinstance(args.report, str) else None)


# ──────────────────────────── agenda ────────────────────────────

def _extract_keywords(text: str) -> List[str]:
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
    text = re.sub(r"\d{4}", "", text)
    kws = re.findall(r"[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_]{1,}", text)
    stop = {"完成", "今天", "今日", "计划", "过期", "任务", "事项", "待办", "收尾", "草稿", "周报", "归档", "整理",
            "done", "todo", "the", "and", "for", "with", "from", "this", "that", "not", "to", "of", "a", "is", "in"}
    return [k for k in kws if k.lower() not in stop and len(k) >= 2]


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
    project_groups: Dict[str, List[str]] = {}

    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        is_done = stripped.lower().startswith(("x ", "✓ ", "✔ ", "[x] ", "[done]", "- [x]"))
        clean = re.sub(r"^(x\s+|✓\s+|✔\s+|\[x\]\s+|\[done\]\s*|-\s*\[x\]\s+)", "", stripped, flags=re.IGNORECASE)

        keywords = _extract_keywords(clean)
        group = keywords[0] if keywords else "未分类"

        m = date_re.search(stripped)
        if m:
            task_date = m.group(1)
            if is_done:
                done_items.append(stripped)
                project_groups.setdefault(group, []).append(f"[已完成] {clean}")
            elif task_date < today_str:
                due_items.append(f"[过期] {stripped}")
                project_groups.setdefault(group, []).append(f"[过期] {clean}")
            elif task_date == today_str:
                todo_items.append(f"[今日] {stripped}")
                project_groups.setdefault(group, []).append(f"[今日] {clean}")
            else:
                todo_items.append(f"[计划] {stripped}")
                project_groups.setdefault(group, []).append(f"[计划] {clean}")
        else:
            if is_done:
                done_items.append(stripped)
                project_groups.setdefault(group, []).append(f"[已完成] {clean}")
            else:
                todo_items.append(f"[待办] {stripped}")
                project_groups.setdefault(group, []).append(f"[待办] {clean}")

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

    if args.project_dir:
        _report_line(report, "")
        _report_line(report, "── 按项目关键词分组 + 关联素材 ──")
        proj_root = Path(args.project_dir).resolve()
        for group, items in sorted(project_groups.items()):
            _report_line(report, f"\n## 项目: {group}")
            for item in items:
                _report_line(report, f"  · {item}")

            related_files: List[str] = []
            if proj_root.is_dir():
                for root, _, files in os.walk(proj_root):
                    rp = Path(root)
                    if _is_locked(rp, args.lock_dir or []):
                        continue
                    for f in files:
                        if f == LOCK_SENTINEL:
                            continue
                        if group.lower() in f.lower():
                            related_files.append(str(rp / f))
            if related_files:
                _report_line(report, f"  📎 相关素材 ({len(related_files)}):")
                for rf in related_files[:10]:
                    _report_line(report, f"      {rf}")
                if len(related_files) > 10:
                    _report_line(report, f"      ... 其余 {len(related_files) - 10} 个省略")

    if args.report is not False:
        md = getattr(args, "markdown", False)
        _write_report(report, args.report if isinstance(args.report, str) else None, markdown=md)


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
    if args.completed_only:
        _report_line(report, "归档模式: 仅确认完成的项目")

    expired: List[Path] = []
    project_dirs: List[Tuple[Path, bool, str]] = []

    for entry in sorted(folder.iterdir()):
        if entry.name.startswith("_"):
            continue
        if _is_locked(entry, args.lock_dir or []):
            _report_line(report, f"  [跳过/锁定] {entry.name}")
            continue
        if entry.is_file():
            mtime = datetime.datetime.fromtimestamp(entry.stat().st_mtime)
            if mtime < cutoff:
                expired.append(entry)
        elif entry.is_dir():
            completed, reason = _project_is_completed(entry)
            project_dirs.append((entry, completed, reason))

    if expired:
        _report_line(report, f"\n过期文件 ({len(expired)}):")
        for f in expired:
            mtime = datetime.datetime.fromtimestamp(f.stat().st_mtime)
            _report_line(report, f"  {mtime:%Y-%m-%d}  {f.name}")

    archive_dir = folder / "_archive"
    if not args.preview:
        archive_dir.mkdir(exist_ok=True)

    to_archive = [(p, c, r) for p, c, r in project_dirs if (not args.completed_only) or c]
    skipped_incomplete = [(p, r) for p, c, r in project_dirs if args.completed_only and not c]

    if skipped_incomplete:
        _report_line(report, f"\n跳过未完成项目 ({len(skipped_incomplete)}):")
        for p, _r in skipped_incomplete:
            _report_line(report, f"  [进行中] {p.name}")

    if args.zip and to_archive:
        _report_line(report, f"\n打包归档项目目录:")
        for pd, completed, reason in to_archive:
            zip_name = f"{pd.name}_{datetime.datetime.now():%Y%m%d}.zip"
            zip_path = archive_dir / zip_name
            if zip_path.exists():
                _report_line(report, f"  [跳过/已归档] {zip_name} 已存在")
                continue
            note = f" (完成: {reason})" if completed and reason else ""
            _report_line(report, f"  {pd.name}  ->  {zip_path}{note}")
            ops.append({"op": "archive_zip", "src": str(pd), "dst": str(zip_path)})
            if not args.preview:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for root, _, files in os.walk(pd):
                        for f in files:
                            fp = Path(root) / f
                            try:
                                arcname = str(fp.relative_to(pd.parent))
                                zf.write(fp, arcname)
                            except Exception:
                                pass

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
        summary = f"归档: 过期文件 {len(expired)} 项目 {len(to_archive)}"
        _record_undo("archive", ops, summary=summary)

    _report_line(
        report,
        f"\n归档完成: 过期文件 {len(expired)}  项目归档 {len([o for o in ops if o.get('op') == 'archive_zip'])}"
        + (" (预览)" if args.preview else ""),
    )
    if args.report is not False:
        md = getattr(args, "markdown", False)
        _write_report(report, args.report if isinstance(args.report, str) else None, markdown=md)


# ──────────────────────────── undo ────────────────────────────

def cmd_undo(args) -> None:
    log = _load_undo_log()
    if not log:
        print("[信息] 没有可撤销的记录"); return

    if args.list:
        print("撤销历史 (最近 20 条):")
        for idx, entry in enumerate(reversed(log[-20:]), 1):
            ts = entry.get("timestamp", "?")[:19]
            task = entry.get("task", "?")
            scheme = entry.get("scheme", "")
            summary = entry.get("summary", "")
            n_ops = len(entry.get("operations", []))
            label = f"[{ts}] {task}"
            if scheme:
                label += f" 方案={scheme}"
            if summary:
                label += f" — {summary}"
            label += f" ({n_ops} 步)"
            print(f"  {len(log) - idx + 1}. {label}")
        return

    entry = log[-1]
    ts = entry.get("timestamp", "?")
    task = entry.get("task", "?")
    scheme = entry.get("scheme", "")
    summary = entry.get("summary", "")
    ops = entry.get("operations", [])

    label = f"撤销: [{task}]"
    if scheme:
        label += f" 命名方案 '{scheme}'"
    if summary:
        label += f" — {summary}"
    print(f"{label}  ({len(ops)} 步, 时间 {ts})")

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
        description="个人效率工具：批量整理本地文件与日程素材（工作区管家版）",
    )

    sub = p.add_subparsers(dest="command", help="可用任务")

    # scan
    s_scan = sub.add_parser("scan", help="扫描指定文件夹，收集文件元数据")
    s_scan.add_argument("folder", help="要扫描的文件夹路径")
    s_scan.add_argument("--json", metavar="FILE", help="将扫描结果导出为 JSON")
    s_scan.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录 (可多次指定)")
    s_scan.add_argument("--expire-days", type=int, default=30, help="过期判定阈值天数 (默认 30)")
    s_scan.add_argument("--markdown", action="store_true", help="报告导出为 Markdown")
    s_scan.add_argument("--report", nargs="?", const=True, default=True, help="生成报告 (可指定路径)")

    # rename
    s_rename = sub.add_parser("rename", help="按日期和关键词重命名文件")
    s_rename.add_argument("folder", help="目标文件夹路径")
    s_rename.add_argument("--keyword", help="只处理文件名包含该关键词的文件")
    s_rename.add_argument("--pattern", help="重命名模板: {date} {name} {keyword}")
    s_rename.add_argument("--no-date", action="store_true", help="不在文件名前添加日期前缀")
    s_rename.add_argument("--preview", action="store_true", help="预览模式，不实际执行")
    s_rename.add_argument("--confirm", action="store_true", help="执行前交互式确认")
    s_rename.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录")
    s_rename.add_argument("--save-scheme", metavar="NAME", help="保存当前命名方案")
    s_rename.add_argument("--load-scheme", metavar="NAME", help="加载已保存的命名方案")
    s_rename.add_argument("--list-schemes", action="store_true", help="列出所有已保存方案")
    s_rename.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # sort
    s_sort = sub.add_parser("sort", help="截图与文档分流到项目目录 (幂等：重复执行安全)")
    s_sort.add_argument("folder", help="源文件夹路径")
    s_sort.add_argument("--project-root", help="项目根目录 (默认为源文件夹)")
    s_sort.add_argument("--preview", action="store_true", help="预览模式")
    s_sort.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录")
    s_sort.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # agenda
    s_agenda = sub.add_parser("agenda", help="读取待办文本生成今日清单，支持按项目分组")
    s_agenda.add_argument("todo_file", help="待办文本文件路径 (todo.txt)")
    s_agenda.add_argument("--project-dir", help="扫描项目素材目录，自动关联待办")
    s_agenda.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录")
    s_agenda.add_argument("--markdown", action="store_true", help="报告导出为 Markdown")
    s_agenda.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # archive
    s_archive = sub.add_parser("archive", help="识别过期文件并打包归档完成项目")
    s_archive.add_argument("folder", help="目标文件夹路径")
    s_archive.add_argument("--days", type=int, default=30, help="过期天数阈值 (默认 30)")
    s_archive.add_argument("--zip", action="store_true", help="将项目子目录打包为 zip")
    s_archive.add_argument("--completed-only", action="store_true", help="仅归档检测到完成标记的项目")
    s_archive.add_argument("--preview", action="store_true", help="预览模式")
    s_archive.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录")
    s_archive.add_argument("--markdown", action="store_true", help="报告导出为 Markdown")
    s_archive.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # undo
    s_undo = sub.add_parser("undo", help="撤销最近一次整理操作")
    s_undo.add_argument("--list", action="store_true", help="列出可撤销的操作历史")

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
