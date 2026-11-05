#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gan.py -- read / edit / validate / open GanttProject (.gan) project files.

GanttProject stores projects as a single UTF-8 XML document.  This tool edits
that document in place, keeping every element it does not understand, so that
custom columns, views, baselines and unknown future attributes survive a
round-trip.

Only the Python standard library is required (3.9+).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
import zipfile
from datetime import date, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

WEEK_KEYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
WEEK_LABELS = ("日", "一", "二", "三", "四", "五", "六")

# <depend type="..."> uses ConstraintType.persistentValue = ordinal + 1
DEP_TYPES = {"SS": 1, "FS": 2, "FF": 3, "SF": 4}
DEP_TYPE_NAMES = {v: k for k, v in DEP_TYPES.items()}
DEP_TYPE_DOC = {
    "SS": "开始-开始 (Start-Start)",
    "FS": "完成-开始 (Finish-Start)",
    "FF": "完成-完成 (Finish-Finish)",
    "SF": "开始-完成 (Start-Finish)",
}

# Task.Priority persistent values are NOT the ordinal order in GanttProject
PRIORITY_ORDINAL = {0: "lowest", 1: "low", 2: "normal", 3: "high", 4: "highest"}
PRIORITY_PERSISTENT = {"lowest": "3", "low": "0", "normal": "1", "high": "2", "highest": "4"}
PERSISTENT_PRIORITY = {v: k for k, v in PRIORITY_PERSISTENT.items()}

DEFAULT_DATE_FMT = "%Y-%m-%d"


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

class GanError(Exception):
    """User-facing error."""


def parse_iso_date(value: str, what: str = "date") -> date:
    if not value:
        raise GanError(f"{what} 不能为空")
    m = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})", str(value))
    if not m:
        raise GanError(f"{what} 格式无效: {value!r}（应为 YYYY-MM-DD）")
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError as exc:
        raise GanError(f"{what} 不是合法日期: {value!r} ({exc})") from exc


def date_suffix(raw: str) -> str:
    """Return the time part of a GanttProject date attribute, if any."""
    if not raw:
        return ""
    m = re.match(r"^\s*\d{4}-\d{2}-\d{2}(T.*)$", raw)
    return m.group(1) if m else ""


def set_attr_date(el: ET.Element, attr: str, new_date: date) -> None:
    suffix = date_suffix(el.get(attr) or "")
    el.set(attr, new_date.isoformat() + suffix)


def disp_width(text: str) -> int:
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def pad(text: str, width: int) -> str:
    return text + " " * max(0, width - disp_width(text))


def truncate(text: str, width: int) -> str:
    if disp_width(text) <= width:
        return text
    out = ""
    used = 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if used + w > width - 1:
            break
        out += ch
        used += w
    return out + "…"


def fmt_date(d: date | None) -> str:
    return d.strftime(DEFAULT_DATE_FMT) if d else "-"


# --------------------------------------------------------------------------
# work calendar
# --------------------------------------------------------------------------

class WorkCalendar:
    """Working-day arithmetic driven by <calendars> of the project file."""

    def __init__(self, root: ET.Element):
        self.week = [True] * 7  # True == working day
        self.holidays: set[date] = set()
        self.extra_work: set[date] = set()

        week_el = root.find("./calendars/day-types/default-week")
        if week_el is not None:
            for idx, key in enumerate(WEEK_KEYS):
                raw = week_el.get(key)
                if raw is not None:
                    # GanttProject: 0 = working day, 1 = day off
                    self.week[idx] = raw.strip() == "0"

        # 3.x style: <calendars><day-types><days><day date=".." type="HOLIDAY"/>
        for day_el in root.findall("./calendars/day-types/days/day"):
            raw_date = day_el.get("date")
            if not raw_date:
                continue
            try:
                d = parse_iso_date(raw_date)
            except GanError:
                continue
            kind = (day_el.get("type") or "").upper()
            if "HOLIDAY" in kind or "OFF" in kind:
                self.holidays.add(d)
            elif "WORK" in kind:
                self.extra_work.add(d)

        # 2.x style: <calendars><date year="2024" month="1" date="1" type="HOLIDAY"/>
        for day_el in root.findall("./calendars/date"):
            try:
                d = date(int(day_el.get("year")), int(day_el.get("month")), int(day_el.get("date")))
            except (TypeError, ValueError):
                continue
            kind = (day_el.get("type") or "").upper()
            if "HOLIDAY" in kind or "OFF" in kind:
                self.holidays.add(d)
            elif "WORK" in kind:
                self.extra_work.add(d)

    def is_working(self, d: date) -> bool:
        if d in self.holidays:
            return False
        if d in self.extra_work:
            return True
        return self.week[d.weekday()]

    def roll_forward(self, d: date) -> date:
        for _ in range(3660):
            if self.is_working(d):
                return d
            d += timedelta(days=1)
        raise GanError("日历中找不到可用工作日，请检查 <calendars> 配置")

    def add_working_days(self, start: date, days: int) -> date:
        """Exclusive end date: `days` working days starting at `start`."""
        d = self.roll_forward(start)
        if days <= 0:
            return d
        remaining = days
        guard = 0
        while remaining > 0:
            d += timedelta(days=1)
            guard += 1
            if guard > 10000:
                raise GanError("工期过大或日历异常")
            if self.is_working(d):
                remaining -= 1
        return d

    def working_days_between(self, start: date, end_exclusive: date) -> int:
        if end_exclusive <= start:
            return 0
        count = 0
        d = start
        while d < end_exclusive:
            if self.is_working(d):
                count += 1
            d += timedelta(days=1)
        return count

    def describe(self) -> str:
        days = [WEEK_LABELS[i] for i in range(7) if self.week[i]]
        extra = f"，另有 {len(self.holidays)} 个假日" if self.holidays else ""
        return "工作日: 周" + "、周".join(days) + extra


# --------------------------------------------------------------------------
# task / project model
# --------------------------------------------------------------------------

class Task:
    __slots__ = ("el", "project", "parent", "children")

    def __init__(self, el: ET.Element, project: "Project", parent: "Task | None"):
        self.el = el
        self.project = project
        self.parent = parent
        self.children: list[Task] = []

    # ---- identity -------------------------------------------------------
    @property
    def id(self) -> int:
        try:
            return int(self.el.get("id"))
        except (TypeError, ValueError):
            return -1

    @property
    def uid(self) -> str:
        return self.el.get("uid") or ""

    @property
    def name(self) -> str:
        return self.el.get("name") or ""

    def set_name(self, value: str) -> None:
        self.el.set("name", value)

    # ---- dates ----------------------------------------------------------
    @property
    def start(self) -> date | None:
        try:
            return parse_iso_date(self.el.get("start"))
        except GanError:
            return None

    @property
    def duration(self) -> int:
        try:
            return int(self.el.get("duration") or 0)
        except ValueError:
            return 0

    @property
    def end_exclusive(self) -> date | None:
        start = self.start
        if start is None:
            return None
        return self.project.cal.add_working_days(start, self.duration)

    @property
    def display_end(self) -> date | None:
        """Last working day shown in GanttProject's 'End date' column."""
        start = self.start
        if start is None:
            return None
        if self.duration <= 0:
            return start
        return self.project.cal.add_working_days(start, self.duration - 1)

    @property
    def complete(self) -> int:
        try:
            return int(self.el.get("complete") or 0)
        except ValueError:
            return 0

    @property
    def is_summary(self) -> bool:
        return bool(self.children)

    @property
    def is_milestone(self) -> bool:
        return self.duration <= 0 and not self.children

    @property
    def depth(self) -> int:
        return 0 if self.parent is None else self.parent.depth + 1

    def path(self) -> str:
        parts = []
        node: Task | None = self
        while node is not None:
            parts.append(node.name)
            node = node.parent
        return " / ".join(reversed(parts))

    # ---- dependencies ---------------------------------------------------
    def deps(self) -> list[dict]:
        """Dependencies stored on this task -> its successors."""
        out = []
        for dep in self.el.findall("depend"):
            try:
                sid = int(dep.get("id"))
            except (TypeError, ValueError):
                continue
            typ = DEP_TYPE_NAMES.get(int(dep.get("type") or 2), "FS")
            try:
                lag = int(dep.get("difference") or 0)
            except ValueError:
                lag = 0
            out.append({
                "successor": sid,
                "type": typ,
                "lag": lag,
                "hardness": dep.get("hardness") or "Strong",
                "el": dep,
            })
        return out

    def predecessors(self) -> list[dict]:
        out = []
        for other in self.project.tasks:
            for dep in other.deps():
                if dep["successor"] == self.id:
                    item = dict(dep)
                    item["predecessor"] = other.id
                    item.pop("el", None)
                    item["predecessor_name"] = other.name
                    out.append(item)
        return out

    def assignments(self) -> list[dict]:
        out = []
        for a in self.project.allocations():
            if a["task_id"] != self.id:
                continue
            res = self.project.resources_by_id.get(a["resource_id"], {})
            out.append({
                "resource_id": a["resource_id"],
                "resource": res.get("name", ""),
                "responsible": a["responsible"],
                "load": a["load"],
            })
        return out

    # ---- mutation -------------------------------------------------------
    def set_start(self, value: date) -> None:
        value = self.project.cal.roll_forward(value)
        set_attr_date(self.el, "start", value)
        # Keep "earliest begin" / "must start on" constraint in sync, otherwise
        # GanttProject's scheduler will drag the task back on open.
        if self.el.get("thirdDate"):
            set_attr_date(self.el, "thirdDate", value)

    def set_duration(self, days: int) -> None:
        if days < 0:
            raise GanError("工期不能为负")
        self.el.set("duration", str(int(days)))

    def set_progress(self, pct: int) -> None:
        self.el.set("complete", str(max(0, min(100, int(pct)))))

    def resource_names(self) -> str:
        names = []
        for alloc in self.assignments():
            res = self.project.resources_by_id.get(alloc["resource_id"])
            if res:
                names.append(res["name"] if not alloc["responsible"] else f"{res['name']}*")
        return ", ".join(names)


class Project:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.format = "xml"
        self.zip_entries: list[tuple[zipfile.ZipInfo, bytes]] = []
        self.zip_member = "project.xml"
        self.root = self._load()
        self.cal = WorkCalendar(self.root)
        self.tasks: list[Task] = []
        self.by_id: dict[int, Task] = {}
        self.by_uid: dict[str, Task] = {}
        self._reindex()

    # ---- io -------------------------------------------------------------
    def _load(self) -> ET.Element:
        if not self.path.exists():
            raise GanError(f"文件不存在: {self.path}")
        data = self.path.read_bytes()
        if data[:4] == b"PK\x03\x04":
            self.format = "zip"
            with zipfile.ZipFile(self.path) as zf:
                names = zf.namelist()
                if "project.xml" in names:
                    self.zip_member = "project.xml"
                else:
                    xml_names = [n for n in names if n.lower().endswith(".xml")]
                    if not xml_names:
                        raise GanError("压缩包内未找到 project.xml")
                    self.zip_member = xml_names[0]
                self.zip_entries = [(info, zf.read(info.filename)) for info in zf.infolist()]
                data = zf.read(self.zip_member)
        if data.startswith(b"\xef\xbb\xbf"):
            data = data[3:]
        try:
            parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
            return ET.fromstring(data, parser=parser)
        except ET.ParseError as exc:
            raise GanError(f"XML 解析失败: {exc}") from exc

    def to_bytes(self) -> bytes:
        # Keep comments, drop them from indentation side effects only.
        ET.indent(self.root, space="    ")
        xml_body = ET.tostring(self.root, encoding="unicode")
        return ('<?xml version="1.0" encoding="UTF-8"?>' + xml_body).encode("utf-8")

    def backup(self) -> Path | None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.bak-{stamp}")
        shutil.copy2(self.path, target)
        return target

    def save(self, backup: bool = True) -> Path | None:
        bak = self.backup() if backup else None
        payload = self.to_bytes()
        if self.format == "zip":
            tmp = self.path.with_name(self.path.name + ".tmp")
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
                for info, blob in self.zip_entries:
                    if info.filename == self.zip_member:
                        blob = payload
                    zf.writestr(info, blob)
            os.replace(tmp, self.path)
        else:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, self.path)
        return bak

    # ---- index ----------------------------------------------------------
    def _reindex(self) -> None:
        self.tasks = []
        self.by_id = {}
        self.by_uid = {}

        def walk(el: ET.Element, parent: Task | None) -> None:
            for child in el.findall("task"):
                task = Task(child, self, parent)
                self.tasks.append(task)
                if task.id in self.by_id:
                    raise GanError(f"任务 id 重复: {task.id}")
                self.by_id[task.id] = task
                if task.uid:
                    self.by_uid[task.uid] = task
                if parent is not None:
                    parent.children.append(task)
                walk(child, task)

        walk(self.root.find("./tasks") if self.root.find("./tasks") is not None else self.root, None)

    def reindex(self) -> None:
        self._reindex()

    # ---- lookup ---------------------------------------------------------
    def find(self, ref) -> Task:
        if isinstance(ref, Task):
            return ref
        text = str(ref).strip()
        if not text:
            raise GanError("任务引用为空")
        if re.fullmatch(r"#?\d+", text):
            tid = int(text.lstrip("#"))
            if tid in self.by_id:
                return self.by_id[tid]
            raise GanError(f"找不到 id={tid} 的任务")
        exact = [t for t in self.tasks if t.name == text]
        if len(exact) == 1:
            return exact[0]
        lower = text.lower()
        partial = [t for t in self.tasks if lower in t.name.lower()]
        if len(partial) == 1:
            return partial[0]
        if len(partial) > 1:
            sample = "；".join(f"#{t.id} {t.name}" for t in partial[:6])
            raise GanError(f"任务名 “{text}” 匹配到 {len(partial)} 个任务，请改用 id：{sample}")
        raise GanError(f"找不到任务 “{text}”")

    def tasks_element(self) -> ET.Element:
        el = self.root.find("tasks")
        if el is None:
            el = ET.SubElement(self.root, "tasks")
        return el

    def next_task_id(self) -> int:
        return max([t.id for t in self.tasks], default=0) + 1

    # ---- resources ------------------------------------------------------
    def resources(self) -> list[dict]:
        out = []
        res_el = self.root.find("resources")
        if res_el is None:
            return out
        for r in res_el.findall("resource"):
            out.append({
                "id": int(r.get("id") or 0),
                "name": r.get("name") or "",
                "function": r.get("function") or "",
                "phone": r.get("phone") or "",
                "contacts": r.get("contacts") or "",
            })
        return out

    @property
    def resources_by_id(self) -> dict[int, dict]:
        return {r["id"]: r for r in self.resources()}

    def find_resource(self, ref) -> dict:
        text = str(ref).strip()
        for r in self.resources():
            if str(r["id"]) == text or r["name"] == text:
                return r
        for r in self.resources():
            if text.lower() in r["name"].lower():
                return r
        raise GanError(f"找不到资源 “{text}”")

    def add_resource(self, name: str, phone: str = "", contacts: str = "") -> dict:
        res_el = self.root.find("resources")
        if res_el is None:
            res_el = ET.SubElement(self.root, "resources")
        rid = max([r["id"] for r in self.resources()], default=-1) + 1
        el = ET.SubElement(res_el, "resource")
        el.set("id", str(rid))
        el.set("name", name)
        el.set("function", "")
        el.set("contacts", contacts)
        el.set("phone", phone)
        rate = ET.SubElement(el, "rate")
        rate.set("name", "standard")
        rate.set("value", "0")
        return {"id": rid, "name": name, "function": "", "phone": phone, "contacts": contacts}

    def allocations(self) -> list[dict]:
        out = []
        alloc_el = self.root.find("allocations")
        if alloc_el is None:
            return out
        for a in alloc_el.findall("allocation"):
            try:
                out.append({
                    "task_id": int(a.get("task-id")),
                    "resource_id": int(a.get("resource-id")),
                    "responsible": (a.get("responsible") or "false").lower() == "true",
                    "load": float(a.get("load") or 100),
                    "el": a,
                })
            except (TypeError, ValueError):
                continue
        return out

    # ---- dependencies ---------------------------------------------------
    def edges(self) -> list[dict]:
        out = []
        for task in self.tasks:
            for dep in task.deps():
                out.append({
                    "predecessor": task.id,
                    "successor": dep["successor"],
                    "type": dep["type"],
                    "lag": dep["lag"],
                    "hardness": dep["hardness"],
                })
        return out

    def remove_dependencies_to(self, task_id: int) -> int:
        removed = 0
        for task in self.tasks:
            for dep in list(task.el.findall("depend")):
                if str(dep.get("id")) == str(task_id):
                    task.el.remove(dep)
                    removed += 1
        return removed

    def remove_allocations_of(self, task_id: int) -> int:
        alloc_el = self.root.find("allocations")
        if alloc_el is None:
            return 0
        removed = 0
        for a in list(alloc_el.findall("allocation")):
            if str(a.get("task-id")) == str(task_id):
                alloc_el.remove(a)
                removed += 1
        return removed

    # ---- summary / scheduling ------------------------------------------
    def refresh_summaries(self) -> list[dict]:
        """Recompute start / duration of every summary task, bottom-up."""
        changes = []

        def visit(task: Task) -> None:
            for child in task.children:
                visit(child)
            if not task.children:
                return
            starts = [c.start for c in task.children if c.start]
            ends = [c.end_exclusive for c in task.children if c.end_exclusive]
            if not starts or not ends:
                return
            new_start = min(starts)
            new_end = max(ends)
            new_duration = self.cal.working_days_between(new_start, new_end)
            if task.start != new_start:
                changes.append({"task": task.id, "name": task.name, "field": "start",
                                "old": fmt_date(task.start), "new": fmt_date(new_start)})
                set_attr_date(task.el, "start", new_start)
            if task.duration != new_duration:
                changes.append({"task": task.id, "name": task.name, "field": "duration",
                                "old": task.duration, "new": new_duration})
                task.el.set("duration", str(new_duration))

        roots = [t for t in self.tasks if t.parent is None]
        for root in roots:
            visit(root)
        return changes

    def reschedule(self) -> list[dict]:
        """Push successors forward so that every dependency constraint holds."""
        changes = []
        incoming: dict[int, list[dict]] = {}
        for edge in self.edges():
            incoming.setdefault(edge["successor"], []).append(edge)

        # topological order over dependency edges (cycles already rejected)
        order: list[int] = []
        state: dict[int, int] = {}

        def dfs(tid: int) -> None:
            if state.get(tid) == 2:
                return
            if state.get(tid) == 1:
                return  # cycle guard, validated elsewhere
            state[tid] = 1
            for edge in [e for e in self.edges() if e["predecessor"] == tid]:
                dfs(edge["successor"])
            state[tid] = 2
            order.append(tid)

        for task in self.tasks:
            dfs(task.id)
        order.reverse()

        for tid in order:
            task = self.by_id.get(tid)
            if task is None or task.is_summary or not task.start:
                continue
            deps = incoming.get(tid, [])
            if not deps:
                continue
            proposed = None
            for edge in deps:
                pred = self.by_id.get(edge["predecessor"])
                if pred is None or pred.start is None or pred.end_exclusive is None:
                    continue
                lag = int(edge["lag"])
                if edge["type"] == "SS":
                    req = self.cal.add_working_days(pred.start, lag)
                elif edge["type"] == "FS":
                    req = self.cal.add_working_days(pred.end_exclusive, lag)
                elif edge["type"] == "FF":
                    req = self.cal.add_working_days(pred.end_exclusive, lag - task.duration)
                else:  # SF
                    req = self.cal.add_working_days(pred.start, lag - task.duration)
                req = self.cal.roll_forward(req)
                proposed = req if proposed is None else max(proposed, req)

            if proposed is None:
                continue
            current = task.start
            if proposed > current:
                changes.append({"task": tid, "name": task.name, "field": "start",
                                "old": fmt_date(current), "new": fmt_date(proposed)})
                task.set_start(proposed)
            elif proposed < current and all(
                e["hardness"].lower() != "rubber" for e in deps
            ):
                changes.append({"task": tid, "name": task.name, "field": "start",
                                "old": fmt_date(current), "new": fmt_date(proposed)})
                task.set_start(proposed)
        return changes

    # ---- validation -----------------------------------------------------
    def validate(self) -> list[tuple[str, str]]:
        problems: list[tuple[str, str]] = []

        seen = set()
        uids = set()
        for task in self.tasks:
            if task.id < 0:
                problems.append(("error", f"任务 “{task.name}” 缺少合法 id"))
            elif task.id in seen:
                problems.append(("error", f"任务 id 重复: {task.id}"))
            seen.add(task.id)
            if task.uid:
                if task.uid in uids:
                    problems.append(("error", f"任务 uid 重复: {task.uid} (#{task.id})"))
                uids.add(task.uid)
            if task.start is None:
                problems.append(("error", f"任务 #{task.id} “{task.name}” 的开始日期无法解析"))
            elif not self.cal.is_working(task.start):
                problems.append(("warn", f"任务 #{task.id} 开始于非工作日 {fmt_date(task.start)}"))
            if task.duration < 0:
                problems.append(("error", f"任务 #{task.id} 工期为负"))
            if not 0 <= task.complete <= 100:
                problems.append(("warn", f"任务 #{task.id} 进度超出 0-100: {task.complete}"))
            if task.parent is not None and task.parent.id in (task.id,):
                problems.append(("error", f"任务 #{task.id} 的父任务非法"))

        # dependencies
        edge_set = set()
        for task in self.tasks:
            for dep in task.deps():
                key = (task.id, dep["successor"])
                if dep["successor"] == task.id:
                    problems.append(("error", f"任务 #{task.id} 依赖自身"))
                if dep["successor"] not in self.by_id:
                    problems.append(("error", f"任务 #{task.id} 的依赖指向不存在的任务 {dep['successor']}"))
                if key in edge_set:
                    problems.append(("error", f"重复依赖 {task.id} -> {dep['successor']}"))
                edge_set.add(key)

        # cycles
        graph: dict[int, list[int]] = {}
        for pred, succ in edge_set:
            graph.setdefault(pred, []).append(succ)
        color: dict[int, int] = {}

        def has_cycle(node: int, stack: list[int]) -> bool:
            color[node] = 1
            stack.append(node)
            for nxt in graph.get(node, []):
                if color.get(nxt) == 1:
                    problems.append(("error", f"依赖成环: {' → '.join(map(str, stack[stack.index(nxt):] + [nxt]))}"))
                    return True
                if color.get(nxt, 0) == 0 and has_cycle(nxt, stack):
                    return True
            stack.pop()
            color[node] = 2
            return False

        for node in list(graph):
            if color.get(node, 0) == 0:
                has_cycle(node, [])

        # allocations
        res_ids = {r["id"] for r in self.resources()}
        for alloc in self.allocations():
            if alloc["task_id"] not in self.by_id:
                problems.append(("error", f"资源分配指向不存在的任务 {alloc['task_id']}"))
            if alloc["resource_id"] not in res_ids:
                problems.append(("error", f"资源分配指向不存在的资源 {alloc['resource_id']}"))

        # summary consistency
        for task in self.tasks:
            if not task.children:
                continue
            starts = [c.start for c in task.children if c.start]
            if not starts or task.start is None:
                continue
            if task.start != min(starts):
                problems.append(("warn", f"汇总任务 #{task.id} 的开始日期与子任务不一致"))

        return problems

    # ---- metadata -------------------------------------------------------
    def summary(self) -> dict:
        starts = [t.start for t in self.tasks if t.start]
        ends = [t.end_exclusive for t in self.tasks if t.end_exclusive]
        return {
            "file": str(self.path),
            "format": self.format,
            "project": self.root.get("name") or "",
            "company": self.root.get("company") or "",
            "version": self.root.get("version") or "",
            "locale": self.root.get("locale") or "",
            "view_date": self.root.get("view-date") or "",
            "tasks": len(self.tasks),
            "milestones": sum(1 for t in self.tasks if t.is_milestone),
            "summaries": sum(1 for t in self.tasks if t.is_summary),
            "resources": len(self.resources()),
            "dependencies": len(self.edges()),
            "start": fmt_date(min(starts)) if starts else "",
            "end": fmt_date(max(ends) - timedelta(days=1)) if ends else "",
            "calendar": self.cal.describe(),
        }


# --------------------------------------------------------------------------
# editing operations
# --------------------------------------------------------------------------

def new_task_element(task_id: int, uid: str, name: str, start: date, duration: int,
                     complete: int, priority: str | None, notes: str | None,
                     milestone: bool) -> ET.Element:
    el = ET.Element("task")
    el.set("id", str(task_id))
    el.set("uid", uid)
    el.set("name", name)
    el.set("color", "#8cb6ce")
    el.set("meeting", "false")
    el.set("start", start.isoformat())
    el.set("duration", "0" if milestone else str(duration))
    el.set("complete", str(complete))
    if priority:
        el.set("priority", priority)
    el.set("expand", "true")
    if notes:
        notes_el = ET.SubElement(el, "notes")
        notes_el.text = notes
    return el


def insert_dep(task_el: ET.Element, successor_id: int, dep_type: str, lag: int, hardness: str) -> None:
    for dep in task_el.findall("depend"):
        if str(dep.get("id")) == str(successor_id):
            dep.set("type", str(DEP_TYPES[dep_type]))
            dep.set("difference", str(lag))
            dep.set("hardness", hardness)
            return
    dep = ET.Element("depend")
    dep.set("id", str(successor_id))
    dep.set("type", str(DEP_TYPES[dep_type]))
    dep.set("difference", str(lag))
    dep.set("hardness", hardness)
    # keep <depend> before nested child tasks, like GanttProject does
    first_child = task_el.find("task")
    if first_child is not None:
        task_el.insert(list(task_el).index(first_child), dep)
    else:
        task_el.append(dep)


def remove_dep(task_el: ET.Element, successor_id: int) -> bool:
    for dep in list(task_el.findall("depend")):
        if str(dep.get("id")) == str(successor_id):
            task_el.remove(dep)
            return True
    return False


def parse_priority(value) -> str:
    text = str(value).strip().lower()
    if text in PRIORITY_PERSISTENT:
        return PRIORITY_PERSISTENT[text]
    if text.isdigit():
        num = int(text)
        if num in PRIORITY_ORDINAL:
            return PRIORITY_PERSISTENT[PRIORITY_ORDINAL[num]]
    raise GanError(f"优先级无效: {value!r}（可用 lowest/low/normal/high/highest 或 0-4）")


class Editor:
    def __init__(self, project: Project, recalc: bool = True):
        self.p = project
        self.recalc = recalc
        self.log: list[dict] = []
        self.warnings: list[str] = []

    # -- helpers ----------------------------------------------------------
    def _log(self, op: str, task_id: int | None, field: str, old, new, name: str = "") -> None:
        self.log.append({
            "op": op, "task": task_id, "name": name,
            "field": field, "old": old, "new": new,
        })

    def _task_label(self, task: Task) -> str:
        return f"#{task.id} {task.name}"

    # -- operations -------------------------------------------------------
    def update(self, ref, fields: dict) -> dict:
        task = self.p.find(ref)
        changed = []

        if "name" in fields and fields["name"] is not None:
            new = str(fields["name"])
            if new != task.name:
                self._log("update", task.id, "name", task.name, new, task.name)
                task.set_name(new)
                changed.append("name")

        if "start" in fields and fields["start"]:
            new_start = self.p.cal.roll_forward(parse_iso_date(fields["start"], "start"))
            if new_start != task.start:
                self._log("update", task.id, "start", fmt_date(task.start), fmt_date(new_start), task.name)
                task.set_start(new_start)
                changed.append("start")

        if "duration" in fields and fields["duration"] is not None:
            new_dur = int(fields["duration"])
            if new_dur != task.duration:
                self._log("update", task.id, "duration", task.duration, new_dur, task.name)
                task.set_duration(new_dur)
                changed.append("duration")

        if "end" in fields and fields["end"]:
            if task.start is None:
                raise GanError(f"任务 #{task.id} 没有开始日期，无法按结束日期推算工期")
            end_inclusive = parse_iso_date(fields["end"], "end")
            new_dur = self.p.cal.working_days_between(task.start, end_inclusive + timedelta(days=1))
            if new_dur <= 0:
                raise GanError(f"结束日期 {fields['end']} 早于开始日期 {fmt_date(task.start)}")
            if new_dur != task.duration:
                self._log("update", task.id, "duration", task.duration, new_dur, task.name)
                task.set_duration(new_dur)
                changed.append("duration")

        if "progress" in fields and fields["progress"] is not None:
            new = max(0, min(100, int(fields["progress"])))
            if new != task.complete:
                self._log("update", task.id, "complete", task.complete, new, task.name)
                task.set_progress(new)
                changed.append("progress")
        if "complete" in fields and fields["complete"] is not None:
            new = max(0, min(100, int(fields["complete"])))
            if new != task.complete:
                self._log("update", task.id, "complete", task.complete, new, task.name)
                task.set_progress(new)
                changed.append("complete")

        if "notes" in fields and fields["notes"] is not None:
            old = task.el.findtext("notes") or ""
            if str(fields["notes"]) != old:
                self._log("update", task.id, "notes", old, str(fields["notes"]), task.name)
                notes_el = task.el.find("notes")
                if notes_el is None:
                    notes_el = ET.Element("notes")
                    task.el.insert(0, notes_el)
                notes_el.text = str(fields["notes"]) or None
                changed.append("notes")

        if "color" in fields and fields["color"]:
            old = task.el.get("color")
            if old != fields["color"]:
                self._log("update", task.id, "color", old, fields["color"], task.name)
                task.el.set("color", str(fields["color"]))
                changed.append("color")

        if "priority" in fields and fields["priority"] is not None:
            old = task.el.get("priority") or "1"
            new = parse_priority(fields["priority"])
            if old != new:
                self._log("update", task.id, "priority", PERSISTENT_PRIORITY.get(old, old),
                          PERSISTENT_PRIORITY.get(new, new), task.name)
                task.el.set("priority", new)
                changed.append("priority")

        if "webLink" in fields and fields["webLink"] is not None:
            task.el.set("webLink", str(fields["webLink"]))
            changed.append("webLink")

        if "milestone" in fields and fields["milestone"] is not None:
            want = bool(fields["milestone"])
            if want:
                if task.children:
                    raise GanError(f"任务 #{task.id} 是汇总任务，不能设为里程碑")
                if task.duration != 0:
                    self._log("update", task.id, "duration", task.duration, 0, task.name)
                    task.set_duration(0)
                    changed.append("milestone")
            else:
                if task.duration == 0:
                    self._log("update", task.id, "duration", 0, 1, task.name)
                    task.set_duration(1)
                    changed.append("milestone")

        if "meeting" in fields and fields["meeting"] is not None:
            task.el.set("meeting", "true" if fields["meeting"] else "false")
            changed.append("meeting")

        if "parent" in fields and fields["parent"] is not None:
            self.move(task.id, fields["parent"])
            changed.append("parent")

        return {"task": task.id, "name": task.name, "changed": changed}

    def add(self, fields: dict) -> Task:
        name = fields.get("name")
        if not name:
            raise GanError("add 需要 name")
        parent_ref = fields.get("parent")
        after_ref = fields.get("after")
        before_ref = fields.get("before")

        parent_task = self.p.find(parent_ref) if parent_ref is not None else None

        start = None
        if fields.get("start"):
            start = self.p.cal.roll_forward(parse_iso_date(fields["start"], "start"))
        elif after_ref is not None:
            anchor = self.p.find(after_ref)
            if anchor.end_exclusive is None:
                raise GanError(f"参考任务 #{anchor.id} 没有结束日期")
            start = self.p.cal.roll_forward(anchor.end_exclusive)
        else:
            starts = [t.start for t in self.p.tasks if t.start]
            start = min(starts) if starts else date.today()

        duration = int(fields.get("duration", 1))
        milestone = bool(fields.get("milestone"))
        if milestone:
            duration = 0
        if duration < 0:
            raise GanError("工期不能为负")

        task_id = self.p.next_task_id()
        el = new_task_element(
            task_id, uuid.uuid4().hex, str(name), start, duration,
            int(fields.get("progress", fields.get("complete", 0)) or 0),
            parse_priority(fields["priority"]) if fields.get("priority") is not None else None,
            fields.get("notes"), milestone,
        )

        # placement
        if parent_task is not None:
            anchor_in_parent = None
            if after_ref is not None:
                candidate = self.p.find(after_ref)
                if candidate.parent is parent_task:
                    anchor_in_parent = candidate
            if anchor_in_parent is not None:
                container = parent_task.el
                container.insert(list(container).index(anchor_in_parent.el) + 1, el)
            else:
                parent_task.el.append(el)
        elif after_ref is not None:
            anchor = self.p.find(after_ref)
            container = self._container_of(anchor.el)
            container.insert(list(container).index(anchor.el) + 1, el)
        elif before_ref is not None:
            anchor = self.p.find(before_ref)
            container = self._container_of(anchor.el)
            container.insert(list(container).index(anchor.el), el)
        else:
            self.p.tasks_element().append(el)

        self.p.reindex()
        task = self.p.by_id[task_id]
        self._log("add", task_id, "task", None, f"{name} @ {fmt_date(start)}", name)

        # optional link to the anchor
        if after_ref is not None and fields.get("link", True):
            anchor = self.p.find(after_ref)
            dep_type = str(fields.get("type", "FS")).upper()
            insert_dep(anchor.el, task_id, dep_type, int(fields.get("lag", 0)),
                       fields.get("hardness", "Strong"))

        return task

    def _container_of(self, el: ET.Element) -> ET.Element:
        for candidate in self.p.root.iter():
            for child in list(candidate):
                if child is el:
                    return candidate
        raise GanError("无法定位任务节点")

    def delete(self, ref) -> int:
        task = self.p.find(ref)
        removed = 0
        for node in [task] + self._descendants(task):
            self.p.remove_dependencies_to(node.id)
            self.p.remove_allocations_of(node.id)
            parent_el = self._container_of(node.el)
            parent_el.remove(node.el)
            self._log("delete", node.id, "task", node.name, None, node.name)
            removed += 1
        self.p.reindex()
        return removed

    def _descendants(self, task: Task) -> list[Task]:
        out = []
        for child in task.children:
            out.append(child)
            out.extend(self._descendants(child))
        return out

    def move(self, ref, parent_ref) -> dict:
        task = self.p.find(ref)
        if str(parent_ref).lower() in ("none", "null", "root", ""):
            new_parent = None
        else:
            new_parent = self.p.find(parent_ref)
        if new_parent is task:
            raise GanError("不能把任务移动到自身之下")
        node = new_parent
        while node is not None:
            if node is task:
                raise GanError("不能把任务移动到自己的子任务之下")
            node = node.parent

        old_parent = task.parent
        old_el = self._container_of(task.el)
        old_el.remove(task.el)
        if new_parent is None:
            self.p.tasks_element().append(task.el)
        else:
            new_parent.el.append(task.el)
        self.p.reindex()
        self._log("move", task.id, "parent",
                  old_parent.name if old_parent else "(根)",
                  new_parent.name if new_parent else "(根)", task.name)
        return {"task": task.id}

    def depend(self, successor_ref, predecessor_ref, dep_type: str = "FS", lag: int = 0,
               hardness: str = "Strong") -> dict:
        successor = self.p.find(successor_ref)
        predecessor = self.p.find(predecessor_ref)
        if successor.id == predecessor.id:
            raise GanError("任务不能依赖自身")
        dep_type = str(dep_type).upper()
        if dep_type not in DEP_TYPES:
            raise GanError(f"依赖类型无效: {dep_type}（可用 SS/FS/FF/SF）")
        if successor.is_summary:
            self.warnings.append(f"#{successor.id} 是汇总任务，GanttProject 会给其所有子任务加隐式约束")
        insert_dep(predecessor.el, successor.id, dep_type, int(lag), hardness)
        self._log("depend", successor.id, "dependency",
                  None, f"{predecessor.id} -> {successor.id} {dep_type}+{lag}({hardness})",
                  successor.name)
        self.p.reindex()
        return {"successor": successor.id, "predecessor": predecessor.id,
                "type": dep_type, "lag": int(lag), "hardness": hardness}

    def undepend(self, successor_ref, predecessor_ref) -> bool:
        successor = self.p.find(successor_ref)
        predecessor = self.p.find(predecessor_ref)
        ok = remove_dep(predecessor.el, successor.id)
        if ok:
            self._log("undepend", successor.id, "dependency",
                      f"{predecessor.id} -> {successor.id}", None, successor.name)
        return ok

    def assign(self, task_ref, resource_ref, load: float = 100.0, responsible: bool = True) -> dict:
        task = self.p.find(task_ref)
        try:
            resource = self.p.find_resource(resource_ref)
        except GanError:
            resource = self.p.add_resource(str(resource_ref))
            self.warnings.append(f"已新建资源 “{resource['name']}”")

        alloc_el = self.p.root.find("allocations")
        if alloc_el is None:
            alloc_el = ET.SubElement(self.p.root, "allocations")
        for a in alloc_el.findall("allocation"):
            if str(a.get("task-id")) == str(task.id) and str(a.get("resource-id")) == str(resource["id"]):
                a.set("load", f"{float(load):.1f}")
                a.set("responsible", "true" if responsible else "false")
                self._log("assign", task.id, "allocation", None,
                          f"{resource['name']} {load}%", task.name)
                return {"task": task.id, "resource": resource["name"], "load": load}
        a = ET.SubElement(alloc_el, "allocation")
        a.set("task-id", str(task.id))
        a.set("resource-id", str(resource["id"]))
        a.set("function", resource.get("function") or "")
        a.set("responsible", "true" if responsible else "false")
        a.set("load", f"{float(load):.1f}")
        self._log("assign", task.id, "allocation", None, f"{resource['name']} {load}%", task.name)
        return {"task": task.id, "resource": resource["name"], "load": load}

    def unassign(self, task_ref, resource_ref) -> bool:
        task = self.p.find(task_ref)
        resource = self.p.find_resource(resource_ref)
        alloc_el = self.p.root.find("allocations")
        if alloc_el is None:
            return False
        for a in list(alloc_el.findall("allocation")):
            if str(a.get("task-id")) == str(task.id) and str(a.get("resource-id")) == str(resource["id"]):
                alloc_el.remove(a)
                self._log("unassign", task.id, "allocation", resource["name"], None, task.name)
                return True
        return False

    # -- batch ------------------------------------------------------------
    def run(self, spec: dict) -> None:
        project_meta = spec.get("project") or {}
        for key in ("name", "company", "webLink"):
            if key in project_meta and project_meta[key] is not None:
                old = self.p.root.get(key)
                if old != str(project_meta[key]):
                    self.p.root.set(key, str(project_meta[key]))
                    self._log("project", None, key, old, str(project_meta[key]))

        for raw in spec.get("ops", []):
            if not isinstance(raw, dict):
                raise GanError(f"无法识别的操作: {raw!r}")
            op = str(raw.get("op", "")).lower()
            if op in ("update", "set"):
                self.update(raw["task"], raw.get("fields") or raw)
            elif op == "add":
                self.add(raw)
            elif op == "delete":
                self.delete(raw["task"])
            elif op == "depend":
                self.depend(raw["task"], raw["predecessor"], raw.get("type", "FS"),
                            raw.get("lag", 0), raw.get("hardness", "Strong"))
            elif op == "undepend":
                self.undepend(raw["task"], raw["predecessor"])
            elif op == "assign":
                self.assign(raw["task"], raw["resource"], raw.get("load", 100),
                            raw.get("responsible", True))
            elif op == "unassign":
                self.unassign(raw["task"], raw["resource"])
            elif op == "move":
                self.move(raw["task"], raw["parent"])
            else:
                raise GanError(f"未知操作: {op}")

    def finish(self) -> None:
        if self.recalc:
            self.log.extend(
                {"op": "reschedule", **c} for c in self.p.reschedule()
            )
        for change in self.p.refresh_summaries():
            self.log.append({"op": "summary", **change})
        self.p.reindex()
        self.fix_view_date()

    def fix_view_date(self) -> None:
        """Move the chart viewport so the edited plan is visible when opened."""
        raw = self.p.root.get("view-date")
        starts = [t.start for t in self.p.tasks if t.start]
        if not raw or not starts:
            return
        try:
            view = parse_iso_date(raw)
        except GanError:
            return
        earliest = min(starts)
        latest = max((t.end_exclusive for t in self.p.tasks if t.end_exclusive), default=earliest)
        if view > latest or view < earliest - timedelta(days=60):
            self.p.root.set("view-date", earliest.isoformat())


# --------------------------------------------------------------------------
# GanttProject executable discovery / launching
# --------------------------------------------------------------------------

def find_ganttproject(explicit: str | None = None) -> Path:
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        if p.is_dir():
            for name in ("ganttproject.exe", "ganttproject.bat"):
                if (p / name).exists():
                    return p / name
        raise GanError(f"指定的 GanttProject 路径无效: {explicit}")

    for env in ("GANTPROJECT_EXE", "GANTPROJECT_HOME"):
        raw = os.environ.get(env)
        if raw:
            p = Path(raw)
            if p.is_file():
                return p
            if p.is_dir():
                for name in ("ganttproject.exe", "ganttproject.bat"):
                    if (p / name).exists():
                        return p / name

    roots = [Path(r"C:\Program Files (x86)"), Path(r"C:\Program Files"), Path(os.environ.get("LOCALAPPDATA", ""))]
    found: list[Path] = []
    for root in roots:
        if not root or not root.exists():
            continue
        try:
            for child in root.iterdir():
                if child.is_dir() and child.name.lower().startswith("ganttproject"):
                    exe = child / "ganttproject.exe"
                    if exe.exists():
                        found.append(exe)
        except OSError:
            continue
    if found:
        found.sort(key=lambda p: p.parent.name)
        return found[-1]

    which = shutil.which("ganttproject") or shutil.which("ganttproject.exe")
    if which:
        return Path(which)

    try:
        import winreg  # type: ignore

        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    key = winreg.OpenKey(hive, sub)
                except OSError:
                    continue
                for i in range(winreg.QueryInfoKey(key)[0]):
                    try:
                        name = winreg.EnumKey(key, i)
                        subkey = winreg.OpenKey(key, name)
                        display = str(winreg.QueryValueEx(subkey, "DisplayName")[0])
                        if "ganttproject" in display.lower():
                            location = str(winreg.QueryValueEx(subkey, "InstallLocation")[0])
                            exe = Path(location) / "ganttproject.exe"
                            if exe.exists():
                                return exe
                    except OSError:
                        continue
    except Exception:
        pass

    raise GanError("找不到 GanttProject。请用 --gp-path 指定安装目录，"
                   "或设置环境变量 GANTPROJECT_HOME")


def ganttprocesses() -> list[str]:
    try:
        out = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20)
    except Exception:
        return []
    names = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        name = line.split(",")[0].strip('"')
        if "gantt" in name.lower():
            names.append(name)
    return names


def launch(gan_path: Path, exe: Path, wait: int = 8, watch: bool = False,
           timeout: int = 21600) -> dict:
    log_path = Path.home() / "ganttproject.log"
    offset = log_path.stat().st_size if log_path.exists() else 0

    creationflags = 0
    if os.name == "nt":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        creationflags = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [str(exe), str(gan_path)],
        cwd=str(exe.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
        close_fds=True,
    )

    result = {"exe": str(exe), "file": str(gan_path), "pid": proc.pid, "warnings": []}
    if wait > 0:
        time.sleep(wait)
        result["alive"] = proc.poll() is None
        if log_path.exists():
            with log_path.open("rb") as fh:
                fh.seek(offset)
                tail = fh.read().decode("utf-8", errors="replace")
            bad = [
                line.strip() for line in tail.splitlines()
                if re.search(r"(Exception|ERROR|Error:|Failed to|Caused by)", line)
                and "jOOQ tip" not in line
            ]
            if bad:
                result["log_errors"] = bad[:8]

    if watch:
        # Holding this process alive also keeps the GanttProject window alive in
        # environments where child processes are reaped when the shell exits.
        deadline = time.time() + max(30, timeout)
        while proc.poll() is None and time.time() < deadline:
            time.sleep(2)
        result["watched"] = True
        result["closed_after"] = round(time.time() - (deadline - timeout))
    return result


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_table(project: Project, show_resources: bool = True) -> str:
    headers = ["ID", "任务", "开始", "结束", "工期", "进度", "前置"]
    if show_resources:
        headers.append("资源")
    rows = []
    for task in project.tasks:
        preds = task.predecessors()
        pred_txt = ", ".join(f"{p['predecessor']}{'' if p['type']=='FS' and p['lag']==0 else '('+p['type']+('+' if p['lag']>=0 else '')+str(p['lag'])+')'}" for p in preds)
        indent = "  " * task.depth
        marker = "◆ " if task.is_milestone else ("▸ " if task.is_summary else "")
        rows.append([
            str(task.id),
            indent + marker + re.sub(r"[\t\r\n]+", " ", task.name),
            fmt_date(task.start),
            fmt_date(task.display_end),
            str(task.duration),
            f"{task.complete}%",
            pred_txt,
            task.resource_names() if show_resources else "",
        ])

    widths = [disp_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], disp_width(cell))
    widths[1] = min(widths[1], 46)

    def line(cells):
        out = []
        for i, cell in enumerate(cells):
            text = truncate(cell, widths[i]) if i == 1 else cell
            out.append(pad(text, widths[i]))
        return "  ".join(out).rstrip()

    lines = [line(headers), "  ".join("-" * w for w in widths)]
    for task, row in zip(project.tasks, rows):
        lines.append(line(row))
    return "\n".join(lines)


def render_info(project: Project, as_json: bool) -> str:
    info = project.summary()
    if as_json:
        payload = dict(info)
        payload["task_list"] = [
            {
                "id": t.id,
                "name": t.name,
                "start": fmt_date(t.start),
                "end": fmt_date(t.display_end),
                "duration": t.duration,
                "complete": t.complete,
                "milestone": t.is_milestone,
                "summary": t.is_summary,
                "parent": t.parent.id if t.parent else None,
                "depth": t.depth,
                "resources": [a for a in t.assignments()],
                "predecessors": t.predecessors(),
                "successors": [d["successor"] for d in t.deps()],
            }
            for t in project.tasks
        ]
        return json.dumps(payload, ensure_ascii=False, indent=2)

    lines = [
        f"文件      : {info['file']}",
        f"项目      : {info['project'] or '(未命名)'}   {('公司: ' + info['company']) if info['company'] else ''}",
        f"版本/语言 : GanttProject {info['version']} / {info['locale'] or '-'}",
        f"格式      : {'ZIP(旧版)' if info['format']=='zip' else 'XML'}",
        f"周期      : {info['start']} → {info['end']}",
        f"任务      : {info['tasks']} 个（汇总 {info['summaries']}，里程碑 {info['milestones']}）",
        f"依赖      : {info['dependencies']} 条",
        f"资源      : {info['resources']} 个",
        f"日历      : {info['calendar']}",
        "",
        render_table(project),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def cmd_info(args) -> int:
    project = Project(args.file)
    if args.format == "json":
        print(render_info(project, True))
    else:
        print(render_info(project, False))
    return 0


def cmd_validate(args) -> int:
    project = Project(args.file)
    problems = project.validate()
    if args.format == "json":
        print(json.dumps([{"level": lv, "message": msg} for lv, msg in problems], ensure_ascii=False, indent=2))
    else:
        if not problems:
            print("✔ 校验通过：结构与依赖关系正常")
        else:
            for level, msg in problems:
                print(("✖ " if level == "error" else "⚠ ") + msg)
            errors = sum(1 for lv, _ in problems if lv == "error")
            print(f"\n共 {errors} 个错误，{len(problems) - errors} 个警告")
    return 1 if any(lv == "error" for lv, _ in problems) else 0


def _guard_running_app(path: Path, force: bool) -> None:
    procs = ganttprocesses()
    if procs and not force:
        print("⚠ 检测到 GanttProject 正在运行。若该文件已在软件中打开，"
              "请在软件里关闭（或不要保存）后再写入，否则软件内的旧内容会覆盖磁盘修改。", file=sys.stderr)


def load_spec(args) -> dict:
    if args.ops == "-":
        raw = sys.stdin.read()
        return json.loads(raw)
    with open(args.ops, "r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def cmd_apply(args) -> int:
    path = Path(args.file)
    _guard_running_app(path, args.force)
    project = Project(path)

    if args.ops:
        spec = load_spec(args)
    else:
        spec = {}
        fields = {}
        if args.name is not None:
            fields["name"] = args.name
        if args.start is not None:
            fields["start"] = args.start
        if args.duration is not None:
            fields["duration"] = args.duration
        if args.end is not None:
            fields["end"] = args.end
        if args.progress is not None:
            fields["progress"] = args.progress
        if args.notes is not None:
            fields["notes"] = args.notes
        if args.color is not None:
            fields["color"] = args.color
        if args.priority is not None:
            fields["priority"] = args.priority
        if args.milestone:
            fields["milestone"] = True
        if args.no_milestone:
            fields["milestone"] = False
        if args.parent is not None:
            fields["parent"] = args.parent
        if not fields:
            raise GanError("没有指定任何修改字段")
        spec["ops"] = [{"op": "update", "task": args.task, "fields": fields}]

    editor = Editor(project, recalc=not args.no_recalc)
    editor.run(spec)
    editor.finish()

    problems = project.validate()
    errors = [m for lv, m in problems if lv == "error"]

    if args.dry_run:
        print("=== 预演（未写入磁盘）===")
        _print_changes(editor.log)
        if errors:
            print("\n✖ 校验错误：")
            for msg in errors:
                print("  " + msg)
        return 1 if errors else 0

    if errors and not args.allow_errors:
        print("✖ 修改未写入，校验发现以下错误：", file=sys.stderr)
        for msg in errors:
            print("  " + msg, file=sys.stderr)
        return 2

    bak = project.save(backup=not args.no_backup)

    if args.format == "json":
        print(json.dumps({
            "file": str(path),
            "backup": str(bak) if bak else None,
            "changes": editor.log,
            "warnings": editor.warnings,
            "problems": [{"level": lv, "message": m} for lv, m in problems],
        }, ensure_ascii=False, indent=2))
    else:
        _print_changes(editor.log)
        if editor.warnings:
            print("\n提示：")
            for w in editor.warnings:
                print("  ⚠ " + w)
        if problems:
            print("\n校验：")
            for lv, msg in problems:
                print(("  ✖ " if lv == "error" else "  ⚠ ") + msg)
        print(f"\n✔ 已保存: {path}")
        if bak:
            print(f"  备份: {bak}")

    if args.open:
        exe = find_ganttproject(args.gp_path)
        launch(path, exe, args.wait)
        print(f"已用 GanttProject 打开（{exe}）")
    return 0


def _print_changes(log: list[dict]) -> None:
    if not log:
        print("（无实际变化）")
        return
    for item in log:
        name = item.get("name") or ""
        field = item.get("field")
        old = item.get("old")
        new = item.get("new")
        op = item.get("op")
        prefix = f"#{item['task']} {name}" if item.get("task") is not None else "项目"
        if op == "add":
            print(f"  + {prefix}: 新增（{new}）")
        elif op == "delete":
            print(f"  - {prefix}: 删除")
        elif op == "depend":
            print(f"  → {prefix}: 新增依赖 {new}")
        elif op == "undepend":
            print(f"  → {prefix}: 删除依赖 {old}")
        elif op == "reschedule":
            print(f"  ~ {prefix}: 开始日期 {old} → {new}（依赖联动）")
        elif field in (None, "task"):
            print(f"  * {prefix}: {old} → {new}")
        else:
            print(f"  * {prefix}: {field} {old} → {new}")


def cmd_open(args) -> int:
    path = Path(args.file)
    if not path.exists():
        raise GanError(f"文件不存在: {path}")
    exe = find_ganttproject(args.gp_path)
    if args.watch:
        print(f"正在打开 {path.name}，窗口关闭后本命令结束（最长 {args.timeout // 60} 分钟）…", flush=True)
    info = launch(path, exe, args.wait, watch=args.watch, timeout=args.timeout)
    print(f"GanttProject : {info['exe']}")
    print(f"项目文件      : {info['file']}")
    print(f"进程 PID      : {info['pid']}")
    if "alive" in info:
        print("状态          : " + ("已启动" if info["alive"] else "进程已退出（可能启动失败）"))
    if info.get("log_errors"):
        print("日志异常      :")
        for line in info["log_errors"]:
            print("  " + line)
    elif args.wait > 0:
        print("日志          : 未见加载异常")
    if info.get("watched"):
        print(f"窗口已关闭     : 持续 {info.get('closed_after', 0)} 秒")
    if info.get("log_errors") or info.get("alive") is False:
        return 1
    return 0


def cmd_where(args) -> int:
    try:
        exe = find_ganttproject(args.gp_path)
    except GanError as exc:
        print(str(exc))
        return 1
    print(f"GanttProject: {exe}")
    print(f"版本目录    : {exe.parent}")
    procs = ganttprocesses()
    print(f"运行中      : {', '.join(procs) if procs else '否'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gan.py",
        description="读取 / 修改 / 校验 / 打开 GanttProject (.gan) 项目文件",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="查看项目概况与任务表")
    p_info.add_argument("file")
    p_info.add_argument("--format", choices=["text", "json"], default="text")
    p_info.set_defaults(func=cmd_info)

    p_val = sub.add_parser("validate", help="校验文件结构与依赖")
    p_val.add_argument("file")
    p_val.add_argument("--format", choices=["text", "json"], default="text")
    p_val.set_defaults(func=cmd_validate)

    p_apply = sub.add_parser("apply", help="按 JSON 操作清单或单任务字段修改项目")
    p_apply.add_argument("file")
    p_apply.add_argument("--ops", help="JSON 操作清单路径，'-' 表示从标准输入读取")
    p_apply.add_argument("--task", help="单任务模式下要修改的任务（id 或名称）")
    p_apply.add_argument("--name")
    p_apply.add_argument("--start")
    p_apply.add_argument("--duration", type=int)
    p_apply.add_argument("--end")
    p_apply.add_argument("--progress", type=int)
    p_apply.add_argument("--notes")
    p_apply.add_argument("--color")
    p_apply.add_argument("--priority")
    p_apply.add_argument("--milestone", action="store_true")
    p_apply.add_argument("--no-milestone", action="store_true")
    p_apply.add_argument("--parent")
    p_apply.add_argument("--dry-run", action="store_true", help="只显示将发生的修改，不写盘")
    p_apply.add_argument("--no-backup", action="store_true")
    p_apply.add_argument("--no-recalc", action="store_true", help="不自动顺延后继任务")
    p_apply.add_argument("--allow-errors", action="store_true", help="即使校验出错也写入")
    p_apply.add_argument("--force", action="store_true", help="忽略 GanttProject 正在运行的提醒")
    p_apply.add_argument("--open", action="store_true", help="保存后用 GanttProject 打开")
    p_apply.add_argument("--gp-path")
    p_apply.add_argument("--wait", type=int, default=8)
    p_apply.add_argument("--format", choices=["text", "json"], default="text")
    p_apply.set_defaults(func=cmd_apply)

    p_open = sub.add_parser("open", help="用 GanttProject 打开项目文件")
    p_open.add_argument("file")
    p_open.add_argument("--gp-path")
    p_open.add_argument("--wait", type=int, default=8)
    p_open.add_argument("--watch", action="store_true",
                        help="保持命令运行直到 GanttProject 窗口关闭（沙箱环境中用于让窗口存活）")
    p_open.add_argument("--timeout", type=int, default=21600, help="--watch 的最长等待秒数")
    p_open.set_defaults(func=cmd_open)

    p_where = sub.add_parser("where", help="显示 GanttProject 安装位置")
    p_where.add_argument("--gp-path")
    p_where.set_defaults(func=cmd_where)

    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except GanError as exc:
        print(f"✖ {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"✖ 文件不存在: {exc.filename}", file=sys.stderr)
        return 2
    except PermissionError as exc:
        print(f"✖ 无权限访问: {exc.filename}（文件可能正被 GanttProject 占用）", file=sys.stderr)
        return 2
    except json.JSONDecodeError as exc:
        print(f"✖ 操作清单不是合法 JSON: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"✖ 文件操作失败: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
