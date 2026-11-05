# GanttProject Skill · 甘特图读写与打开

> 让 AI Agent 直接读、改、校验 GanttProject 的 `.gan` 项目文件，并在桌面端打开确认。

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](https://www.python.org/)
[![GanttProject](https://img.shields.io/badge/GanttProject-3.3-2f7d32.svg)](https://www.ganttproject.biz/)

面向 **GanttProject** 编写的非官方 AI Skill：不依赖 GUI 自动化，直接按文件格式读写项目，改完调用 GanttProject 打开查看。

## 特性

- **读**：项目概况、任务树、开始/结束日期、工期、进度、依赖、资源分配，支持文本表格与 JSON 输出
- **改**：任务名称、开始日期、工期、结束日期、完成度、备注、颜色、优先级、里程碑、层级
- **结构**：新增、删除、移动任务，支持父任务与同级插入
- **依赖**：SS / FS / FF / SF 四种类型，可带 lag 与 Strong/Rubber 硬度；修改后按拓扑序自动顺延后继任务
- **资源**：分配 / 取消，资源不存在可自动创建
- **安全**：`--dry-run` 预演、时间戳备份、原子写入、写前结构校验（id 重复 / 依赖成环 / 悬空引用）
- **打开**：自动定位 GanttProject 并用它打开文件，可选等待窗口关闭

## 安装

WorkBuddy（用户级，推荐）：

```bash
git clone https://github.com/dianwanyue/ganttproject-skill.git ~/.workbuddy/skills/ganttproject
```

项目级：克隆到 `<项目>/.workbuddy/skills/ganttproject`。
Claude Code：克隆到 `~/.claude/skills/ganttproject`。

依赖：

- Python 3.9+（只用标准库，无需 pip 安装）
- GanttProject 3.x。Windows 下自动定位安装目录；macOS/Linux 用 `--gp-path` 指定可执行文件

## 快速开始

```bash
# 1. 看项目里有什么
python scripts/gan.py info "plan.gan"

# 2. 预演一个修改
python scripts/gan.py apply "plan.gan" --task 3 --duration 30 --progress 50 --dry-run

# 3. 落盘（自动备份为 plan.gan.bak-YYYYmmdd-HHMMSS）
python scripts/gan.py apply "plan.gan" --task 3 --duration 30 --progress 50

# 4. 用 GanttProject 打开
python scripts/gan.py open "plan.gan" --wait 10
```

批量修改用 ops JSON（`--ops -` 可从标准输入读取）：

```json
{
  "project": {"name": "示例项目"},
  "ops": [
    {"op": "update", "task": 1, "fields": {"duration": 25, "progress": 35, "notes": "增加缓冲"}},
    {"op": "add", "name": "性能压测", "after": 6, "duration": 4},
    {"op": "depend", "task": 5, "predecessor": 8, "type": "FS", "lag": 1},
    {"op": "assign", "task": 3, "resource": "张三", "load": 80},
    {"op": "delete", "task": 12}
  ]
}
```

```bash
python scripts/gan.py apply "plan.gan" --ops examples/ops-example.json --dry-run
python scripts/gan.py apply "plan.gan" --ops examples/ops-example.json
```

## 命令一览

| 命令 | 作用 |
|---|---|
| `info <file> [--format json]` | 项目概况 + 任务表 |
| `validate <file>` | 结构、日期、依赖、资源分配校验 |
| `apply <file> --ops ops.json` | 批量修改 |
| `apply <file> --task 3 --duration 30` | 单任务修改 |
| `open <file> [--wait 10] [--watch]` | 用 GanttProject 打开 |
| `where` | 显示 GanttProject 安装位置与运行状态 |

`apply` 常用开关：`--dry-run` 预演、`--no-recalc` 不联动顺延、`--no-backup` 不备份、`--allow-errors` 跳过校验。

## 可修改字段

| 字段 | 说明 |
|---|---|
| `name` | 任务名称 |
| `start` | 开始日期 `YYYY-MM-DD`，非工作日自动顺延 |
| `duration` | 工期（**工作日**），`0` 表示里程碑 |
| `end` | 结束日期（按界面显示的含当天结束日反推工期） |
| `progress` / `complete` | 完成度 0–100 |
| `notes` | 备注 |
| `color` | 条形颜色 `#rrggbb` |
| `priority` | `lowest/low/normal/high/highest` 或 0–4 |
| `milestone` | 里程碑开关 |
| `parent` | 父任务（改层级） |

只读保留、不会被破坏：自定义列、视图列配置、基线、日历、角色、费率。

## 文件格式要点

- `.gan` 是单个 UTF-8 XML 文档（旧版 ZIP 封装会自动识别），根节点 `<project>`
- `<depend>` 写在**前置任务**上、`id` 指向**后继任务**——写反会让整个计划重排，脚本已按官方约定处理
- 工期按项目日历计算工作日（`default-week` 中 `0`=工作日、`1`=休息日）
- 汇总任务的日期由子任务推导，修改后自动重算
- 写回时保留未知元素与属性，输出无 BOM、4 空格缩进

细节见 [`references/gan-format.md`](references/gan-format.md)。

## 已知限制

- 只做文件级编辑，不做 GUI 自动化（不点菜单、不拖拽）
- 不运行 GanttProject 内置调度器，自己按依赖顺延；汇总任务作后继、跨日历例外等复杂场景可能有细微差异
- 编辑时文件不能在 GanttProject 中打开，否则软件保存会覆盖磁盘修改
- 仅支持 `.gan`，不支持 `.mpp` / MSPDI
- 自动定位并启动 GanttProject 仅 Windows 验证；其他平台用 `--gp-path`

## 关于 GanttProject

[GanttProject](https://www.ganttproject.biz/) 是一款开源的甘特图与项目管理桌面软件，采用 **GPL-3.0** 许可证，支持 Windows / macOS / Linux。

本项目是针对其文件格式独立编写的**非官方**配套工具，不包含 GanttProject 的源代码，与 GanttProject 项目及其维护者无隶属关系，也未获其背书。"GanttProject" 名称及相关标识归其作者所有。

## AI 生成声明

本仓库的 Skill 说明、Python 脚本与参考文档均由 **AI 生成**（WorkBuddy Agent，2026-11），由仓库作者定义需求、组织测试与验收。

已在 Windows 11 + GanttProject 3.3.3316 实测：读取、批量修改、校验、保存、调用软件打开，并用 GanttProject 二次保存后逐节点比对——除列宽外内容完全一致。

AI 生成内容可能存在错误。正式使用前请先 `--dry-run` 核对变更，并保留脚本自动生成的备份。

## License

[MIT](LICENSE)
