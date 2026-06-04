# TODO: Schema 推断与五类信息组织管线

> 执行顺序：schema_partition → build_schema_unit → vocab_table

---

## 完成情况总览

| 条目 | 状态 |
|------|------|
| 共享类型 `schema_types.py` | ✅ 已完成 |
| `schema_partition.py`（partition_file，分片） | ✅ 已完成 |
| `schema_unit.py`（build_schema_unit，构建） | ✅ 已完成 |
| `vocab_table.py`（build_vocab_table，词汇表） | ✅ 已完成（`profile_similarity` 占位，见待完成项） |
| `extractor.py` 新增 `extract_all()` | ✅ 已完成 |
| `main.py` 写出新格式报告 | ✅ 已完成（schema_units.json / vocab_table.json） |
| 三个测试文件（126 用例） | ✅ 已完成，全部通过 |
| `extract_five_infos()` 改为薄包装 | ✅ 已完成（复用 extract_all 拍平为全局扁平视图，删除约 300 行重复 _iter 逻辑） |
| `profile_similarity()` 完整实现 | ⚠️ 占位版本（见待完成项 2） |
| 分片误 split 与分片阶段边界 | 🟡 部分（字段路径对齐随待完成项 5、CSV 整文件 load(3.2)已修；见待完成项 3，仅剩分桶判据误 split(3.1)、大 JSON 丢弃(3.3)未做，Tier B 方案已存档） |
| 分片策略现状 + `optional_field_grouping` 保留接口 | 📝 已存档（见待完成项 4，接口暂不实现） |
| SchemaUnit 五类信息双方案（A 折叠并集 / B 模板路径） | ✅ 已实现（见待完成项 5；`build_schema_unit(p, mode=...)`，CLI `--field-mode`，默认 B） |

---

## 待完成项

### 1. `extract_five_infos()` 改为 `extract_all()` 的薄包装

**文件**: `src/extract/extractor.py`

**当前状态**: `extract_five_infos()` 与 `extract_all()` 各自独立实现，逻辑重复。

**目标**: 将 `extract_five_infos()` 改为薄包装，复用 `extract_all()` 的结果：

```python
def extract_five_infos(tier1_grades: List[Grade]) -> Dict[str, Any]:
    """保留接口兼容性，内部调用 extract_all() 取 global_view 返回。"""
    _, _, global_view = extract_all(tier1_grades)
    # 将 global_view 格式转换为原有 extract_five_infos 输出格式
    return {
        "skeletons":            global_view.get("top_skeletons", {}),
        "shape_templates_B":    global_view.get("shape_templates_B", 0),
        "field_vocab":          {},   # global_view 不含 field_vocab，按需补充
        "naming_templates_A":   global_view.get("naming_templates_A", 0),
        "AB_ratio":             global_view.get("AB_ratio", 0.0),
        "value_profiles":       {},   # global_view 不含 per-path 画像，按需补充
        "topology":             {},
        "pii_seeds":            {},
        "total_records_sampled": global_view.get("total_records_sampled", 0),
    }
```

**注意**: 改动前需确认现有 49 个 `extract_five_infos` 相关测试仍通过。

---

### 2. `profile_similarity()` 完整实现

**文件**: `src/extract/vocab_table.py`

**当前占位逻辑**（仅 type + length）：
```python
def profile_similarity(p1, p2) -> float:
    # type 不同 → 0.0
    # type 相同有 len_dist → 0.5 + 0.5 × min/max 比
    # type 相同无 len_dist → 0.8
```

**目标**: 接入 `aggregate_profiles()` 已产出的 `avg_char_dist` 字段，做多维度加权相似度：

```python
def profile_similarity(p1: dict, p2: dict) -> float:
    """
    多维度加权:
      - type 不同 → 0.0（硬截断）
      - len_dist.mean 比值          权重 0.4
      - avg_char_dist 各维度差值    权重 0.4
        (digit_pct, alpha_pct, cjk_pct 各占 1/3)
      - top_patterns 重叠率          权重 0.2
    """
```

**前提**: `profile_value()` 已产出 `avg_char_dist` 和 `top_patterns`（当前已实现），
`aggregate_profiles()` 已将其聚合到 unit 级别的 value_profile 中。
改动仅在 `vocab_table.py` 内，不影响上游。

**阈值**: `_PROFILE_SIM_THRESHOLD = 0.7` 保持不变，需在 test_data/samples 上验证聚类效果。

---

### 3. 分片误 split 与分片阶段边界（remaining）

> 字段路径对齐（折叠模板路径、扇出消除、画像按折叠路径聚合、死代码清理）已由
> **待完成项 5** 实现完成（`build_schema_unit` 单遍折叠 + A/B 双方案）。本项只剩
> 下面三个**未做**的分片阶段问题。仅 JSON/JSONL 受 split 影响；CSV/SQL/SQLite 扁平无此问题。

#### 3.1 上游根因：`structure_signature` 作分桶 key 有损（未做）

`structure_signature` 既是骨架展示、又被 `_cluster_by_skeleton_json` / `_partition_jsonl`
当作**分桶 key**，两处脆弱：
- list 只取 `value[0]` → 漏 element[1+] 的字段类型；
- **对元素顺序敏感** → `[{a},{b}]` 与 `[{b},{a}]` 仅因首元素不同被劈进两桶（**误 split**）。

关键事实：分桶存的是**完整 rec**（非 value[0] 截断），所以真正损失是"同一 schema 被误 split
成多个 SchemaUnit、各看一个有偏子集"，**而非桶内丢字段**（fold 模式已能在桶内挖回元素级异构）。

- **实现思路**：把 signature 改成**元素顺序无关 + 跨元素取并集**（各元素签名排序后合并）→
  误 split 消失，且 `most_common == union`。
- **需拍板的权衡**：跨元素并集会把"本应拆成子 schema 的异构数组"（如
  `events:[{click},{purchase},{login}]`）并成一个"全 optional"超级 schema。按本项目目标
  （PII 抽取 + 画像）合并无害；若目标是"发现几种记录结构"则会抹掉子类型。动手前先定目标取向。

#### 3.2 CSV 抽取整文件 load 进内存（✅ 已修）

原 `_partition_csv._iter_records` 用 `content = f.read().replace("\x00","")` 整文件读进 RAM
再采样，违反"禁止整文件 load"。已改为**流式**：逐行剥 NUL 的生成器喂 `csv.DictReader`，
`islice` 到 `SAMPLE_PER_FILE`，内存降到 O(单行)，大文件读够即停。多行引号字段由 csv
跨行重组（`newline=""`），不受逐行影响；同时清理了不再需要的 `import io`。

#### 3.3 大体积 explicit-key JSON 静默丢弃（未做）

**问题**：`_detect_explicit_keys` 只 `json.loads` 前 64KB，文件 >64KB 解析失败 → 落
`_cluster_by_skeleton_json`，而 `ijson.items(f,"item")` 对顶层 object 不 yield 也不抛异常
（兜底 `json.load` 进不去）→ **0 partition，文件消失**。同时 explicit-key 检测受 64KB 上限。

##### 技术方案 Tier B：`ijson.parse` 前缀栈状态机（待实现，先存档）

**名词定义**
- **`ijson.parse(f)`**：流式产出三元组 `(prefix, event, value)`；`event` ∈
  {`start_map`,`end_map`,`start_array`,`end_array`,`map_key`,`string`,`number`,`boolean`,`null`}。
- **prefix（前缀）**：当前位置的点路径，**数组元素以 `.item` 表示**。
  顶层数组元素 prefix=`item`；顶层对象 key `users` 的数组元素 prefix=`users.item`。
- **前缀栈**：用 prefix 判定"当前事件落在 JSON 树何处"，据此 (a) 跟踪 `active_key`、
  (b) 识别"元素从哪开始"。**进入元素组装后不再看 prefix，只数嵌套 depth。**
- **状态机（状态变量 + 逐事件转移规则）**：
  - `root_kind`：`array`/`object`，由第一个事件（`start_array`/`start_map`）决定
  - `active_key`：对象根下当前顶层 key（遇 `('', map_key, K)` 更新）
  - `element_prefix`：元素起始前缀 = `"item"`（数组根）或 `f"{active_key}.item"`（对象根）
  - `collecting` / `builder`(`ijson.ObjectBuilder`) / `depth`：单个元素的组装状态
  - `buckets`：`{桶名 → record 列表}`，每桶 capped 到 `SAMPLE_PER_FILE`

**逐事件转移**
1. 第一个事件定 `root_kind`。
2. 未组装时：对象根遇 `('', map_key, K)` → 更新 `active_key`；遇 `(K, start_array, _)` 确认 K 是数组 key。
3. **元素开始**：`event==start_map 且 prefix==element_prefix` → 起 `ObjectBuilder`，`depth=1`，`collecting=True`。
4. **组装中**：每事件喂 builder；`start_*`→`depth+1`、`end_*`→`depth-1`；`depth==0` 时元素结束 →
   取 `builder.value` 路由。（只数 depth，元素内嵌套数组/对象不会误判结束。）
5. **路由（桶法分叉）**：对象根 → 桶=`active_key`（method=`explicit_key`）；
   数组根 → 桶=`"sig_"+hash(structure_signature(rec))`（method=`skeleton_cluster`，保持现状）。
6. 桶满 `SAMPLE_PER_FILE` 不再 append；数组根单桶满可直接 `break`。

**收益**：一举去掉 explicit-key 的 64KB 上限 + 顶层 object 静默丢弃，全程流式不构建整数组；
统一替代 `_detect_explicit_keys` + `_cluster_by_skeleton_json` + `_stream_json_records` 在 JSON 路径的职责。
**必须保留的退路**：ijson 默认按 UTF-8 读字节，**GBK** JSON 会抛错 → 整个路由包 `try/except`，
失败转文本 `json.load`/`json5`（带 encoding）。

##### 技术方案 Tier A：判根 + `ijson.kvitems`（更简单，推荐先上）

**名词定义**
- **判根**：读文件第一个非空白字节（`[` → 数组根；`{` → 对象根；其他 → 走文本兜底）。
  不必整文件 load，只 peek 头部。
- **`ijson.items(f, "item")`**：流式产出顶层数组的**每个元素**（逐个构建，**不**构建整数组）。
  数组根本就用它（`_cluster_by_skeleton_json`），未坏，保留。
- **`ijson.kvitems(f, "")`**：流式产出顶层对象的 `(key, value)` 对；其中每个 `value`
  在 yield 前会被**完整构建成 Python 对象**（这是与 Tier B 的本质差异）。

**处理逻辑**
1. **判根**：peek 首字节定 array / object。
2. **数组根**：完全保持现状——`ijson.items(f,"item")` 逐元素 + `structure_signature` 骨架分桶
   （method=`skeleton_cluster`）。本分支不改。
3. **对象根**：`kvitems(f, "")` 逐个取顶层 `(key, value)`：
   - `value` 是非空 `list` 且 `value[0]` 是 `dict` → 桶=key，取
     `[r for r in value[:SAMPLE_PER_FILE] if isinstance(r, dict)]`（method=`explicit_key`）；
   - 其他（值为标量/对象/标量数组）→ 忽略。
4. 这一步去掉 `_detect_explicit_keys` 的 64KB 上限（不再 `json.loads(head)`），
   并让顶层 object 不再静默丢弃。

**收益**：解决 3.3 两点（64KB 上限 + 顶层 object 丢弃），代码量比 Tier B 小一个数量级，
无需手工 `ObjectBuilder`/depth/前缀栈。
**代价与边界**：`kvitems` 对每个顶层 key 的 `value` **整体构建进内存** → 峰值内存 ≈ 最大顶层数组。
对"几 MB、多 key 的配置/导出型包装 JSON"够用；只有"单 key 挂 GB 级巨数组"才吃紧（而那种
通常是顶层数组/JSONL，走的是本方案未动、仍流式的 `ijson.items` 分支）。
**必须保留的退路**：同 Tier B——ijson 按 UTF-8 读字节，**GBK** JSON 抛错 → 转文本
`json.load`/`json5`（带 encoding），对 dict 仍按 `{key→list[dict]}` 取桶。

**Tier A vs Tier B 取舍**：A 简单、对象根整体构建（内存 = 最大顶层数组）；B 全程逐元素流式
（应付 GB 级单数组包装）但需前缀栈状态机。**建议先上 Tier A**，在对象根分支加日志（命中大文件
kvitems 全量构建时记一条），靠真实数据分布决定是否升级 Tier B。

---

### 4. 分片策略（schema_partition）现状与保留扩展接口

**文件**: `src/extract/schema_partition.py`

#### 4.1 当前实现：按范式分三条路径

| 格式 | 优先策略 | 兜底策略 | method |
|------|----------|----------|--------|
| **JSON/JSONL** | 检测"顶层 key → 同构数组"显式分区（如 `{users:[...], orders:[...]}`），命中则每个 key 直接作为一个 partition | 无显式分区时，按**骨架签名**（字段路径集合排序哈希，`structure_signature`）聚类，**签名严格相等**才归同一 partition | `explicit_key` / `skeleton_cluster` |
| **CSV/TSV** | 整文件单 partition | 列数标准差 > 0.5 或首行列数 < 2 → `noisy=True`（build_schema_unit据此跳过拓扑） | `single` |
| **SQL/SQLite** | 按 CREATE/INSERT 表名（SQLite 读 `sqlite_master`）分片，每表一个 partition | — | `table_name` |

- **JSON 显式分区**只 `json.loads` 前 64KB（`_detect_explicit_keys`），文件 >64KB 时该路径失效（见待完成项 3.5）。
- **骨架聚类**：`structure_signature` 对 list 只取 `value[0]`、且对元素顺序敏感 → 签名严格相等才同桶（见待完成项 3.4 的误 split 分析）。
- 每个 partition 的 `record_iter` 惰性流式，下游每桶最多采样 `SAMPLE_PER_FILE=1000` 条。

#### 4.2 保留接口（已设计，暂不实现）

```python
# ⚠ 保留接口（reserved，未实现）
def optional_field_grouping(records, field_freq):
    """共现频率分组 + 可选字段出现率建模。

    动机：当前"签名严格相等才同桶"会把仅因可选字段有无/数组元素顺序
          不同的同类记录拆散（误 split）。本接口拟改为按字段共现频率
          做软聚类，并对每个字段建模"出现率"，容忍可选字段的有无。
    输入：
      records    : 桶内（或文件内）记录迭代
      field_freq : {field_path: occurrence} —— 字段出现率映射
                   · occurrence = 该字段在分组记录中出现的比例 ∈ [0, 1]
                   · 含义：用于区分必填字段（occurrence >= 0.9）vs 可选字段
                   · 来源：由build_schema_unit SchemaUnit.fields[*].occurrence 汇出
                   · 现状：当前 occurrence 为占位符 1.0，真实值随本接口一并落地
    产出：更稳健的 partition 划分（可选字段不再导致 split）。
    """
```

- **与 occurrence 字段的关系**：`field_info.occurrence`（字段出现率，定义为区分必填
  `required>=0.9` vs 可选）的真正用武之地就是这个接口——把字段出现率回喂给分片层，
  按"可选字段可有可无"做软聚类。
- **现状**：occurrence 在原版及本轮 schema_unit 重构中**均保留占位符 `1.0`**（`required` 恒 True）；
  真实出现率的计算与本接口一并落地，**暂不实现**。

#### 4.3 后续扩展方向

1. **共现频率分组**：用字段共现矩阵替代"签名严格相等"，把同类但可选字段有无不同的记录聚到一起。
2. **可选字段出现率建模**：每个 partition 记录 `{field: occurrence}`，作为 schema 画像的一部分，也作为 `optional_field_grouping` 的输入。
3. 与待完成项 3.4（`structure_signature` 顺序无关 + 跨元素并集）协同：先解决误 split，再叠加共现软聚类。

---

### 5. SchemaUnit 五类信息提取：双方案（A 折叠并集 / B 模板路径）

> 详细方案讨论见对话；此处存档落地要点。两套方案**共用同一遍折叠遍历**，
> 差异仅在"字段主干路径集"的选取（B 的路径集 ⊆ A 的路径集）。

**共同基础（single-pass，决策 2）**：一遍遍历桶内记录，同时产出
(i) 逐记录 `structure_signature` 喂 Counter → 保住 `skeleton_count_B` / `skeleton_counts` / `AB_ratio`；
(ii) 折叠 leaf 路径的 `template_values`（元素级值聚合）+ `dtype_seen`（每路径类型计数，**null 不计入**，决策 3）。

**信息二·词汇表（决策 7）**：两套方案都用**折叠并集**派生 vocab（`{key_name: {折叠路径}}`），
组装 field_info **不再依赖 `build_vocabulary`**，而是直接遍历"字段主干路径集"读 `template_values`。

| 维度 | 方案 A：实际记录折叠并集 | 方案 B：原版模板 schema 路径（**默认**） |
|------|--------------------------|------------------------------|
| 字段主干 | 全部折叠 leaf 路径**并集**（含数组元素级异构、少数派可选字段） | 仅 `most_common` 签名展开的模板叶子路径（子集） |
| skeleton | 由并集派生；dtype = **最高频 + 多型标记** `(path, dtype, {multi_type, dominant_ratio})`（决策 1） | 沿用 `_most_common_skeleton_as_path_list`，单一 dtype |
| fields 主干 | union 全路径 | **仅签名主干路径**；值取主干路径对应聚合信息，**允许丢失非主干字段**（决策 6） |
| occurrence | 占位符 `1.0`（决策 5，定义不变，真值待 `optional_field_grouping`） | 同 A |
| topology | 折叠路径（主干=union），depth **仅按 `.` 深度**（`[]` 不计层级）：`depth = path.count(".") + 1` | **裁剪到主干签名路径**（与 fields 同子集） |
| vocab（信息二） | 不进 SchemaUnit；折叠路径经 `fields` 汇入**全局** VocabTable（vocab_table）。SchemaUnit 是单分片视图，全局词汇表才是 vocab 最终产物 | 同左（B 的全局 vocab 反映主干子集） |
| skeleton / fields | **分开保留**两个 key（决策 6 上一轮） | 分开保留 |
| 增量价值 | 找回数组元素级异构（`items[].b`）+ 少数派字段 | 干净的主导形状，字段集有界 |
| 取舍 | 异构数组被合并成"全 optional"超级 schema | 丢元素级异构 + 少数派字段（已接受） |

- **`structure_signature` 不动（决策 3 上一轮）**，仅改 schema_unit 内 skeleton 的产出方式。
- **暴露方式（决策 4）**：`build_schema_unit(partition, mode="template"|"fold")`，**默认 `template`（B 方案）**，
  `fold`（A 方案）经 flag 开启；`extract_all` 透传，CLI 加对应开关。
- **depth 口径**：`orders[].amt` → depth 2（只数 `.`），`tags[]` → depth 1，`meta.geo.lat` → depth 3。

---

## 1. 背景与现状（存档）

### 原始问题

`extract_five_infos(tier1_grades)` 将**所有** tier1 文件的 records 混合后做全局聚合：

```
tier1 file A ──┐
tier1 file B ──┼──► all_records (混合) ──► 一份全局五类信息 dict
tier1 file C ──┘
```

**问题**：无法追溯某条 field/skeleton 来自哪个文件/哪张表；SQL/SQLite 多表文件内部的表边界被抹平；后续 LLM 调用或人工审查时缺乏「这是哪张表」的上下文。

### 已完成的架构改造

```
tier1 grades
    │
    ▼ partition_file()                       src/extract/schema_partition.py
    │  按格式分片 → list[SchemaPartition]
    │  - JSON: 显式 key 检测 / 骨架聚类
    │  - CSV/TSV: 单 partition + noisy 标记
    │  - SQL/SQLite: 按表名分片
    │
    ▼ build_schema_unit()                    src/extract/schema_unit.py
    │  单遍折叠遍历组装五类信息 → list[SchemaUnit]
    │  - 全局唯一 ID: sch_NNNNN / f_NNNNN_NN
    │  - 骨架、拓扑、字段词汇、value 画像、PII 种子
    │
    ▼ build_vocab_table()                    src/extract/vocab_table.py
       三证据聚类 → VocabTable + uncertain_list
       - C: PII 类型一致
       - B: value 画像相似（占位，待完成项 2）
       - A: 字符串相似度校正
```

## 9. 全局约束（持续有效）

- **GB 量级**: 流式/抽样，禁止整文件载入；JSON 大数组用 ijson，JSONL 逐行，SQLite 每表 `LIMIT SAMPLE_PER_FILE`
- **中英混合**: chardet 探编码，decode `errors='replace'`
- **⚠ 绝不持久化原始 value**: `profile_value()` 立即丢弃输入值，只返回特征 dict
- **占位接口**: `profile_similarity()` 当前为最小化实现（type + length），待完成项 2 扩展，不影响整体管线正确性
- **向后兼容**: `extract_five_infos()` 保留，不破坏现有测试
- **ID 作用域**: SchemaUnit ID 和 field_id 在单次 pipeline 运行内自增，不跨 run 持久化
- **SQLite 只读**: `sqlite3.connect("file:path?mode=ro", uri=True)`
