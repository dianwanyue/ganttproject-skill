---
name: ganttproject
description: 读取、修改并用 GanttProject 打开 .gan 甘特图项目文件。当用户要求查看甘特图、调整任务名称/开始日期/工期/进度/依赖关系/资源分配、新增或删除任务、重排项目计划，或说“用 GanttProject 打开这个文件”时使用。This skill should be used whenever a GanttProject (.gan) file must be inspected, edited, validated, or opened in the desktop app.
---

# GanttProject 甘特图编辑

直接操作 GanttProject 的 `.gan` 项目文件（UTF-8 XML），完成任务的增删改、依赖与资源调整，保存后用 GanttProject 打开查看。

## 适用与不适用

适用：

- 查看某个 `.gan` 里有哪些任务、日期、工期、进度、依赖、负责人
- 修改任务名称、开始日期、工期、结束日期、完成度、备注、颜色、优先级、里程碑
- 新增 / 删除 / 移动任务，建立或取消依赖（SS/FS/FF/SF，可带 lag）
- 分配 / 取消资源，改项目名称与公司
- 保存副本并调用 GanttProject 打开确认

不适用：Microsoft Project 的 `.mpp` / MSPDI `.xml`、Excel/CSV 导入导出、GanttProject 的 PDF/PNG 导出。这些请用软件本身完成。

## 前置检查（每次任务开始时做一次）

1. 确认文件存在且是 `.gan`。默认位置：`~/Documents/GanttProject/*.gan`。
2. 确认 GanttProject 位置：
   ```bash
   python "<skill>/scripts/gan.py" where
   ```
   找不到时用 `--gp-path "C:\Program Files (x86)\GanttProject-3.3"` 指定，或设置 `GANTPROJECT_HOME`。
3. 确认软件没有正在编辑该文件：
   ```bash
   tasklist | grep -i -E "gantt|javaw"
   ```
   若 GanttProject 正开着同一文件，先让用户在软件里关闭（不要保存），否则软件内的旧内容会覆盖磁盘修改。

## 标准工作流

1. **读**：`info` 拿到任务 id、名称、日期、工期、进度、依赖。后续所有修改都用 id 定位，比名称可靠。
2. **写方案**：把修改写成 ops JSON（批量）或单任务参数。
3. **预演**：加 `--dry-run`，逐条核对 `旧值 → 新值` 以及依赖联动带来的日期变化。
4. **落盘**：去掉 `--dry-run`。脚本会自动备份为 `<原名>.bak-YYYYmmdd-HHMMSS`，原子写入。
5. **打开**：`open --watch`，窗口关闭后命令结束。

> 修改前务必先 `--dry-run` 给用户看变更清单，确认后再写盘，尤其是会触发行程顺延的工期/日期改动。

## 命令参考

脚本路径相对本 skill 目录：`scripts/gan.py`。所有命令都接受绝对路径，Windows 路径请加引号。

### 查看

```bash
python "<skill>/scripts/gan.py" info "D:\plan\project.gan"
python "<skill>/scripts/gan.py" info "D:\plan\project.gan" --format json
python "<skill>/scripts/gan.py" validate "D:\plan\project.gan"
```

`info` 输出项目概况 + 任务表（ID / 名称 / 开始 / 结束 / 工期 / 进度 / 前置 / 资源）。
`validate` 检查 id 重复、日期非法、依赖悬空/成环、资源分配悬空、汇总任务日期一致性。

### 单任务修改

```bash
python "<skill>/scripts/gan.py" apply "<file>" --task 3 --duration 30 --progress 50 --dry-run
python "<skill>/scripts/gan.py" apply "<file>" --task "AI分析模块" --start 2026-11-20
python "<skill>/scripts/gan.py" apply "<file>" --task 7 --end 2026-12-31
```

`--task` 接受 id（`3` 或 `#3`）或名称（精确匹配优先，其次唯一子串）。名称有歧义时会报错并列出候选 id。

可用字段：`--name --start --duration --end --progress --notes --color --priority --milestone --no-milestone --parent`。

### 批量修改（推荐）

```bash
python "<skill>/scripts/gan.py" apply "<file>" --ops ops.json --dry-run
python "<skill>/scripts/gan.py" apply "<file>" --ops ops.json
```

`--ops -` 从标准输入读取。

```json
{
  "project": {"name": "健身 App 开发计划", "company": "某某公司"},
  "ops": [
    {"op": "update", "task": 1, "fields": {"duration": 28, "progress": 40, "notes": "增加一周缓冲"}},
    {"op": "add", "name": "性能压测", "after": 6, "duration": 3},
    {"op": "add", "name": "阶段A", "start": "2026-10-01", "duration": 5},
    {"op": "add", "name": "子任务A1", "parent": "阶段A", "start": "2026-10-01", "duration": 3},
    {"op": "depend", "task": 5, "predecessor": 8, "type": "FS", "lag": 2, "hardness": "Strong"},
    {"op": "undepend", "task": 5, "predecessor": 6},
    {"op": "assign", "task": 3, "resource": "张三", "load": 100, "responsible": true},
    {"op": "unassign", "task": 3, "resource": "张三"},
    {"op": "move", "task": 4, "parent": 3},
    {"op": "delete", "task": 8}
  ]
}
```

### 打开

```bash
python "<skill>/scripts/gan.py" open "<file>" --wait 10
python "<skill>/scripts/gan.py" open "<file>" --wait 10 --watch
```

- `--wait`：启动后等待秒数，期间读取 `~/ganttproject.log` 的新增内容，检查是否有加载异常。
- `--watch`：保持命令运行，直到 GanttProject 窗口关闭。
- `--gp-path`：显式指定安装目录或 exe。

## 可修改字段

| 参数 / JSON 字段 | 写入位置 | 取值范围与说明 |
|---|---|---|
| `name` | `<task name>` | 任意文本 |
| `start` | `<task start>` | `YYYY-MM-DD`；非工作日会自动前滚到下一个工作日；若该任务带 `thirdDate` 约束会同步更新，避免 GanttProject 打开后被拉回 |
| `duration` | `<task duration>` | 整数，**工作日**；`0` 表示里程碑 |
| `end` | 换算为 `duration` | `YYYY-MM-DD`，按 GanttProject 界面显示的“结束日期”（含当天）反推工期 |
| `progress` / `complete` | `<task complete>` | 0–100 |
| `notes` | `<notes>` | 纯文本 |
| `color` | `<task color>` | `#rrggbb` |
| `priority` | `<task priority>` | `lowest / low / normal / high / highest` 或 0–4；XML 持久值为 3/0/1/2/4（GanttProject 的映射不是顺序的） |
| `milestone` | `<task duration>` | `true` → 工期设 0；`false` → 工期设 1；汇总任务不能设 |
| `parent` | 任务嵌套层级 | 任务 id / 名称；`null` 或 `root` 表示移到根层 |
| `after` / `before` | 同级插入位置 | 仅 `add` 使用；`after` 默认同时建立 FS 依赖，可用 `"link": false` 关闭 |
| `depend` | `<depend>` | `type`：`SS/FS/FF/SF`；`lag`：整数天；`hardness`：`Strong`/`Rubber` |
| `assign` / `unassign` | `<allocations>` | 资源按 id 或名称；资源不存在时自动新建 |
| `project.name/company/webLink` | `<project>` | 项目级元数据 |

**只读、会原样保留**：自定义列（`<customproperty>` 与 `tpd*` 定义）、视图列配置、基线（`<previous>`）、`<calendars>` 日历、角色集、费率、未知属性。修改不会破坏这些数据。

## 文件格式处理

- `.gan` 是单个 UTF-8 XML 文档，根节点 `<project>`；兼容旧版 ZIP 封装（自动读写其中的 `project.xml`）。
- 用 ElementTree 解析并回写：保留注释、未知元素、未知属性、元素顺序；输出为 4 空格缩进、无 BOM、`<?xml version="1.0" encoding="UTF-8"?>` 声明。
- 写入采用“临时文件 + `os.replace`”原子替换；默认先备份 `<原名>.bak-时间戳`（`--no-backup` 可关闭）。
- 任务 id 为项目内唯一整数，新增取 `max(id)+1`；`uid` 为 32 位十六进制 UUID。
- **依赖方向容易搞错**：`<depend>` 写在**前置任务**上，`id` 指向**后继任务**（GanttProject 保存时按“我作为前置”的关系写）。脚本已按其真实约定处理，直接调用 `depend` 即可。
- 工期按项目日历（`<calendars>` 的 `default-week`，0=工作日、1=休息日，外加节假日）计算工作日。
- 汇总任务的开始/工期由子任务推导，脚本会在修改后自底向上重算。
- 默认开启依赖联动：修改工期/日期后，按拓扑序把后继任务顺延到满足约束的位置（`--no-recalc` 关闭），与 GanttProject 调度器行为一致。
- 若 `view-date` 落在所有任务之外，会调整为项目最早开始日，避免打开后看不到图。

详细字段与算法见 `references/gan-format.md`。

## 调用与打开软件的行为

1. 定位可执行文件：`--gp-path` → 环境变量 `GANTPROJECT_EXE` / `GANTPROJECT_HOME` → `Program Files`、`Program Files (x86)`、`%LOCALAPPDATA%` 下的 `GanttProject-*` 目录 → `PATH` → 注册表 Uninstall 项。
2. 以安装目录为工作目录启动 `ganttproject.exe "<绝对路径>"`，无控制台窗口。
3. 等待 `--wait` 秒，检查进程是否存活，并读取 `~/ganttproject.log` 自启动后新增的内容；出现 `Exception` / `Failed to` 等行会打印出来。
4. 退出码：`0` 正常；`1` 启动后进程已退出或日志报错；`2` 参数/文件错误。
5. **窗口存活**：普通终端下窗口会一直存在。在受管 shell（如 WorkBuddy 的 Bash 工具）中，命令结束后子进程可能被回收，此时必须使用 `--watch` 并以**后台任务**方式运行，窗口才能保持到用户关闭为止。用户关窗后后台任务自动结束。

## 限制与注意事项

- 只做“文件级编辑 + 启动软件”，不模拟鼠标键盘操作菜单；需要界面内交互时让用户手动完成。
- 不运行 GanttProject 内置调度器，自己按依赖做顺延。汇总任务作为后继、带 `thirdDate` 约束、跨日历例外等复杂场景可能与软件内结果有细微差别，建议 `--dry-run` 后打开核对。
- 写盘前必须确保该文件没有在 GanttProject 中打开，否则软件保存时会覆盖。
- 不修改：资源费率、角色定义、日历、基线、自定义列定义；这些只原样保留。
- 依赖成环、id 重复、日期非法会阻止写入（`--allow-errors` 可强制）。
- 编辑功能跨平台；自动定位并启动 GanttProject 仅 Windows 验证过（macOS/Linux 需用 `--gp-path` 指定可执行文件）。
- 需要 Python 3.9+，只用标准库。

## 交付话术

修改后向用户说明：改了哪几个任务、哪些是因为依赖联动而顺延的、备份文件在哪、是否已用 GanttProject 打开。不要把整张任务表原样贴回，只列变更行。

## 关于与署名

- 目标软件：**GanttProject**（<https://www.ganttproject.biz/>），开源甘特图与项目管理桌面软件，GPL-3.0。本 Skill 是针对其 `.gan` 文件格式编写的**非官方**配套工具，不包含 GanttProject 源代码，与 GanttProject 项目及其维护者无隶属关系，也未获其背书。
- 本 Skill 由 **AI 生成**（WorkBuddy Agent，2026-11），作者负责需求定义、测试与验收；已在 Windows 11 + GanttProject 3.3.3316 实测。
- 以 MIT 许可证发布，可自由修改与再分发。
