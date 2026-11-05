# GanttProject `.gan` 文件格式参考

基于 GanttProject 3.3.3316（Windows 安装包自带 `HouseBuildingSample.gan`）实测，并对照官方源码 `TaskSaver.kt`、`GanttXMLSaver.java`、`ConstraintType.java`、`Task.java` 整理。

## 1. 容器与总体结构

`.gan` 是一个 UTF-8 纯文本 XML 文件（无 BOM），根节点 `<project>`。旧版本可能保存为 ZIP 包（内含 `project.xml`）；读取时按 `PK\x03\x04` 魔数判断并透明处理。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<project name="项目名" company="" webLink="http://" view-date="2026-08-01"
         view-index="0" gantt-divider-location="403" resource-divider-location="300"
         version="3.3.3316" locale="zh_CN">
  <description/>
  <view id="gantt-chart">…列配置、筛选、缩放…</view>
  <view id="resource-table">…</view>
  <!-- -->
  <calendars>…工作日/节假日…</calendars>
  <tasks empty-milestones="true">
    <taskproperties>…列定义…</taskproperties>
    <task …>…</task>
  </tasks>
  <resources>…</resources>
  <allocations>…</allocations>
  <vacations/>
  <previous/>          <!-- 基线 -->
  <roles roleset-name="Default"/>
</project>
```

顶层子元素顺序固定为上图顺序；修改脚本会保持原有顺序，只替换数值。

## 2. 根节点属性

| 属性 | 含义 | 备注 |
|---|---|---|
| `name` / `company` / `webLink` | 项目元数据 | 可改 |
| `view-date` | 甘特图视口左边界日期 | 修改后若落在任务范围外，脚本会重置为最早开始日 |
| `view-index` | 当前视图索引 | 保持 |
| `gantt-divider-location` / `resource-divider-location` | 界面分隔条位置 | 保持 |
| `version` | 写入该文件的 GanttProject 版本 | 保持（不要伪造成更高版本） |
| `locale` | 界面语言，如 `zh_CN` | 保持 |

## 3. 任务

任务以嵌套方式表达层级：子任务写在父 `<task>` 内部。

```xml
<task id="3" uid="5421ee20b2ca4e389a7cff1354238baf" name="AI分析模块"
      color="#8cb6ce" meeting="false" start="2026-11-09" duration="21"
      complete="0" priority="2" thirdDate="2026-11-09" thirdDate-constraint="1"
      expand="true">
  <notes>备注文本</notes>
  <depend id="4" type="2" difference="0" hardness="Strong"/>
  <task id="10" …>子任务</task>
</task>
```

### 3.1 属性表

| 属性 | 类型 | 说明 |
|---|---|---|
| `id` | int | 项目内唯一，自增。新增任务取 `max(id)+1` |
| `uid` | 32 位 hex | 跨文件唯一标识，`uuid4().hex` |
| `name` | string | 任务名 |
| `color` | `#rrggbb` | 条形颜色，可省略 |
| `meeting` | bool | 旧版里程碑标记（`isLegacyMilestone`）。普通任务写 `false` |
| `start` | `YYYY-MM-DD` | 允许带时间 `YYYY-MM-DDThh:mm`（启用时间管理时） |
| `duration` | int | **工作日**天数；`0` 在 `empty-milestones="true"` 下表示里程碑 |
| `complete` | 0–100 | 完成百分比 |
| `priority` | string | 持久值见下表；等于默认值时可省略 |
| `thirdDate` | 日期 | “最早开始/必须开始于”约束日期 |
| `thirdDate-constraint` | int | 约束类型；脚本在移动任务时同步 `thirdDate`，避免被调度器拉回 |
| `expand` | bool | 树是否展开 |
| `cost-manual-value` / `cost-calculated` | 数值/bool | 成本 |
| `project` | bool | 标记项目级任务（少见） |
| `shape` | 字符串 | 自定义条形形状（少见） |

### 3.2 优先级持久值（非顺序！）

来源：`Task.Priority` 枚举。

| 枚举 | 含义 | XML `priority` |
|---|---|---|
| LOWEST | 最低 | `3` |
| LOW | 低 | `0` |
| NORMAL | 普通（默认） | `1` |
| HIGH | 高 | `2` |
| HIGHEST | 最高 | `4` |

脚本对外接受 `lowest/low/normal/high/highest` 或序号 0–4（0=最低），内部转换成上表持久值。

### 3.3 里程碑

- 3.x 没有 `milestone="true"` 属性；当 `<tasks empty-milestones="true">` 时，`duration="0"` 即渲染为里程碑。
- `meeting="true"` 是历史遗留的“会议/里程碑”标记，保留原值即可。
- 汇总任务（有子任务）不能是里程碑。

### 3.4 自定义列

```xml
<taskproperties>
  <taskproperty id="tpd7" name="completion" type="default" valuetype="int"/>
  <taskproperty id="tpc0" name="Is done" type="custom" valuetype="boolean" defaultvalue="false">
    <simple-select select="completion=100"/>
  </taskproperty>
</taskproperties>
…
<customproperty taskproperty-id="tpc0" value="false"/>
```

定义在 `<taskproperties>`，取值在各任务的 `<customproperty>`。**脚本不修改、不删除这些节点**，新增任务不会自动继承自定义列的值（GanttProject 会用默认值）。

## 4. 依赖（最容易写反的地方）

```xml
<depend id="3" type="2" difference="14" hardness="Strong"/>
```

关键语义（源码 `TaskSaver.writeTask`：`task.dependenciesAsDependee` 遍历后写 `next.dependant.taskID`）：

> `<depend>` 写在**前置任务（dependee）** 上，`id` 指向**后继任务（dependant）**。

即：A 的 `<depend id="B">` 表示「B 依赖 A」，不是「A 依赖 B」。用官方 `HouseBuildingSample.gan` 可验证：任务 1「Foundation building」工期 15、2024-07-01 开始（按 5 天工作周结束于 07-19，独占结束日 07-22），其后继任务 2「Ground Floor building」正好 07-22 开始，而 `<depend id="2">` 写在任务 1 里。

| `type` | 含义 | 约束表达式（`difference` = lag） |
|---|---|---|
| `1` | SS 开始-开始 | `后继.start = 前置.start + lag` |
| `2` | FS 完成-开始 | `后继.start = 前置.end + lag` |
| `3` | FF 完成-完成 | `后继.end = 前置.end + lag` |
| `4` | SF 开始-完成 | `后继.end = 前置.start + lag` |

- `end` 为**独占结束日**：`start + duration 个工作日`。界面上显示的“结束日期”是 `end - 1 个工作日`。
- `lag`（GanttProject 里叫 difference）单位为天，脚本按工作日推进。
- `hardness`：`Strong` 表示等号（强约束），`Rubber` 表示 `>=`（松约束，只往后推不往前拉）。
- 依赖不能成环，不能指向自身。GanttProject 不允许从汇总任务建依赖，但文件里可能存在历史数据，脚本仅告警。

## 5. 工期与日历

```xml
<calendars>
  <day-types>
    <day-type id="0"/><day-type id="1"/>
    <default-week id="1" name="default" sun="1" mon="0" tue="0" wed="0" thu="0" fri="0" sat="1"/>
    <only-show-weekends value="false"/>
    <overriden-day-types/>
    <days>
      <day date="2026-10-01" type="HOLIDAY"/>
    </days>
  </day-types>
  <date year="2026" month="10" date="1" type="HOLIDAY"/>   <!-- 2.x 兼容写法 -->
</calendars>
```

- `default-week` 的属性值：`0` = 工作日，`1` = 休息日（注意与直觉相反）。
- 节假日：`type="HOLIDAY"`；补班日：`type="WORKING_DAY"`。
- `duration` 只计工作日；`start` 落在休息日时，GanttProject 会顺延到下一个工作日（脚本同样处理）。

### 汇总任务

有子任务的 `<task>` 是汇总任务，其 `start` 和 `duration` 由子任务推导：

```
start    = min(子任务.start)
end      = max(子任务.end)            # 独占结束日
duration = 工作日数(start, end)
```

脚本在每次修改后自底向上重算，保证与软件显示一致。GanttProject 2.8+ 界面不允许直接编辑汇总任务的日期。

## 6. 资源与分配

```xml
<resources>
  <resource id="0" name="张三" function="Default:1" contacts="" phone="13800138000">
    <rate name="standard" value="10"/>
  </resource>
</resources>
<allocations>
  <allocation task-id="0" resource-id="0" function="Default:1" responsible="true" load="90.0"/>
</allocations>
```

- `function` 是角色引用（`角色集:角色id`），新建资源时若留空，GanttProject 视为“默认角色”。
- `responsible="true"` 表示负责人（界面上名字后带 `*`）。
- `load` 为分配百分比。
- 删除任务时必须同时删除其 `<allocation>`，脚本已处理。

## 7. 视图与基线（只读）

```xml
<view zooming-state="default:8" id="gantt-chart">
  <field id="tpd3" name="名称" width="228" order="0"/>
  <option id="filter.completedTasks" value="false"/>
  <timeline><![CDATA[0]]></timeline>
</view>
```

- `<field>` 的 `id` 取自 `<taskproperty>`（`tpd*`）或自定义列（`tpc*`）。
- `<previous>` 保存基线快照，结构复杂，脚本原样保留。
- `<vacations>` 为资源休假，脚本不修改。

## 8. 已知坑

1. **依赖方向**：见第 4 节，写反会导致打开后所有任务被重排。
2. **`difference` 可能是历史残留**：手工拖动过的任务，其 lag 与实际日期间可能不严格相等；以 `start` 为准，lag 仅在顺延时使用。
3. **`thirdDate-constraint`**：带“最早开始”约束的任务若只改 `start` 不改 `thirdDate`，打开时会被调度器拉回；脚本会同步。
4. **周末/节假日**：直接写入休息日的 `start` 会被 GanttProject 修正，脚本预先顺延避免差异。
5. **并发覆盖**：GanttProject 打开文件后只在保存时写盘；若文件同时被脚本修改，软件内的旧内存会覆盖磁盘。写前确认已关闭。
6. **自定义列**：新增任务不写 `<customproperty>`，GanttProject 使用列定义的 `defaultvalue`；`<simple-select>` 类型的列会按公式自动计算。
7. **编码**：必须 UTF-8 无 BOM，声明为 `UTF-8`；写入中文备注不要转义成数字实体（ElementTree 默认行为即可）。
