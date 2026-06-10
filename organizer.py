#!/usr/bin/env python3
"""
Personal Efficiency Organizer — Workspace Steward
Batch-organize local files and schedule materials for knowledge workers.

Tasks: scan, rename, sort, agenda, archive, status, config
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
from typing import Any, Dict, List, Optional, Tuple

LOCK_SENTINEL = ".lock"
UNDO_LOG = Path.home() / ".organizer_undo.json"
SCHEMES_FILE = Path.home() / ".organizer_schemes.json"
ARCHIVE_INDEX = Path.home() / ".organizer_archive_index.json"
WORKSPACE_CONFIG = ".organizer.json"
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
    file_count: int = 0
    workspace: str = ""
    report_path: str = ""
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


def _line_is_done(line: str) -> bool:
    s = line.strip().lower()
    if not s:
        return False
    if re.match(r"^[-*]\s*\[[x\*]\]", s):
        return True
    if s.startswith(("x ", "✓ ", "✔ ", "[x] ", "[done] ", "[*] ")):
        return True
    return False


def _line_is_list_item(line: str) -> bool:
    s = line.strip().lower()
    if not s:
        return False
    if re.match(r"^[-*]\s*(\[|)", s):
        return True
    if s.startswith(("x ", "✓ ", "✔ ", "[", "[x] ", "[done] ")):
        return True
    return False


def _project_is_completed(p: Path) -> Tuple[bool, str]:
    for marker in MARKER_DONE:
        if (p / marker).exists():
            return True, f"检测到完成标记: {marker}"
    for todo_name in ("todo.txt", "TODO.md", "todo.md"):
        todo = p / todo_name
        if todo.exists():
            try:
                text = todo.read_text(encoding="utf-8-sig", errors="ignore")
                raw_lines = [l for l in text.splitlines() if l.strip()]
                if not raw_lines:
                    continue
                list_lines = [l for l in raw_lines if _line_is_list_item(l)]
                if not list_lines:
                    continue
                done_lines = [l for l in list_lines if _line_is_done(l)]
                pending_lines = [l for l in list_lines if not _line_is_done(l)]
                total = len(done_lines) + len(pending_lines)
                if len(done_lines) > 0 and len(pending_lines) == 0:
                    return True, f"清单全部完成 ({len(done_lines)}/{total})"
                if pending_lines and len(done_lines) >= len(pending_lines):
                    return True, f"清单完成度高 ({len(done_lines)}/{total})"
            except Exception:
                pass
    return False, ""


# ──────────────────────────── workspace config ────────────────────────────

def _find_workspace_root(folder: Path) -> Optional[Path]:
    current = folder.resolve()
    for _ in range(10):
        cfg = current / WORKSPACE_CONFIG
        if cfg.is_file():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _load_workspace_config(folder: Path) -> dict:
    ws_root = _find_workspace_root(folder)
    if ws_root is None:
        cfg_path = folder.resolve() / WORKSPACE_CONFIG
        if cfg_path.is_file():
            ws_root = folder.resolve()
    if ws_root is None:
        return {}
    cfg_path = ws_root / WORKSPACE_CONFIG
    try:
        return json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}


def _save_workspace_config(folder: Path, config: dict) -> None:
    cfg_path = folder.resolve() / WORKSPACE_CONFIG
    cfg_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def _apply_config(args, config: dict) -> None:
    if not config:
        return
    if not getattr(args, "lock_dir", None):
        args.lock_dir = config.get("lock_dirs", [])
    if getattr(args, "expire_days", None) is None and "expire_days" in config:
        args.expire_days = config["expire_days"]
    if getattr(args, "days", None) is None and "expire_days" in config:
        args.days = config["expire_days"]
    if not getattr(args, "report_path", None) and "report_path" in config:
        if isinstance(args.report, bool) and args.report:
            args.report = config["report_path"]
    if "markdown" in config and not getattr(args, "markdown", False):
        args.markdown = config["markdown"]
    if "project_keywords" in config:
        if not hasattr(args, "_config_keywords"):
            args._config_keywords = config["project_keywords"]
    if "aliases" in config:
        if not hasattr(args, "_config_aliases"):
            args._config_aliases = config["aliases"]


# ──────────────────────────── undo / schemes log ────────────────────────────

def _load_undo_log() -> List[dict]:
    if UNDO_LOG.exists():
        return json.loads(UNDO_LOG.read_text(encoding="utf-8"))
    return []


def _save_undo_log(log: List[dict]) -> None:
    UNDO_LOG.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def _record_undo(task: str, ops: List[Dict], scheme: str = "", summary: str = "",
                 file_count: int = 0, workspace: str = "", report_path: str = "") -> None:
    log = _load_undo_log()
    entry = UndoEntry(
        timestamp=datetime.datetime.now().isoformat(),
        task=task,
        scheme=scheme,
        summary=summary,
        file_count=file_count,
        workspace=workspace,
        report_path=report_path,
        operations=ops,
    )
    log.append(asdict(entry))
    if len(log) > 100:
        log = log[-100:]
    _save_undo_log(log)


def _load_schemes() -> Dict[str, dict]:
    if SCHEMES_FILE.exists():
        return json.loads(SCHEMES_FILE.read_text(encoding="utf-8"))
    return {}


def _save_schemes(schemes: Dict[str, dict]) -> None:
    SCHEMES_FILE.write_text(json.dumps(schemes, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_archive_index() -> List[dict]:
    if ARCHIVE_INDEX.exists():
        return json.loads(ARCHIVE_INDEX.read_text(encoding="utf-8-sig"))
    return []


def _save_archive_index(entries: List[dict]) -> None:
    ARCHIVE_INDEX.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def _add_archive_entry(project_name: str, workspace: str, zip_path: str,
                       completed_reason: str = "", expired_files: int = 0,
                       total_files: int = 0) -> None:
    entries = _load_archive_index()
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "project": project_name,
        "workspace": workspace,
        "zip_path": zip_path,
        "completed_reason": completed_reason,
        "expired_files": expired_files,
        "total_files": total_files,
    }
    entries.append(entry)
    if len(entries) > 500:
        entries = entries[-500:]
    _save_archive_index(entries)


# ──────────────────────────── reporting ────────────────────────────

def _report_line(lines: List[str], msg: str) -> None:
    lines.append(msg)
    print(msg)


def _write_report(lines: List[str], report_path: Optional[str] = None,
                  markdown: bool = False) -> str:
    if markdown:
        dst = Path(report_path) if report_path else Path("organizer_report.md")
        md_lines = _convert_to_markdown(lines)
        dst.write_text("\n".join(md_lines), encoding="utf-8")
        print(f"\n[报告] 已写入 Markdown {dst}")
    else:
        dst = Path(report_path) if report_path else Path(REPORT_FILE)
        dst.write_text("\n".join(lines), encoding="utf-8")
        print(f"\n[报告] 已写入 {dst}")
    return str(dst)


def _convert_to_markdown(lines: List[str]) -> List[str]:
    out: List[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("====="):
            title = stripped.strip("= ").strip()
            out.append(f"# {title}")
        elif stripped.startswith("⚠") or stripped.startswith("📋") or stripped.startswith("✅"):
            out.append(f"## {stripped}")
        elif stripped.startswith("──"):
            out.append(f"## {stripped.strip('─ ')}")
        elif re.match(r"^\d+\.\s", stripped):
            out.append(f"- {stripped}")
        elif stripped.startswith("  ·"):
            out.append(f"- {stripped[3:].strip()}")
        elif stripped.startswith("  ["):
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

    config = _load_workspace_config(folder)
    _apply_config(args, config)

    records: List[FileRecord] = []
    lock_dirs: List[str] = []
    project_summaries: Dict[str, ProjectSummary] = {}
    report: List[str] = []
    _report_line(report, f"===== 扫描报告  {datetime.datetime.now():%Y-%m-%d %H:%M} =====")
    _report_line(report, f"目标目录: {folder}")

    days_expired = args.expire_days if args.expire_days is not None else 30
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days_expired)
    aliases: Dict[str, str] = getattr(args, "_config_aliases", {})

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
            if fp.name in (LOCK_SENTINEL, WORKSPACE_CONFIG):
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
        rp = _write_report(report, args.report if isinstance(args.report, str) else None, markdown=md)
        _record_undo("scan", [], summary=f"扫描 {len(records)} 个文件", workspace=str(folder), report_path=rp)


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


def _batch_resolve_rename_conflicts(plans: List[Tuple[Path, str]]) -> List[Tuple[Path, str, Path]]:
    plans_sorted = sorted(plans, key=lambda x: (str(x[0].parent).lower(), x[1].lower(), x[0].name.lower()))

    per_dir_real: Dict[str, set] = {}
    for fp, _ideal in plans_sorted:
        key = str(fp.parent.resolve()).lower()
        if key not in per_dir_real:
            per_dir_real[key] = set()
            for child in fp.parent.iterdir():
                if child.is_file():
                    per_dir_real[key].add(child.name.lower())

    planned_names: Dict[str, set] = {}
    result: List[Tuple[Path, str, Path]] = []

    for fp, ideal_name in plans_sorted:
        key = str(fp.parent.resolve()).lower()
        occupied_lower = per_dir_real.get(key, set()) | planned_names.get(key, set())

        if ideal_name.lower() in occupied_lower:
            stem = Path(ideal_name).stem
            suffix = fp.suffix
            counter = 1
            while True:
                candidate = f"{stem}_{counter}{suffix}"
                if candidate.lower() not in occupied_lower:
                    final_name = candidate
                    break
                counter += 1
        else:
            final_name = ideal_name

        final_path = fp.parent / final_name
        result.append((fp, ideal_name, final_path))
        planned_names.setdefault(key, set()).add(final_name.lower())

    return result


def cmd_rename(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    config = _load_workspace_config(folder)
    _apply_config(args, config)

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

    raw_plans: List[Tuple[Path, str]] = []

    for root, dirs, files in os.walk(folder):
        rp = Path(root)
        if _is_locked(rp, args.lock_dir or []):
            dirs.clear(); continue
        if _is_inside_sort_targets(rp, folder):
            dirs.clear(); continue
        for f in files:
            fp = rp / f
            if fp.name in (LOCK_SENTINEL, WORKSPACE_CONFIG):
                continue
            result = _apply_rename_scheme(fp, scheme)
            if result is None:
                continue
            new_name, _new_stem = result
            try:
                if (fp.parent / new_name).resolve() == fp.resolve():
                    continue
            except Exception:
                pass
            raw_plans.append((fp, new_name))

    resolved_plans = _batch_resolve_rename_conflicts(raw_plans)

    conflicts = [(fp, ideal, final) for fp, ideal, final in resolved_plans if final.name != ideal]
    clean_plans = [(fp, ideal, final) for fp, ideal, final in resolved_plans if final.name == ideal]

    if conflicts:
        _report_line(report, "")
        _report_line(report, f"⚠ 检测到 {len(conflicts)} 处命名冲突 (已自动处理):")
        for fp, ideal, final in conflicts[:30]:
            _report_line(report, f"  {fp.name}  ->  {ideal}  =>  {final.name}")
        if len(conflicts) > 30:
            _report_line(report, f"  ... 其余 {len(conflicts) - 30} 处省略")

    _report_line(report, "")
    _report_line(report, f"计划重命名 ({len(resolved_plans)} 个)  最终文件名:")
    for fp, ideal, final in resolved_plans[:60]:
        if final.name == ideal:
            _report_line(report, f"  {fp.name}  ->  {final.name}")
        else:
            _report_line(report, f"  {fp.name}  ->  {final.name}  (原方案: {ideal})")
    if len(resolved_plans) > 60:
        _report_line(report, f"  ... 其余 {len(resolved_plans) - 60} 个省略")

    if not resolved_plans:
        _report_line(report, "  (没有需要重命名的文件)")

    if args.preview:
        _report_line(report, f"\n预览: {len(resolved_plans)} 个文件将被重命名 (未执行)")
        if args.report is not False:
            _write_report(report, args.report if isinstance(args.report, str) else None)
        return

    if args.confirm and resolved_plans:
        try:
            answer = input(f"\n确认对以上 {len(resolved_plans)} 个文件执行重命名? [y/N]: ").strip().lower()
        except EOFError:
            answer = "n"
        if answer not in ("y", "yes"):
            _report_line(report, "\n用户取消，未执行任何操作。")
            if args.report is not False:
                _write_report(report, args.report if isinstance(args.report, str) else None)
            return

    count = 0
    for fp, ideal, final in resolved_plans:
        try:
            fp.rename(final)
            ops.append({"op": "rename", "src": str(fp), "dst": str(final)})
            count += 1
        except Exception as e:
            _report_line(report, f"  [失败] {fp.name}: {e}")

    rp = ""
    if args.report is not False:
        rp = _write_report(report, args.report if isinstance(args.report, str) else None)

    if ops:
        summary = f"重命名 {count} 个文件"
        _record_undo("rename", ops, scheme=scheme_name, summary=summary,
                     file_count=count, workspace=str(folder), report_path=rp)

    _report_line(report, f"\n重命名完成: {count} 个文件" + (f" (方案: {scheme_name})" if scheme_name else ""))


# ──────────────────────────── sort ────────────────────────────

def cmd_sort(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    config = _load_workspace_config(folder)
    _apply_config(args, config)

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
            if fp.name in (LOCK_SENTINEL, WORKSPACE_CONFIG):
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
                _report_line(report, f"  [跳过/相同] {fp.name} 已存在于目标")
                continue

            dest = dest_dir / f
            counter = 1
            while dest.exists():
                dest = dest_dir / f"{fp.stem}_{counter}{fp.suffix}"
                counter += 1

            _report_line(report, f"  [{cat}] {fp.name}  ->  {dest}")
            ops.append({"op": "move", "src": str(fp), "dst": str(dest)})
            if cat == "screenshot":
                count_ss += 1
            else:
                count_doc += 1

            if not args.preview:
                shutil.move(str(fp), str(dest))

    rp = ""
    if not args.preview and ops:
        summary = f"分流: 截图 {count_ss} 文档 {count_doc}"
        if args.report is not False:
            rp_val = _write_report(report, args.report if isinstance(args.report, str) else None)
            rp = rp_val
        _record_undo("sort", ops, summary=summary, file_count=len(ops),
                     workspace=str(folder), report_path=rp)

    _report_line(report, "")
    _report_line(report, f"截图分流: {count_ss}  文档分流: {count_doc}" + (" (预览)" if args.preview else ""))
    if count_skip_duplicate:
        _report_line(report, f"跳过重复文件: {count_skip_duplicate}")
    if count_skip_already:
        _report_line(report, f"跳过已在目标目录: {count_skip_already}")
    if not ops and not count_skip_duplicate and not count_skip_already:
        _report_line(report, "  (没有需要分流的文件，工作区已整洁)")

    if not args.preview and not ops:
        if args.report is not False:
            _write_report(report, args.report if isinstance(args.report, str) else None)


# ──────────────────────────── agenda ────────────────────────────

def _extract_tags(text: str) -> List[str]:
    return re.findall(r"#(\w+)", text)


def _extract_keywords(text: str, aliases: Dict[str, str] = None) -> List[str]:
    aliases = aliases or {}
    clean = re.sub(r"#\w+", "", text)
    clean = re.sub(r"\d{4}-\d{2}-\d{2}", "", clean)
    clean = re.sub(r"\d{4}", "", clean)
    kws = re.findall(r"[\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_]{1,}", clean)
    stop = {"完成", "今天", "今日", "计划", "过期", "任务", "事项", "待办", "收尾", "草稿", "周报", "归档", "整理",
            "done", "todo", "the", "and", "for", "with", "from", "this", "that", "not", "to", "of", "a", "is", "in"}
    result = []
    for k in kws:
        if k.lower() in stop or len(k) < 2:
            continue
        resolved = aliases.get(k, aliases.get(k.lower()))
        if resolved is None:
            for alias_val in aliases.values():
                if k.lower() in alias_val.lower() or alias_val.lower() in k.lower():
                    resolved = alias_val
                    break
        if resolved is None:
            resolved = k
        if resolved not in result:
            result.append(resolved)
    return result


def _find_todo_files(root: Path, lock_dirs: List[str] = None) -> List[Path]:
    lock_dirs = lock_dirs or []
    todo_names = {"todo.txt", "todo.md", "TODO.md", "TODO.txt", "todos.md", "TODOS.md"}
    results: List[Path] = []

    def _walk(p: Path) -> None:
        try:
            if _is_locked(p, lock_dirs):
                return
            for entry in p.iterdir():
                if entry.is_file() and entry.name.lower() in todo_names:
                    results.append(entry)
                elif entry.is_dir() and not entry.name.startswith("_") and entry.name not in SORT_DIR_NAMES:
                    _walk(entry)
        except (PermissionError, OSError):
            pass

    _walk(root)
    return sorted(results)


def _normalize_todo_text(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"^\s*[-*]\s*(\[[ x*]\]\s*)?", "", text)
    text = re.sub(r"^\s*[x✓✔]\s+", "", text)
    text = re.sub(r"#\w+", "", text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}", "", text)
    text = re.sub(r"\s+", "", text)
    return text


def cmd_agenda(args) -> None:
    todo_files: List[Path] = []

    positional = Path(args.todo_file).resolve()
    if positional.is_file():
        todo_files.append(positional)
        config_base = positional.parent
    elif positional.is_dir():
        found = _find_todo_files(positional, args.lock_dir or [])
        todo_files.extend(found)
        config_base = positional
    else:
        print(f"[错误] {positional} 不存在"); return

    if getattr(args, "file", None):
        for f in args.file:
            fp = Path(f).resolve()
            if fp.is_file() and fp not in todo_files:
                todo_files.append(fp)

    if getattr(args, "workspace_agenda", None):
        ws = Path(args.workspace_agenda).resolve()
        if ws.is_dir():
            found = _find_todo_files(ws, args.lock_dir or [])
            for fp in found:
                if fp not in todo_files:
                    todo_files.append(fp)

    if not todo_files:
        print("[错误] 未找到任何待办文件"); return

    config = _load_workspace_config(config_base)
    _apply_config(args, config)

    if args.project_dir:
        pd_config = _load_workspace_config(Path(args.project_dir))
        _apply_config(args, pd_config)

    aliases: Dict[str, str] = getattr(args, "_config_aliases", {})

    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")
    weekday_cn = ["一", "二", "三", "四", "五", "六", "日"][today.weekday()]

    all_items: List[dict] = []
    seen_normalized: Dict[str, dict] = {}
    source_counts: Dict[str, int] = {}

    date_re = re.compile(r"(\d{4}-\d{2}-\d{2})")

    for tf in todo_files:
        try:
            lines = tf.read_text(encoding="utf-8-sig").splitlines()
        except Exception:
            continue
        src = str(tf)
        source_counts[src] = 0
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            norm = _normalize_todo_text(stripped)
            if not norm:
                continue

            is_done = _line_is_done(stripped)
            clean = re.sub(r"^(x\s+|✓\s+|✔\s+|\[x\]\s+|\[done\]\s*|-\s*\[x\]\s+|\*\s*\[x\]\s+)",
                            "", stripped, flags=re.IGNORECASE)
            tags = _extract_tags(stripped)
            keywords = _extract_keywords(clean, aliases)

            group = None
            for t in tags:
                if t in aliases:
                    group = aliases[t]
                    break
                resolved = aliases.get(t, aliases.get(t.lower()))
                if resolved:
                    group = resolved
                    break
            if group is None:
                for k in keywords:
                    if k in aliases:
                        group = aliases[k]
                        break
                else:
                    group = keywords[0] if keywords else "未分类"

            m = date_re.search(stripped)
            status = "待办"
            if is_done:
                status = "已完成"
            elif m:
                td = m.group(1)
                if td < today_str:
                    status = "过期"
                elif td == today_str:
                    status = "今日"
                else:
                    status = "计划"

            item_info = {
                "text": clean,
                "tags": tags,
                "keywords": keywords,
                "status": status,
                "source": src,
                "raw": stripped,
            }

            if norm in seen_normalized:
                prev = seen_normalized[norm]
                if src not in prev["sources"]:
                    prev["sources"].append(src)
            else:
                item_info["sources"] = [src]
                seen_normalized[norm] = item_info
                all_items.append(item_info)
                source_counts[src] = source_counts.get(src, 0) + 1

    report: List[str] = []
    multi_source = len(todo_files) > 1
    _report_line(report, f"===== 今日清单  {today_str} 星期{weekday_cn} =====")

    if multi_source:
        _report_line(report, f"来源文件 ({len(todo_files)} 个, {len(all_items)} 条去重后):")
        for tf in todo_files:
            cnt = source_counts.get(str(tf), 0)
            _report_line(report, f"  · {tf}  ({cnt} 条)")

    due_items = [it for it in all_items if it["status"] == "过期"]
    todo_items = [it for it in all_items if it["status"] in ("今日", "计划", "待办")]
    done_items = [it for it in all_items if it["status"] == "已完成"]

    _report_line(report, "")

    if due_items:
        _report_line(report, "⚠ 过期事项:")
        for i, it in enumerate(due_items, 1):
            src_tag = f"  ({Path(it['sources'][0]).name})" if multi_source else ""
            _report_line(report, f"  {i}. [{it['status']}] {it['text']}{src_tag}")
        _report_line(report, "")

    _report_line(report, "📋 待办事项:")
    for i, it in enumerate(todo_items, 1):
        src_tag = f"  ({Path(it['sources'][0]).name})" if multi_source else ""
        _report_line(report, f"  {i}. [{it['status']}] {it['text']}{src_tag}")

    if done_items:
        _report_line(report, "")
        _report_line(report, "✅ 已完成:")
        for it in done_items:
            src_tag = f"  ({Path(it['sources'][0]).name})" if multi_source else ""
            _report_line(report, f"  · {it['text']}{src_tag}")

    _report_line(report, "")
    _report_line(report, f"统计: 待办 {len(todo_items)}  过期 {len(due_items)}  完成 {len(done_items)}")

    project_groups: Dict[str, List[dict]] = {}
    for it in all_items:
        group = it["keywords"][0] if it["keywords"] else "未分类"
        project_groups.setdefault(group, []).append(it)

    show_groups = bool(args.project_dir) or bool(aliases) or len(project_groups) > 1

    if show_groups:
        proj_root = Path(args.project_dir).resolve() if args.project_dir else None
        _report_line(report, "")
        _report_line(report, "── 按项目关键词分组" + (" + 关联素材" if proj_root else "") + " ──")

        file_index: Dict[str, List[str]] = {}
        dir_name_index: Dict[str, List[str]] = {}
        if proj_root and proj_root.is_dir():
            for entry in sorted(proj_root.iterdir()):
                if entry.is_dir() and not entry.name.startswith("_") and entry.name not in SORT_DIR_NAMES:
                    if not _is_locked(entry, args.lock_dir or []):
                        dir_name_index.setdefault(entry.name.lower(), []).append(str(entry))
            for root, _, files in os.walk(proj_root):
                rp = Path(root)
                if _is_locked(rp, args.lock_dir or []):
                    continue
                for f in files:
                    if f in (LOCK_SENTINEL, WORKSPACE_CONFIG):
                        continue
                    for kw in _extract_keywords(f, aliases):
                        file_index.setdefault(kw.lower(), []).append(str(rp / f))

        todo_keywords_set = set()
        for group in project_groups:
            todo_keywords_set.add(group.lower())

        def _find_related_files(group_name: str) -> List[str]:
            results: List[str] = []
            g_lower = group_name.lower()
            if g_lower in file_index:
                results.extend(file_index[g_lower])
            if g_lower in dir_name_index:
                for dp in dir_name_index[g_lower]:
                    for root, _, files in os.walk(dp):
                        for f in files:
                            if f not in (LOCK_SENTINEL, WORKSPACE_CONFIG):
                                results.append(str(Path(root) / f))
            for alias_from, alias_to in aliases.items():
                if alias_to.lower() == g_lower:
                    if alias_from.lower() in file_index:
                        for fp in file_index[alias_from.lower()]:
                            if fp not in results:
                                results.append(fp)
                    if alias_from.lower() in dir_name_index:
                        for dp in dir_name_index[alias_from.lower()]:
                            if dp not in [str(Path(r).parent.parent) for r in results]:
                                for root, _, files in os.walk(dp):
                                    for f in files:
                                        if f not in (LOCK_SENTINEL, WORKSPACE_CONFIG):
                                            results.append(str(Path(root) / f))
            return results

        for group, items in sorted(project_groups.items()):
            _report_line(report, f"\n## 项目: {group}")
            for it in items:
                tag_str = " ".join(f"#{t}" for t in it.get("tags", []))
                src_tag = f"  ({Path(it['sources'][0]).name})" if multi_source else ""
                line = f"  · [{it['status']}] {it['text']}"
                if tag_str:
                    line += f"  {tag_str}"
                line += src_tag
                _report_line(report, line)

            if proj_root:
                related = _find_related_files(group)
                if related:
                    _report_line(report, f"  📎 相关素材 ({len(related)}):")
                    for rf in related[:10]:
                        _report_line(report, f"      {rf}")
                    if len(related) > 10:
                        _report_line(report, f"      ... 其余 {len(related) - 10} 个省略")

        orphaned_materials: List[Tuple[str, int]] = []
        if proj_root:
            for kw_lower, files in file_index.items():
                if kw_lower not in todo_keywords_set:
                    orphaned_materials.append((kw_lower, len(files)))

        orphaned_todos: List[str] = []
        if proj_root:
            for group in project_groups:
                if not _find_related_files(group) and group != "未分类":
                    orphaned_todos.append(group)

        long_expired_projects: List[str] = []
        for group, items in project_groups.items():
            if any(it.get("status") == "过期" for it in items):
                long_expired_projects.append(group)

        if orphaned_materials or orphaned_todos or long_expired_projects:
            _report_line(report, "")
            _report_line(report, "── 周报摘要 ──")
            if orphaned_materials:
                _report_line(report, f"\n无待办关联的素材 ({len(orphaned_materials)} 个关键词):")
                for kw, cnt in sorted(orphaned_materials, key=lambda x: -x[1])[:10]:
                    _report_line(report, f"  · {kw}: {cnt} 个文件")
            if orphaned_todos:
                _report_line(report, f"\n无素材关联的待办 ({len(orphaned_todos)} 个):")
                for g in orphaned_todos:
                    _report_line(report, f"  · {g}")
            if long_expired_projects:
                _report_line(report, f"\n长期过期项目 ({len(long_expired_projects)} 个):")
                for g in long_expired_projects:
                    _report_line(report, f"  · {g}")

    if args.report is not False:
        md = getattr(args, "markdown", False)
        _write_report(report, args.report if isinstance(args.report, str) else None, markdown=md)


# ──────────────────────────── archive ────────────────────────────

def cmd_archive(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    config = _load_workspace_config(folder)
    _apply_config(args, config)

    days = args.days if args.days is not None else 30
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    zip_mode = getattr(args, "zip", False)
    zip_all = getattr(args, "zip_all", False)

    report: List[str] = []
    ops: List[Dict] = []
    _report_line(report, f"===== 归档报告  {datetime.datetime.now():%Y-%m-%d %H:%M} =====")
    _report_line(report, f"过期阈值: {days} 天 (早于 {cutoff:%Y-%m-%d})")

    if zip_mode and not zip_all:
        _report_line(report, "归档模式: --zip (仅确认完成的项目，如需全部打包请用 --zip-all)")
    elif zip_all:
        _report_line(report, "归档模式: --zip-all (打包所有项目目录)")

    expired: List[Path] = []
    project_dirs: List[Tuple[Path, bool, str]] = []

    for entry in sorted(folder.iterdir()):
        if entry.name.startswith("_"):
            continue
        if entry.name == WORKSPACE_CONFIG:
            continue
        if _is_locked(entry, args.lock_dir or []):
            _report_line(report, f"  [跳过] {entry.name}  原因: 锁定目录")
            continue
        if entry.is_file():
            mtime = datetime.datetime.fromtimestamp(entry.stat().st_mtime)
            if mtime < cutoff:
                expired.append(entry)
        elif entry.is_dir():
            if entry.name in SORT_DIR_NAMES:
                _report_line(report, f"  [跳过] {entry.name}  原因: 系统目录")
                continue
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

    if zip_mode or zip_all:
        to_archive = []
        for p, completed, reason in project_dirs:
            if zip_all or completed:
                to_archive.append((p, completed, reason))

        skipped = [(p, c, r) for p, c, r in project_dirs if p not in [x[0] for x in to_archive]]

        _report_line(report, "")
        _report_line(report, f"项目目录评估 ({len(project_dirs)} 个):")
        for pd, completed, reason in project_dirs:
            if completed:
                action = "归档"
                detail = f"完成: {reason}"
            elif zip_all:
                action = "归档"
                detail = "未完成 (--zip-all 强制)"
            else:
                action = "跳过"
                detail = "未完成"
            _report_line(report, f"  [{action}] {pd.name}  原因: {detail}")

        if to_archive:
            _report_line(report, f"\n打包归档:")
            archived_projects: List[dict] = []
            for pd, completed, reason in to_archive:
                zip_name = f"{pd.name}_{datetime.datetime.now():%Y%m%d}.zip"
                zip_path = archive_dir / zip_name
                if zip_path.exists():
                    _report_line(report, f"  [跳过] {pd.name}  原因: 归档 {zip_name} 已存在")
                    continue
                detail = f"完成: {reason}" if completed else "未完成 (--zip-all)"
                _report_line(report, f"  {pd.name}  ->  {zip_path}  ({detail})")
                ops.append({"op": "archive_zip", "src": str(pd), "dst": str(zip_path)})
                file_count = 0
                if not args.preview:
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for root, _, files in os.walk(pd):
                            for f in files:
                                fp = Path(root) / f
                                try:
                                    arcname = str(fp.relative_to(pd.parent))
                                    zf.write(fp, arcname)
                                    file_count += 1
                                except Exception:
                                    pass
                    archived_projects.append({
                        "project": pd.name,
                        "zip_path": str(zip_path),
                        "completed_reason": reason if completed else "未完成 (--zip-all 强制)",
                        "total_files": file_count,
                    })
                    _add_archive_entry(
                        project_name=pd.name,
                        workspace=str(folder),
                        zip_path=str(zip_path),
                        completed_reason=reason if completed else "未完成 (--zip-all 强制)",
                        total_files=file_count,
                    )
                else:
                    cnt = sum(len(files) for _, _, files in os.walk(pd))
                    archived_projects.append({
                        "project": pd.name,
                        "zip_path": str(zip_path),
                        "completed_reason": reason if completed else "未完成 (--zip-all 强制)",
                        "total_files": cnt,
                    })

            if not args.preview and archived_projects:
                local_idx = archive_dir / "index.json"
                existing = []
                if local_idx.exists():
                    try:
                        existing = json.loads(local_idx.read_text(encoding="utf-8-sig"))
                    except Exception:
                        existing = []
                ts = datetime.datetime.now().isoformat()
                for ap in archived_projects:
                    existing.append({**ap, "timestamp": ts, "expired_files": len(expired)})
                local_idx.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        for pd, completed, reason in project_dirs:
            _report_line(report, f"  [跳过] {pd.name}  原因: 未使用 --zip 或 --zip-all")

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

    rp = ""
    if not args.preview and ops:
        summary = f"归档: 过期文件 {len(expired)} 项目 {len([o for o in ops if o.get('op') == 'archive_zip'])}"
        if args.report is not False:
            rp_val = _write_report(report, args.report if isinstance(args.report, str) else None,
                                   markdown=getattr(args, "markdown", False))
            rp = rp_val
        _record_undo("archive", ops, summary=summary, file_count=len(ops),
                     workspace=str(folder), report_path=rp)

    zip_count = len([o for o in ops if o.get("op") == "archive_zip"])
    _report_line(report, f"\n归档完成: 过期文件 {len(expired)}  项目归档 {zip_count}" + (" (预览)" if args.preview else ""))

    if not args.preview and not ops:
        if args.report is not False:
            _write_report(report, args.report if isinstance(args.report, str) else None,
                          markdown=getattr(args, "markdown", False))


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
            fc = entry.get("file_count", 0) or len(entry.get("operations", []))
            label = f"[{ts}] {task}"
            if scheme:
                label += f"  方案={scheme}"
            if summary:
                label += f"  {summary}"
            label += f"  ({fc} 个文件)"
            print(f"  {len(log) - idx + 1}. {label}")
        return

    entry = log[-1]
    ts = entry.get("timestamp", "?")
    task = entry.get("task", "?")
    scheme = entry.get("scheme", "")
    summary = entry.get("summary", "")
    fc = entry.get("file_count", 0) or len(entry.get("operations", []))
    ops = entry.get("operations", [])

    label = f"撤销: [{task}]"
    if scheme:
        label += f"  方案='{scheme}'"
    if summary:
        label += f"  {summary}"
    label += f"  ({fc} 个文件)"
    print(f"{label}  (时间 {ts})")

    for op in reversed(ops):
        kind = op.get("op")
        src = op.get("src", "")
        dst = op.get("dst", "")

        if kind == "rename":
            if Path(dst).exists():
                Path(dst).rename(src)
                print(f"  还原重命名: {Path(dst).name} -> {Path(src).name}")
            else:
                print(f"  [跳过] 目标不存在: {dst}")

        elif kind == "move":
            if Path(dst).exists():
                src_dir = Path(src).parent
                src_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(dst, src)
                print(f"  还原移动: {Path(dst).name} -> {Path(src).name}")
            else:
                print(f"  [跳过] 目标不存在: {dst}")

        elif kind == "archive_zip":
            zp = Path(dst)
            if zp.exists():
                zp.unlink()
                print(f"  删除归档: {zp.name}")
            src_p = Path(src)
            if src_p.exists():
                print(f"  保留原目录: {src_p.name}")
            else:
                print(f"  [警告] 原目录已不存在: {src}")

    log.pop()
    _save_undo_log(log)
    print("撤销完成。")


# ──────────────────────────── status ────────────────────────────

def cmd_status(args) -> None:
    log = _load_undo_log()
    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")

    print(f"===== 工作区状态  {today_str} =====\n")

    if not log:
        print("暂无操作历史。")
        return

    recent = log[-20:]

    task_counts: Dict[str, int] = {}
    for entry in log:
        task = entry.get("task", "?")
        task_counts[task] = task_counts.get(task, 0) + 1

    print(f"累计操作: {len(log)} 次")
    for task, cnt in sorted(task_counts.items(), key=lambda x: -x[1]):
        print(f"  {task}: {cnt} 次")

    today_ops = [e for e in log if e.get("timestamp", "")[:10] == today_str]
    if today_ops:
        print(f"\n今日操作 ({len(today_ops)} 次):")
        for entry in today_ops:
            ts = entry.get("timestamp", "?")[11:19]
            task = entry.get("task", "?")
            summary = entry.get("summary", "")
            scheme = entry.get("scheme", "")
            fc = entry.get("file_count", 0) or len(entry.get("operations", []))
            label = f"  [{ts}] {task}"
            if scheme:
                label += f"  方案={scheme}"
            if summary:
                label += f"  {summary}"
            label += f"  ({fc} 个文件)"
            print(label)

    print(f"\n可撤销操作 ({len(log)} 条):")
    for idx, entry in enumerate(reversed(recent), 1):
        ts = entry.get("timestamp", "?")[:19]
        task = entry.get("task", "?")
        scheme = entry.get("scheme", "")
        summary = entry.get("summary", "")
        fc = entry.get("file_count", 0) or len(entry.get("operations", []))
        ws = entry.get("workspace", "")
        rp = entry.get("report_path", "")

        label = f"  {idx}. [{ts}] {task}"
        if scheme:
            label += f"  方案={scheme}"
        if summary:
            label += f"  {summary}"
        label += f"  ({fc} 个文件)"
        print(label)
        if ws:
            print(f"     工作区: {ws}")
        if rp:
            print(f"     报告: {rp}")

    report_files: List[str] = []
    for entry in recent:
        rp = entry.get("report_path", "")
        if rp and Path(rp).exists():
            if rp not in report_files:
                report_files.append(rp)
    if report_files:
        print(f"\n最近生成的报告:")
        for rp in report_files:
            size = Path(rp).stat().st_size
            mtime = datetime.datetime.fromtimestamp(Path(rp).stat().st_mtime)
            print(f"  {rp}  ({size / 1024:.1f}KB, {mtime:%Y-%m-%d %H:%M})")
    else:
        print(f"\n(未找到最近的报告文件)")

    archive_entries = _load_archive_index()
    if archive_entries:
        by_workspace: Dict[str, List[dict]] = {}
        for e in archive_entries[-50:]:
            ws = e.get("workspace", "未知")
            by_workspace.setdefault(ws, []).append(e)

        print(f"\n归档历史 (共 {len(archive_entries)} 条):")
        for ws, entries in sorted(by_workspace.items()):
            print(f"\n工作区: {ws}")
            for e in reversed(entries[-10:]):
                ts = e.get("timestamp", "?")[:19]
                proj = e.get("project", "?")
                reason = e.get("completed_reason", "")
                fc = e.get("total_files", 0)
                zp = e.get("zip_path", "")
                label = f"  [{ts}] {proj}"
                if reason:
                    label += f"  ({reason})"
                label += f"  {fc} 个文件"
                print(label)
                if zp:
                    print(f"     zip: {zp}")
    else:
        print(f"\n(暂无归档记录)")


# ──────────────────────────── config ────────────────────────────

def cmd_config(args) -> None:
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"[错误] {folder} 不是有效目录"); return

    cfg_path = folder / WORKSPACE_CONFIG

    if args.init:
        config = {
            "lock_dirs": [],
            "expire_days": 30,
            "report_path": "",
            "markdown": False,
            "project_keywords": [],
            "aliases": {},
        }
        _save_workspace_config(folder, config)
        print(f"已初始化工作区配置: {cfg_path}")
        return

    if args.validate:
        if not cfg_path.is_file():
            print(f"[错误] 工作区无配置文件: {cfg_path}"); return
        config = _load_workspace_config(folder)
        issues: List[Dict] = []

        report_path = config.get("report_path", "")
        if report_path:
            rp = Path(report_path)
            if not rp.is_absolute():
                rp = folder / rp
            if not rp.exists():
                issues.append({"类型": "报告目录", "键": "report_path", "值": report_path, "问题": "路径不存在"})

        lock_dirs = config.get("lock_dirs", []) or []
        for i, ld in enumerate(lock_dirs):
            lp = Path(ld)
            if not lp.is_absolute():
                lp = folder / lp
            if not lp.exists():
                issues.append({"类型": "锁定目录", "键": f"lock_dirs[{i}]", "值": ld, "问题": "目录不存在"})

        aliases = config.get("aliases", {}) or {}
        for alias, target in aliases.items():
            if isinstance(target, str) and ("/" in target or "\\" in target):
                tp = Path(target)
                if not tp.is_absolute():
                    tp = folder / tp
                if not tp.exists():
                    issues.append({"类型": "别名指向", "键": f"aliases.{alias}", "值": target, "问题": "指向路径不存在"})

        print(f"===== 配置校验  {cfg_path} =====")
        if not issues:
            print("✅ 所有配置项均有效，未发现失效路径")
        else:
            print(f"⚠ 发现 {len(issues)} 个失效项:\n")
            for iss in issues:
                print(f"  [{iss['类型']}] {iss['键']}")
                print(f"    值: {iss['值']}")
                print(f"    问题: {iss['问题']}")
                print()
        return

    if args.show or (not args.set_key and not args.set_alias and not args.remove_key):
        if not cfg_path.is_file():
            print(f"当前工作区无配置文件。使用 --init 创建。")
            return
        config = _load_workspace_config(folder)
        print(f"工作区配置: {cfg_path}\n")
        print(json.dumps(config, ensure_ascii=False, indent=2))
        return

    config = _load_workspace_config(folder)

    if args.remove_key:
        for k in args.remove_key:
            if k in config:
                del config[k]
                print(f"已移除: {k}")
            else:
                print(f"[跳过] 键不存在: {k}")
        _save_workspace_config(folder, config)
        return

    if args.set_key:
        for kv in args.set_key:
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    v = json.loads(v)
                except (json.JSONDecodeError, ValueError):
                    pass
                config[k] = v
                print(f"已设置: {k} = {v}")
        _save_workspace_config(folder, config)

    if args.set_alias:
        for kv in args.set_alias:
            if "=" in kv:
                alias, target = kv.split("=", 1)
                aliases = config.setdefault("aliases", {})
                aliases[alias] = target
                print(f"已添加别名: {alias} -> {target}")
        _save_workspace_config(folder, config)


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
    s_scan.add_argument("--expire-days", type=int, default=None, help="过期判定阈值天数 (默认 30)")
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
    s_agenda.add_argument("todo_file", help="待办文件或工作区目录 (自动扫描 todo 文件)")
    s_agenda.add_argument("--file", action="append", default=[], help="额外的待办文件路径 (可重复)")
    s_agenda.add_argument("--workspace", dest="workspace_agenda", help="从工作区递归扫描所有 todo 文件并合并")
    s_agenda.add_argument("--project-dir", help="扫描项目素材目录，自动关联待办")
    s_agenda.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录")
    s_agenda.add_argument("--markdown", action="store_true", help="报告导出为 Markdown")
    s_agenda.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # archive
    s_archive = sub.add_parser("archive", help="识别过期文件并打包归档完成项目")
    s_archive.add_argument("folder", help="目标文件夹路径")
    s_archive.add_argument("--days", type=int, default=None, help="过期天数阈值 (默认 30)")
    s_archive.add_argument("--zip", action="store_true", help="打包完成的项目 (仅完成项目)")
    s_archive.add_argument("--zip-all", action="store_true", help="打包所有项目 (含未完成)")
    s_archive.add_argument("--preview", action="store_true", help="预览模式")
    s_archive.add_argument("--lock-dir", action="append", default=[], help="手动锁定目录")
    s_archive.add_argument("--markdown", action="store_true", help="报告导出为 Markdown")
    s_archive.add_argument("--report", nargs="?", const=True, default=True, help="生成报告")

    # undo
    s_undo = sub.add_parser("undo", help="撤销最近一次整理操作")
    s_undo.add_argument("--list", action="store_true", help="列出可撤销的操作历史")

    # status
    s_status = sub.add_parser("status", help="查看工作区状态与操作历史")

    # config
    s_config = sub.add_parser("config", help="管理工作区配置")
    s_config.add_argument("folder", help="工作区目录路径")
    s_config.add_argument("--init", action="store_true", help="初始化工作区配置文件")
    s_config.add_argument("--show", action="store_true", help="显示当前配置")
    s_config.add_argument("--validate", action="store_true", help="校验配置中的路径是否有效")
    s_config.add_argument("--set", dest="set_key", action="append", help="设置配置键值 (key=value)")
    s_config.add_argument("--set-alias", action="append", help="设置项目别名 (alias=target)")
    s_config.add_argument("--remove", dest="remove_key", action="append", help="移除配置键")

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
        "status": cmd_status,
        "config": cmd_config,
    }
    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
