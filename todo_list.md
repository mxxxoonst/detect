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
| 测试套件（当前 165 用例） | ✅ 已完成，全部通过 |
| `extract_five_infos()` 改为薄包装 | ✅ 已完成（复用 extract_all 拍平为全局扁平视图，删除约 300 行重复 _iter 逻辑） |
| 全流水线日志埋点 + `pii_detect` 根命名空间 + `-v` | ✅ 已完成（详见 docs/guides.md §7） |
| 分片阶段 JSON 容错对齐（编码 + json5） | ✅ 已完成（`_stream_json_records(path, encoding)` 兜底，修掉 GBK/JSON5 文件零产出；见待完成项 3.3 说明） |
| 阶段间落盘流式 + 断点续跑（内存恒定） | ✅ 已完成（`grades.jsonl` / `schema_units.jsonl` 流式追加，CLI 走流式版 `stream_schema_units`+`finalize_from_units`，`--restart` 强制重来；见待完成项 6） |
| `profile_similarity()` 完整实现 | ⚠️ 占位版本（见待完成项 2） |
| 分片误 split 与分片阶段边界 | 🟡 部分（字段路径对齐随待完成项 5、CSV 整文件 load(3.2)已修、编码/JSON5 容错已修；见待完成项 3，仅剩分桶判据误 split(3.1)、64KB explicit-key 上限+大顶层 object 丢弃(3.3)未做，Tier A/B 方案已存档） |
| 分片策略现状 + `optional_field_grouping` 保留接口 | 📝 已存档（见待完成项 4，接口暂不实现） |
| SchemaUnit 五类信息双方案（A 折叠并集 / B 模板路径） | ✅ 已实现（见待完成项 5；`build_schema_unit(p, mode=...)`，CLI `--field-mode`，默认 B） |
| `value_profile.py` 重构（MECE 宏桶 + 脚本直方图 + 同源 pattern + 样本保留 flag） | ✅ 已完成（单趟 unicodedata 7 宏桶 + 8 脚本直方图 + 同源 pattern；样本默认关，`--keep-samples`/`--mask-samples` 显式开） |
| 签名基数爆炸：根因 / 无损收敛 / B 聚类近线性化 | 📝 已存档（见待完成项 7，串起 2 / 3.1 / 4，含 Pass2 卡死诊断与落地顺序） |

---

## 待完成项

### 1. `extract_five_infos()` 改为 `extract_all()` 的薄包装 ✅ 已完成

`extract_five_infos()` 现为 `extract_all()` 的薄包装：跑新管线后把每个 SchemaUnit
（折叠路径）拍平成旧的全局扁平五类信息 dict（无溯源，新代码勿用，见 docs/guides.md §6）。
已删约 300 行重复 `_iter` 逻辑，测试全通过。

---

### 2. `profile_similarity()` 完整实现

> ⚠ **本项的真实度量 + 近线性化已并入待完成项 7.3**（含分块/LSH 把 O(n²) 降到近线性后才真正
> 打开 B 证据）。此处仅留"占位现状 + 致命错配"的速记，详细落地见 §7.3。

**文件**: `src/extract/vocab_table.py`

**当前占位逻辑（且已错配）**：`profile_similarity` 读 `p.get("type")`，
但 `aggregate_profiles()` 聚合后的 value_profile **根本没有 `type` 键**（那是单值
`profile_value` 的键；聚合用的是 `len_dist` / `avg_char_dist` / `top_patterns` / `avg_scripts`）。
→ **B 证据恒返回 0.0**，`_initial_clusters_by_bc` 的 O(n²) 两两比既算错又是纯无用功（见 §7.1(b)）。

**目标**: 用重构后的 `aggregate_profiles()` 字段做多维度加权（字段名已更新）：

```python
def profile_similarity(p1: dict, p2: dict) -> float:
    """
    多维度加权:
      - dtype 门 (取自 len_dist 是否存在 / top_patterns) 不一致 → 0.0
      - len_dist.mean / std 接近度                         权重 0.4
      - avg_char_dist 7 宏桶向量余弦 / 1−JSD               权重 0.4
        (number/letter/mark/punct/symbol/space/other; MECE 和=1, 是合法概率分布)
      - top_patterns 两集合 Jaccard                        权重 0.2
      - (可选) avg_scripts 直方图距离
    """
```

**前提**: `aggregate_profiles()` 已产出 `avg_char_dist`(7 宏桶) / `avg_scripts` / `top_patterns` /
`len_dist`（均已实现）。改动仅在 `vocab_table.py` 内，不影响上游。
**阈值**: `_PROFILE_SIM_THRESHOLD = 0.7` 保持不变，需在 test_data/samples 上验证聚类效果。
**前置**: 必须先做 §7.3 的分块/LSH（否则 O(n²) 跑不动），并先用 §7.4 ① 的护栏止血。

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

#### 3.3 大体积 explicit-key JSON 静默丢弃（部分已修）

> **已修（本轮）**：编码与 JSON5 语法导致的零产出。`_stream_json_records(path, encoding)` 和
> `_detect_explicit_keys` 已补「按 `grade.encoding` 读文本 → json 严格 → json5 容错」兜底，
> 修掉 **GBK 编码** JSON（ijson 硬走 UTF-8 抛错）与 **JSON5 脏数据**（注释/单引号/尾逗号）
> 在分片阶段静默零产出的问题。详见 docs/guides.md §4.2。
>
> **仍未做**：下面的 **64KB explicit-key 上限 + 大顶层 object 丢弃**——属于"大体积/结构"维度，
> 与编码/语法正交，需 Tier A/B 流式状态机方案。

**问题**：`_detect_explicit_keys` 只读前 64KB，文件 >64KB 时显式 key 检测失效 → 落
`_cluster_by_skeleton_json`，而 `ijson.items(f,"item")` 对顶层 object 不 yield 也不抛异常
（容错兜底虽能进，但对 GB 级大对象仍需整体构建）→ 大体积包装 object 风险。同时 explicit-key 检测受 64KB 上限。

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

### 6. 阶段间落盘流式 + 断点续跑 ✅ 已完成（含残留方向）

**动机**：原 CLI（`extract`/`pipeline`）把全部 `tier1_grades` 收进 list 再交 `extract_all`，
且 `extract_all` 内部把 `all_partitions` + `schema_units`（五类信息全集）全量驻留内存、
末尾 `_save_output(schema_units, "schema_units.json")` 整体序列化。**46000 文件 / GB 级下内存爆**，
且全程无中间落盘 → OOM/网络/其它中断 = 已解析信息全丢、无法续跑。

**已落地**：
- 阶段1 `_stream_grades`：逐文件 sniff+grade → **即时追加** `grades.jsonl`，内存恒定 O(1)。
- 阶段2 `stream_schema_units`（extractor.py）：流式读 `grades.jsonl`(tier1) → 逐文件 partition→build_unit
  → **即时追加** `schema_units.jsonl`；`finalize_from_units` 两遍流式聚合 global_view + vocab_table。
- **断点续跑**：`grades.jsonl` 按 path 跳过已处理；`schema_units.jsonl` 按 `source_file` 跳过，
  ID 计数器 `set_unit_counter(max_seq+1)` 续接；崩溃截断尾行由 `iter_jsonl` 容错跳过；`--restart` 强制重来。
- `pipeline` 的 phase0 交叉表改为从 `grades.jsonl` 派生，省去原 `profile_corpus` 的额外整轮嗅探。
- 关键事实：`partition_file` 只用 `grade.path/fmt/encoding` 并从磁盘重读原文件，故阶段间只需传轻量行
  （`grade_from_summary` 重建最小 Grade），无需完整 Grade/parsed 负载。

**残留方向（未做）**：
1. **vocab 聚类仍 O(字段条目数)内存**：`finalize_from_units` 第二遍把所有 KeyEntry 收进内存做并查集聚类。
   46000 文件 × 平均字段数可能上百万条目（每条含 value_profile 小 dict），峰值仍可观。
   未来可考虑：value 画像做 LSH/分桶外部化，或按语义类分片聚类，避免全量 KeyEntry 驻留。
2. **`schema_units.jsonl` 体量**：每分片一行全量五类信息，超大语料文件本身会很大；
   如需可加分卷（按文件数滚动）或压缩（gzip 行）。
3. **续跑粒度为"文件级"**：单文件内分片中途崩溃会整文件重做（幂等，无错但有重复计算）；
   当前可接受，若单文件巨大可细化到分片级 checkpoint。

---

### 7. 签名基数爆炸：根因、无损收敛与 B 聚类近线性化

> 本项汇总两轮讨论，串起三个已存在的散点（待完成项 2 / 3.1 / 4），给出
> 「为什么阶段2会卡死 + 怎么治本（不丢信息）」的完整链路。

#### 7.1 根因诊断：尾段 unit 暴涨 + Pass2 卡死

实测日志：
```
阶段2 进度: 已处理 3800 文件, 累计写出 21601 unit   (正常 ~5.7 unit/文件)
阶段2 Pass1 完成: 处理 3926 文件(跳过 17), 新写出 79346 unit
                  ↑ 尾段 126 文件 +57745 unit ≈ 458 unit/文件 (~80×)
随后终端静默, 程序无法继续。
```

两个独立病因：

**(a) 签名基数爆炸（写出端）**：`structure_signature` 被 `_cluster_by_skeleton_json` /
`_partition_jsonl` 当分桶 key，「签名严格相等才同桶」。但签名会因下面这些**非本质差异**裂开——
它们都是「同一张逻辑表 + 稀疏/脏数据」，**不是不同 schema**：

| 裂开诱因 | 例 | 后果 |
|----------|-----|------|
| 可选字段在/不在 | `{a,b}` vs `{a,b,c}` | k 个可选字段 → 最多 **2ᵏ** 签名 |
| 可空字段 | 某字段 `<str>` vs `<null>` | ×2 签名 |
| 类型漂移 | `<int>`/`<float>`/`<str>` 互换 | 各 1 签名 |
| 空列表 vs 有元素 | `[]` vs `[<str>]` | ×2 签名 |

一个 12 可选字段的记录类型，单文件就能炸出 ~4096 签名 = ~4096 unit。尾段 458 unit/文件正是
这种组合噪声，**不是真有 458 种 schema**。（顺序敏感导致的误 split 见待完成项 3.1，与此正交叠加。）

**(b) O(n²) 空转 + 无日志（聚合端，卡死的直接原因）**：`vocab_table.py:_initial_clusters_by_bc`
的 B 证据是**全字段两两比**（`for i: for j>i`），entry 数 = 全语料字段条目（数十万～百万级），
O(n²) 直接卡死；且循环内**无任何日志** → 终端静默。雪上加霜：`profile_similarity` 读
`p.get("type")`，而**聚合后的 value_profile 根本没有 `type` 键** → B 恒返回 0.0 → 这个 O(n²)
既**算错**又是**纯无用功**（详见待完成项 2）。

#### 7.2 无损收敛：折叠噪声，而非砍长尾

top-K 截断签名桶是**有损**的（长尾里可能藏稀有但带 PII 的字段），不可取。更好的杠杆是把
7.1(a) 那些非本质差异**折叠掉**——对字段信息**无损**，因为它们本就不是不同 schema。

关键事实：形状多样性**已被** `skeleton_count_B` / `skeleton_counts`（逐记录签名计数，top-50）
以统计量记住。「按签名切 partition」与 `skeleton_counts` **重复**，而它恰是爆炸源。

做法（JSON/JSONL）：**不再按 exact signature 切 partition，改每文件一个 partition**（显式包装
key 的真表拆分保留），`build_schema_unit` 走 **fold 模式**（已实现，见待完成项 5）：

| 原靠「多 unit」表达 | fold 折叠后无损保留于 |
|---------------------|----------------------|
| 每条记录精确形状 | `skeleton_counts`（top-50 形状 + 计数） |
| 形状种类数 | `skeleton_count_B` |
| 字段在/不在（可选性） | 每路径 `occurrence` |
| 类型漂移 | `multi_type` + `dominant_ratio` |
| **每个字段路径**（含稀有可选字段） | backbone = 折叠路径**并集** |

唯一「丢」的是把每种精确字段共现组合实例化成独立 unit 的能力——而那恰是 2ᵏ 组合噪声本身
（top 组合仍在 `skeleton_counts`）。真正多实体文件（数组里混 user+order）按**顶层 key 集合**
粗聚类、K 很小，而非按 exact signature。

**前置缺口**：要让「可选性」真正无损，`occurrence` 不能再是占位 `1.0`（现状），需落地真实
出现率（= 待完成项 4 的 `optional_field_grouping`）。所以本条工作量 = 改 JSON/JSONL 分片策略
（去签名聚类）+ 落地 occurrence，**而非加 cap**。

#### 7.3 `profile_similarity` 修复 + 分块/LSH：O(n²) → 近线性

核心思想：**绝大多数 entry 对不可能同义，别去比**。两层：

**(a) 分块（blocking / canopy）**：给每 entry 算廉价离散 block key，只在同 key 块内两两比，
开销从 n² 降到 Σ(块大小)²。block key 用粗化画像特征（重构后 value_profile 的现成字段）：
```
block_key = (dtype, len_band, dominant_pattern_class, dominant_script)
  "13800000000" → (str, len11,   "D{11}",      -)      手机块
  "a@b.com"      → (str, len6-15, "L+@L+.L+",  Latin)  邮箱块
  "张三"         → (str, len1-4,  "C{n}",       Han)    中文名块
```
**召回兜底（多键分块）**：边界样本（len10 vs 11 落不同 band）会漏配 → 每 entry 挂多个 block
key（按 len_band 与 pattern_class 各挂一次），任一键相撞即候选对。

**(b) LSH（分块的概率化稳健版）**：`avg_char_dist` 7 桶向量 → **SimHash**（余弦敏感）；
`top_patterns` 集合 → **MinHash**（Jaccard 敏感）；再 **banding**：任一段哈希相同即候选对，
近似项高概率相撞、远的几乎不撞，候选对再用真实 `profile_similarity` 复核。

**(c) 真实 `profile_similarity`（替占位的恒 0）**：
```
1. dtype 门:           不一致 → 0
2. char_dist 分布距离: 7 桶向量余弦 / 1−JSD
   —— avg_char_dist 现已 MECE、和恒为 1, 是合法概率分布, 此距离才有意义
3. 长度重叠:           len_dist mean/std 接近度
4. pattern 重叠:       top_patterns 两集合 Jaccard
5. (可选) avg_scripts 直方图距离
```
关键：**喂进 block key 的粗特征要与打分特征同源**（都从同一份画像粗化），分块与打分逻辑自洽。

**(d) 新流程与复杂度**：
```
每 entry 算 block key(s)                    O(n)
按 key 分桶                                 O(n)
每桶内真实 profile_similarity 跑 union-find   O(Σ b_i²), 桶小则近线性
C(PII 类型)全局 union 不变
```
块有界（或 canopy + 上限保护）时整体近线性——**这时才有资格真正打开 B 证据**。

#### 7.4 落地顺序（建议）

| 步 | 动作 | 性质 | 收益 |
|----|------|------|------|
| ① **立即止血** | `vocab_table.py` 加 `_PROFILE_B_ENABLED=False` 护栏，关掉 7.1(b) 的 O(n²) 空转；Pass2 补进度日志 | 最小、最急；B 现恒 0，关掉**零行为变化** | 当前运行立即解锁、不再静默卡死 |
| ② **治本去爆炸** | JSON/JSONL 改单文件 fold 分片（去签名聚类）+ 落地真实 occurrence | 结构性，改动面中 | unit 数回到 O(文件数)，字段无损（见 7.2） |
| ③ **打开 B** | 修 `profile_similarity`（真实度量）+ 分块/LSH | 结构性，改动面大 | B 证据可用且跑得起（见 7.3） |

> ① 与待完成项 6 残留方向 1（vocab 聚类 O(字段条目数)内存）同源；② 与待完成项 3.1 / 4 同源；
> ③ 取代待完成项 2 的占位升级路径。**三步独立可分别落地，①最优先**。

---

### 8. 面向编码器的精简 SchemaUnit / IR 设计（演进方向，未实现）

> 本项记录「IR 数据集喂结构/代码预训练编码器（GraphCodeBERT/UniXcoder）+ 可控注噪」
> 这一论文主线确定后，对 SchemaUnit 抽象的重新定位。**与现行 5-info 落盘结构并存**：
> 现行 SchemaUnit 继续作数据资产编目视图；本项定义的是**喂模型的精简投影视图**。
> 来源为多轮设计讨论，落地前需与 §5（fold 模式）、§7.2（去签名聚类）协同。

#### 8.1 前提纠正：两个不同的 I(x)，别混用

| | **Tier 期 I(x)** | **训练期 I(x)** |
|---|---|---|
| 来源 | 容错解析器在**真实脏数据**上的可恢复残量 | 可控注噪的**噪声元数据**（改了哪些字段/记录） |
| 性质 | **估计量**，无真值分母，不稳定 | **oracle 标签**，注噪前后差分即真值 |
| 用途 | **仅** Tier1/2/3 分桶（粗筛） | 编码器结构完整性 / c_struct 头的监督信号 |

- 已识别的那批 Tier 期 I bug（CSV I≡1.0、JSON 早崩漏计、SQL DDL/DML 混入、64KB 头盲视尾部）
  **不污染训练信号**——它们只影响 Tier 分桶。粗筛门控不需要计量级稳定性，与
  「样本级 Î(x) 神经头已砍（解析器能算）」自洽。
- **唯一仍要守的**：Tier 期 I 决定**哪些文件成为 Tier1 干净种子**。若 precision 不足（如 CSV 恒判干净），
  带真噪文件混进种子 → 注噪基底 `x` 不纯（garbage-in）。对策：Tier1 选取**从严**（高阈值 + 多判据），
  Tier 期 I 仅作其中一个**弱判据**，不单独定生死。

#### 8.2 精简抽象：`{全路径 → 样本值, dtype}`，拓扑序列化期派生

确定的形态（输入视图）：
```
SchemaUnit(精简/IR 投影) {
  source_file, format, partition_id,
  fields: {
    "order.buyer.tel":  { samples: [按频率降序 ≤5, 按 pattern 去重], dtype: "str" },
    "order.buyer.name": { samples: [...],                          dtype: "str" },
    "order.amt":        { samples: [...],                          dtype: "float" },
    ...                          # 全点路径 + [] 标记作 key
  }
}
```

设计决策与理由：

1. **保留原始样本值（取代 profile 统计作 IR 主体）**。三条理由：
   - 编码器在真实 token 上预训练，强项是读字面值；喂 `D{11}` 抽象反而丢了它最擅长的词法信号。
   - **可控注噪在操作上必需字面内容**——截断/引号不闭合/分隔符未转义无法施加在 `D{11}` 上，
     pattern 不可逆，无法从它重建可注噪的物件。
   - 内网环境，**暂不引入 PII 双视图/掩码**（合规底线此处不考虑）。复用现成 `_select_samples`
     （按 pattern 去重 ≤5，`value_profile.py:225`），样本集天然显式化了值的异质性。
   - 廉价增强：样本**按频率降序**排列，保留弱比例暗示（弥补丢掉精确比例）。

2. **不要 dispersion 异质标量**（`unique_pattern_ratio`/`len_std`/`B` 不进精简 unit）。理由：
   - 异质标量本身就是要为 token 编码器减掉的手工特征；
   - 按 pattern 去重的 ≤5 样本**已把不同形状显式化**，异质性编码器直接从样本看见；
   - c_struct 的监督**标签来自注噪元数据，不来自观测离散度**；编码器从 `{samples + 邻域}`
     就能学到「样本长得不像路径/邻居 → 低 c_struct」。

3. **拓扑：必须可达编码器，但不物化成冗余字典**。
   - 邻域/嵌套层级是**路径集合的纯函数**（`depth=path.count('.')+1`、`parent=rsplit`、
     `siblings=同父`），全点路径 + `[]` 标记作 key 就**零成本保住**（故删 `key_name`，它是路径的派生）。
   - 邻域作为**边**在序列化期派生（保留 `_build_topology_folded`，配 `_expand_with_ancestors`
     补出 `order.buyer` 这种无叶子样本的中间容器节点，否则「tel 与 name 同属 buyer」的邻域推理会断），
     与 GraphCodeBERT「结构当边喂」一致，也是研究内容二 `s_context`（上下文邻域）证据来源。
   - **绝不存 `siblings:[...]` 列表**：200 字段扁平记录 → O(n²) 膨胀 + 与路径漂移。若坚持物化，
     至多存 `parent` 指针（siblings 同父反查、depth 由路径算）。

4. **主干取 `fold` 而非 `template`**（与现行默认相反）。精简设计以「样本 + 拓扑」为核心，
   拓扑保真关键；`template` 只留 most_common 主干会丢少数派兄弟字段 → 派生的邻域是**剪过的邻域**，
   `s_context` 证据失真。把异质性交给样本与完整路径集承载，而非剪枝+标量。与 §7.2（去签名聚类、
   单文件 fold 分片）同向。

5. **输入表示 ≠ 标签，二者并行存、不混进 input unit**：
   - **类型标签**（CTA 目标，是否手机号等）：来自 `pii_seed` 弱标（key 名规则 + 自由文本判定），
     **不可由路径或样本派生**，是规则决策，有独立价值 → 留作弱标签源（后续少量人工校验）。
   - **结构/噪声标签**（I(x)、哪一层破坏、哪条路径被改）：来自注噪对齐三元组（干净 x、带噪 x'、
     噪声元数据）。这是稳定 I(x) 的家。

#### 8.3 落地缺口（实现前需补）

- `schema_unit_to_ir()` 投影函数：从富 SchemaUnit（或直接 build）产出精简视图；拓扑派生为边/相对结构，
  不落冗余字典。
- `value_profile` 降级：不再是 IR 主体，保留为规则模块 / 内容三 / vocab_table 的备用特征源（§7.3 仍用其粗特征做分块）。
- 与 §5 协同：精简投影默认走 `fold`；与 §7.2 协同：JSON/JSONL 去签名聚类、单文件分片后，
  fold 的全路径并集才是稳定 backbone。
- 注噪流水线（研究内容一）：消费精简 unit 的 `samples` 作 `x`，施加噪声算子 `T(x,Θ_n)` 产出对齐三元组，
  回填 8.2(5) 的标签视图。

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
