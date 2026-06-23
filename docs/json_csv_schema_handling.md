# JSON / CSV 结构处理设计文档

> 面向 SchemaStruct（对多源异构噪声数据做结构鲁棒编码）的研究目标。本文档聚焦数据流水线
> 阶段2（Schema 提取）中 **JSON/JSONL 分片** 与 **CSV schema 去重** 两个结构鲁棒性问题：
> 它们是同一条原则在两个尺度上的体现。
>
> 全文以 `文件路径:行号` 形式引用真实函数，便于点击核对。可复现产物：
> [`test_data/_validate_part.py`](../test_data/_validate_part.py)。

---

## 目录

1. [现状简述](#一现状简述)
   - [JSON/JSONL 现状](#11-jsonjsonl-现状)
   - [CSV 现状](#12-csv-现状)
2. [解决方案](#二解决方案)
   - [JSON：精确签名 → 兼容性合并 / 模式包络](#21-json精确签名--兼容性合并--模式包络union-schema)
   - [CSV：schema 级去重（保留重数）](#22-csvschema-级去重保留重数)
3. [要点解读](#三要点解读)
   - [统一原则](#31-统一原则贯穿-json-与-csv)
   - [验证加载器原理](#32-验证加载器原理_validate_partpy)
   - [三个要硬化的诚实边界](#33-三个要硬化的诚实边界)
   - [深层一句](#34-深层一句指纹--类型系统--容差)
4. [落地与约束](#四落地与约束)

---

## 一、现状简述

### 1.1 JSON/JSONL 现状

阶段2 的文件内分片由 [`schema_partition.py:75`](../src/extract/schema_partition.py) `_partition_json()`
负责：先尝试探测显式包装 key（[`schema_partition.py:95`](../src/extract/schema_partition.py)
`_detect_explicit_keys`），兜底走骨架签名聚类
（[`schema_partition.py:126`](../src/extract/schema_partition.py) `_cluster_by_skeleton_json`）。

兜底路径按 [`skeleton.py:7`](../src/extract/skeleton.py) `structure_signature` 的 **精确签名**
给每条记录分桶（[`schema_partition.py:133-134`](../src/extract/schema_partition.py)）。该签名由
[`skeleton.py:22`](../src/extract/skeleton.py) `_signature` 递归生成，规则严格：

- dict 按 key 排序保留键名（[`skeleton.py:61-65`](../src/extract/skeleton.py)）；
- 标量替换为类型标记 `<int>`/`<float>`/`<str>`/`<bool>`/`<null>`（[`skeleton.py:46-55`](../src/extract/skeleton.py)）；
- list 只取 **首元素** 递归（[`skeleton.py:56-60`](../src/extract/skeleton.py)），空 list 记为 `[]`；
- 空 dict 记为 `{}`。

**问题：签名过脆，一个同质 schema 被打散成大量分片。** 实测（在 `test_data/DataPart` 上，
用 [`test_data/_validate_part.py`](../test_data/_validate_part.py)）：

| 文件 | 记录数 | 现行精确签名分片 | 合并后分片 |
|---|---|---|---|
| test1.json | 3 | 3 | 1 |
| test2.json | 2,219 | 33 | 1 |
| test3.json | 4,734 | 1,253 | 1 |

`test3` 是决定性证据：一个同质 Mongo User 导出被碎成 **1253 个「schema 单元」**
（≈26% 记录各占一个签名）。对 SchemaStruct 而言，这意味着同一种实体被错误地编码成上千个
互不相干的结构原子，研究目标（结构鲁棒编码）在此直接失效。

### 1.2 CSV 现状

阶段2 把每个 CSV 视为整文件单 partition（[`schema_partition.py:227`](../src/extract/schema_partition.py)
`_partition_csv`），逐文件计数。但真实语料里大量文件 **共享同一 schema**：切片 dump
（`part-0001.csv…`）、多张同布局表，都因为「在数文件而非数 schema」而拉高 CSV 占比。

研究原子是 **distinct schema，不是文件**。`csv_tests/` 样本 60 个，典型重复族包括
`ad_line_carriers` + `_1..4`、`ad_line_devices` + `_1..4`、无列名的 `1.csv`..`21.csv`
（同值结构）。把这些都当独立 schema 计数，会让分布统计严重失真。

---

## 二、解决方案

### 2.1 JSON：精确签名 → 兼容性合并 / 模式包络（union schema）

把分片键从「精确签名」改为 **兼容性合并 / 模式包络**。SchemaStruct 的正确原子是
**一个记录家族的并集 schema**，每个字段带 presence-rate（出现率）与 type-set（类型集）。

#### 2.1.1 三条并列成因（都在样本里出现）

精确签名脆在三处，每一条都能独立把同质数据炸开：

1. **可选键（键存在/缺失）** —— 键集不同 → 签名不同。
   `test3` 的 `geo` 字段时有时无；`test1` 记录间键集差十几个字段。

2. **null 多态** —— `<null>` ≠ `<str>`；可空字段按「本条是否为 null」翻签名。
   `test2` 的 `address1` / `zip` / `province_code` 即此类。

3. **空容器 vs 填充容器** —— `[]` ≠ `[{...}]`、`{}` ≠ `{...}`。
   `test3` 的 `devices`、`rate:{}`、`ratings:{}`、`traffic_source:{}` 都因此翻签名。

此外有两条 **潜在成因**（当前样本未必触发，但同样脆）：

- `<int>` ≠ `<float>`（[`skeleton.py:50-53`](../src/extract/skeleton.py) 区分两者）；
- list 只取首元素（[`skeleton.py:59`](../src/extract/skeleton.py) 仅递归 `value[0]`），
  首元素与后续元素结构不同则签名不代表整个列表。

**数学上**：k 个相互独立的可选/可空/空容器字段 → 最多 **2^k** 个签名。这正是 1253 这种量级的来源。

#### 2.1.2 判据与归一化

两条记录归同一分片 ⟺ 在 **共享叶路径上无类型冲突**。归一化规则把上述脆点逐一消解：

| 脆点 | 归一化 |
|---|---|
| null | → 不携带类型（可空，通配） |
| 缺键 | → 不算冲突（可选） |
| 空 `{}` / `[]` | → 通配 |
| int / float | → 统一 `num` |
| list | → 对 **所有元素** 取并，不只首元素 |

#### 2.1.3 单遍贪心聚类

每条记录并入第一个兼容的原型（扩张其并集 + 累加每字段出现率），否则开新原型。

真正异质的实体（共享路径上有 **真冲突**，例如一个文件混了 users + orders）仍会被正确分开——
归一化只放过「可选/可空/空容器」这类伪差异，不放过类型实质冲突。

每字段的 presence-rate 落进 `SchemaPartition.occurrence`
（[`schema_partition.py:468`](../src/extract/schema_partition.py) 已有字段，由
`build_schema_unit` 消费后回填）。这样 **可选字段的信息从「分片爆炸」变成「一个统计量」**：
不再是上千个签名，而是一个并集 schema 上每个字段的出现概率。

#### 2.1.4 算法骨架

取自 [`test_data/_validate_part.py`](../test_data/_validate_part.py) 的验证原型
（[`_validate_part.py:63-104`](../test_data/_validate_part.py)），生产实现给伪代码即可：

```text
norm_type(v):  None→None(通配); bool→bool; int/float→num; str→str; dict→obj; list→arr
leaf_types(v, prefix, out):  展平成 {叶路径: set(类型)}; 跳过空容器; list 对所有元素取并
compatible(proto, rec):  不存在"共享路径且类型集不相交"
merge_cluster(records):  贪心并入第一个兼容原型(并集+计数), 否则新原型
```

对应到验证脚本：`norm_type` 见 [`_validate_part.py:63`](../test_data/_validate_part.py)，
`leaf_types` 见 [`_validate_part.py:72`](../test_data/_validate_part.py)（注意 `:75`/`:77`
对空 dict/空 list 直接 `return`，即「跳过空容器」；`:78` 对 list 的每个元素递归，即「取并」），
`compatible` 见 [`_validate_part.py:86`](../test_data/_validate_part.py)（判定共享路径类型集是否
`isdisjoint`），`merge_cluster` 见 [`_validate_part.py:92`](../test_data/_validate_part.py)。

#### 2.1.5 落地点

- 在 [`skeleton.py`](../src/extract/skeleton.py) 增加 **归一化签名 / 包络合并** 能力
  （`norm_type` + `leaf_types` + 兼容性判定）。
- 把 [`schema_partition.py:126`](../src/extract/schema_partition.py) `_cluster_by_skeleton_json`
  改为调用它（替换当前 `structure_signature` + sha256 分桶逻辑，
  [`schema_partition.py:133-136`](../src/extract/schema_partition.py)）。
- **验证**：test1 / test2 / test3 各收敛到 1 个分片。

#### 2.1.6 附带 bug：JSONL-as-`.json` 静默丢文件

`test1.json` 实为 JSONL（逐行独立对象）却用 `.json` 后缀。当前 [`json_parser.py:16`](../src/parse/json_parser.py)
`parse_json` 的处理链：

1. [`json_parser.py:67`](../src/parse/json_parser.py) `_ijson_count_items` 用 `ijson.items(f, "item")`
   找顶层数组元素 → JSONL 没有顶层数组 → 得 0 条；
2. `good == 0` 兜底走 [`json_parser.py:146`](../src/parse/json_parser.py) `_json_tolerant`，
   用 `json5.loads` 整文读 → 多个顶层值 → 报 "Extra data"；
3. 结果：**零分片（静默丢文件）**。

> 注：分片阶段的流式入口 [`schema_partition.py:141`](../src/extract/schema_partition.py)
> `_stream_json_records` 也是同样链路（ijson `item` → 兜底 json/json5 整文），对 JSONL-as-`.json`
> 同样产出 0 条记录。

**建议**：顶层数组取不到记录时，探测是否为「逐行独立对象」，是则转 JSONL 迭代
（逐行 `json.loads`，与 [`json_parser.py:108`](../src/parse/json_parser.py) `parse_jsonl` 一致）。

---

### 2.2 CSV：schema 级去重（保留重数）

#### 2.2.1 为什么不靠人工删减

用户初步想法是人工核对删减重复文件。**不建议作为主手段**，四个理由：

1. **不可复现** —— 论文站不住脚；
2. **不可扩展** —— 全量 45k 文件人工核不动；
3. **判不准** —— 人对无列名近重复文件的等价性判断不可靠；
4. **丢掉频率** —— 而频率正是标定退化曲线 / Tier2 频率权重所需，应 **记录而非删除**。

#### 2.2.2 方案：指纹 → 聚类 → 留代表 + 存重数

schema 级去重：每个 CSV 算一个 **schema 指纹** → 聚类 → 每簇留 1（或 k）个代表进 SchemaStruct 集，
把 `cluster_size` 作为 **频率权重** 存下。

- **无列名指纹**：`(众数列数, 各列 value-profile 签名元组)`；复用 [`value_profile.py`](../src/extract/value_profile.py)
  的字符宏类分布（[`value_profile.py:77`](../src/extract/value_profile.py) `_macro_class`）/长度桶/
  模式模板（[`value_profile.py:179`](../src/extract/value_profile.py) `_make_pattern`，复用
  [`value_profile.py:134`](../src/extract/value_profile.py) `_str_profile` 的同源 pattern）。
- **有列名指纹**：归一化表头元组（小写 / strip，**保留顺序**）+ 可选叠加列类型 profile。
- **跨文件夹全局去重**，不按文件夹分桶。
- **人工的正确位置** 是审核自动簇（抽查代表 + 孤立/低置信簇），当 QA，不当去重机制本身。

**类比（dedup-with-multiplicity，同 LM 预训练去重）**：去重用于训练均衡（别让编码器过拟合最高频布局），
保留重数用于分布标定。**训练吃 distinct schema，评估/标定用真实频率权重。**

#### 2.2.3 实测

用 [`test_data/_validate_part.py`](../test_data/_validate_part.py)（Q2 段，
[`_validate_part.py:160`](../test_data/_validate_part.py) 起）：

> **60 文件 → 29 个精确指纹桶 → 16 个 distinct schema**（空格当通配 + 兼容性合并后）。

- 20 个无列名编号文件（`1.csv`..`21.csv`）全并成 1；
- 各 split-dump 家族正确折叠：carriers×5、devices×5、day_parts×5、attribute_values×5、
  attributes×4、dmp_targeting×7；
- 真正的 singleton（`Report`、`ad_group_*`）未被误并。

#### 2.2.4 关键发现：null 多态在列层重演

第一版 CSV 指纹（每列取众数宏类）把 20 个编号文件拆成 8 个簇——某列在采样行里
「恰好空 / 恰好有值」就翻签名。**这与 §2.1.1 的 null 多态是同一个病，只是从记录字段下沉到 CSV 列。**

把「空 → 通配」这条规则照搬到列上（见 [`_validate_part.py:177`](../test_data/_validate_part.py)：
`if c != "e": s.add(c)`，空 cell 不携带 schema 信息；以及 [`_validate_part.py:181`](../test_data/_validate_part.py)
`v_compatible` 允许一方为空集），20 个立刻并成 1。

---

## 三、要点解读

### 3.1 统一原则（贯穿 JSON 与 CSV）

> **按鲁棒 schema 指纹聚类、空/null 当通配、留代表、保住计数（`cluster_size` / `occurrence`）。**

- **Q1 在记录层**（union-schema 合并，[`schema_partition.py:126`](../src/extract/schema_partition.py)），
- **Q2 在文件层**（schema 去重）。

两者是 **同一条原则的两个尺度**：记录之于文件，正如字段之于列。null/空容器在记录层制造伪签名差异，
空 cell 在文件层制造伪列差异——同一种病、同一味药。

### 3.2 验证加载器原理（`_validate_part.py`）

[`test_data/_validate_part.py:34`](../test_data/_validate_part.py) `load_records` 用于本地无 ijson
（或 ijson 失败）时复现，两层结构：

1. **ijson 流式（快路径，与流水线一致）**：[`_validate_part.py:39-46`](../test_data/_validate_part.py)；
2. **兜底逐元素恢复**：[`_validate_part.py:48-61`](../test_data/_validate_part.py)，用
   `json.JSONDecoder().raw_decode(s, idx)` 逐顶层值恢复。

#### 3.2.1 `raw_decode` 与 `json.loads` 的关键区别

`raw_decode` 只从 `idx` 解析出 **一个** 值就停、忽略其后内容；`json.loads` 要求整篇是一个完整值，
**中途任何不闭合则整篇归零**。这是「保留干净前缀」能力的来源。

#### 3.2.2 技巧：把大数组降维成元素流

[`_validate_part.py:51`](../test_data/_validate_part.py)：
`ws = re.compile(r"[\s,\[\]]*")` 把空白 / 逗号 / `[` / `]` 全当可跳过分隔符，
从而把「一个大数组」降维成「一串元素」，逐个 `raw_decode`
（[`_validate_part.py:53-60`](../test_data/_validate_part.py)），撞到损坏的那条就 `break`
（[`_validate_part.py:57`](../test_data/_validate_part.py)），保留干净前缀。

`test3` 恢复 **4734 条 = 4734 个完整对象 + 一条截断尾巴**（深度扫描显示到 EOF 括号深度停在 2、
未归零）。

#### 3.2.3 这恰好演示 C/P/L（通道模型）

恢复的 4734 = **通道一（C+P）**，坏尾 = **通道二（L）**；即「容错解析器划定恢复边界」。
这与流水线 [`json_parser.py:37-61`](../src/parse/json_parser.py)（ijson 崩溃前的部分恢复 +
按比例估算 I(x)）是同一个语义。

#### 3.2.4 诚实边界

这是 **脚手架替身**，不是生产实现：

- 兜底 `open().read()` 整文件进内存（[`_validate_part.py:48`](../test_data/_validate_part.py)），
  **违反 GB 流式约束**，仅 12MB 样本可接受；
- 「`[` / `]` 当分隔符」假设顶层是对象数组 / JSONL。

生产用 ijson（常数内存流式，撞坏尾同样 yield 完前缀再抛——见
[`json_parser.py:91-102`](../src/parse/json_parser.py) 的 `_PosTracker` + 崩溃捕获），故验证脚本是
**忠实替身**：行为同构，只是内存模型不同。

### 3.3 三个要硬化的诚实边界

#### 3.3.1 quote 感知切列（必须 `csv.reader`，不能 `split(sep)`）

CSV 字段加引号后可含分隔符。例：`330000,330003,2,"12,14,26"` 是 **4 列**，naive split 数成 **6 列**。
`ad_line_dmp_targeting.csv` 因此被数成 158 列、误判无列名（7 个 dmp 文件碰巧错得一样才聚在一起）。
换 `csv.reader`（RFC 4180：尊重 `"..."`、转义 `""`、引号内换行）后回到正确 4 列。

**为什么要命**：列数是 **最基本的结构不变量**，列数错则下游每个结构签名都错；引号含分隔符在真实 CSV
极常见。

**生产已解决**：[`csv_parser.py:33`](../src/parse/csv_parser.py) `_parse_delimited` 用 `csv.reader`、
[`schema_partition.py:242`](../src/extract/schema_partition.py) `_partition_csv` 用 `csv.DictReader`，
**复用即可**。验证脚本侧见 [`_validate_part.py:151`](../test_data/_validate_part.py) `read_rows`
（已用 `csv.reader`）。

**铁律**：不能先按行 split 再喂 csv（会切断引号内换行的多行字段）；分隔符嗅探
（[`csv_parser.py:80`](../src/parse/csv_parser.py) `_sniff_delimiter`、
[`schema_partition.py:283`](../src/extract/schema_partition.py) `_sniff_sep`）目前用
`line.count(sep)` **非 quote 感知**，是已知薄弱点，须一并 quote 感知化。

#### 3.3.2 表头探测启发式

当前判据「row0 无纯数字、row1 有纯数字 ⟹ 有表头」
（[`_validate_part.py:142`](../test_data/_validate_part.py) `has_header`）**两个方向都会错**：

- **漏判**：`ad_groups.csv`（有表头却判无列名）、`Report` 文件（引号文本表头 + 文本数据）；
- **误判**：把「首行恰好全文本」的数据误判成表头。

**为什么要命**：判错 = 同一 schema 被劈进「列名空间」与「值结构空间」，**永不互相去重**；
漏判有表头时还丢掉列名（SchemaStruct 最想要的语义层信号）。

**硬化方向**：

1. **类型不一致检验**：row0 该列是 str、而该列数据是稳定非 str 类型，**多数列** 出现此错位 ⟹ 表头；
   把单一数字判据推广到 date / bool / float 且用 **多数表决**；
2. **全字符串表回退词法线索**：短 snake_case / Title 标识符、列名互不重复、命中字段名词表
   （可扩 `PII_KEY_PATTERN`）；
3. **置信度 + 弃权**：低置信文件两个空间都打指纹，或进人工审核道。

> 这是文件层的「首行是否表头」——本质是 **结构噪声**，正解是 **置信度路由而非脆弱布尔**。
> 流水线侧已有相关信号：[`csv_parser.py:71`](../src/parse/csv_parser.py) 的
> `header_col_mismatch`（表头列数 ≠ 数据众数列数）可作为弃权/路由的输入。

#### 3.3.3 数值/类型类的归并阈值

扁平互斥类有重叠，会制造 **假冲突挡住合并**：

- `@`（email）⊂ `s`（string）；
- `p`（phone）与 `n`（纯数字）重叠——10 位电话 **既 `p` 又 `n`**
  （见 [`_validate_part.py:134`](../test_data/_validate_part.py) `cell_class`，分支互斥但语义重叠）；
- 同列 A 落 `{n}`、B 落 `{p}` → 假劈；采样行少时尤甚；
- id / 编码的 int/float/前导零（`00187`）也会分叉。

**两难**：类太细造假冲突 → 重新膨胀；类太粗 → 错并真不同 schema。

**硬化方向**：

1. **带包含关系的类型格**：`email ⊏ string`、`phone ⊏ numeric-ish ⊏ string`、`int ⊏ num`；
   兼容性 = **存在非平凡共同上界**（而非要求类型集严格相交）；
2. **主导类型 + 容差比例**：非空 ≥ τ 是数值即判数值列，对脏值 / 小样本更稳；
3. **高信号列用 `value_profile` 的 pattern template 而非粗类**
   （[`value_profile.py:170`](../src/extract/value_profile.py) `_str_profile` 产出的 `pattern`，
   token 集有界、跨语言可比对，[`value_profile.py:99-105`](../src/extract/value_profile.py)
   `_letter_token`）；
4. **阈值在真实语料上标定**。

### 3.4 深层一句：指纹 = 类型系统 + 容差

> 指纹好坏 = **类型系统 + 容差**；这套类型系统应是 **唯一一套**，被三处共用：
> ① JSON 记录签名、② CSV 列类、③ `value_profile` 模板。

三份「判类型」实现 = 三处会漂的地方（**DRY**）。统一它，阈值只在真实数据上 **标定一次**。

- §3.3.1（quote 感知切列）是 **生产已解决的复用**；
- §3.3.2、§3.3.3 **本质依赖真实语料**，本地标定不了，须到远程全量调。

值得一提：[`value_profile.py`](../src/extract/value_profile.py) 内部已贯彻这一思想——char_dist 与
scripts 直方图共用同一个 `_macro_class` / `_script_of`
（[`value_profile.py:14-17`](../src/extract/value_profile.py) 的设计注释明确「不存在两套分类器漂移」）。
本节主张把这一「单一真源」原则从 value 画像内部，扩展到 JSON 签名 / CSV 列类的跨模块层面。

---

## 四、落地与约束

### 4.1 落地顺序建议

1. **先 Q1 包络合并**（§2.1）：改动明确、可在 DataPart 三样本立即验证；
2. **再 Q2 CSV 指纹去重**（§2.2）；
3. **附带修 test1 的 JSONL-as-`.json` 路由**（§2.1.6）。

### 4.2 环境约束

- 本地仅 **编辑 + 读小样本**；代码实际跑在远程：
  `ssh root@172.17.66.200 "cd data/header_parser/zlf/PII_detect/detect/ && uv run python <脚本>"`；
- 两个方案都须在 **远程全量复跑** 确认（本地无 ijson / 真实数据，只能验证逻辑）。

### 4.3 不变约束

- ❌ **禁止整文件 load**（流式 / ijson）——大 JSON 走 ijson、JSONL 逐行、CSV/xlsx 流式分桶；
- ❌ **禁止持久化 PII 原值**——画像只存统计摘要（[`value_profile.py`](../src/extract/value_profile.py)
  默认 `sample_mode="off"`，[`value_profile.py:294-298`](../src/extract/value_profile.py)）。

### 4.4 可复现产物

[`test_data/_validate_part.py`](../test_data/_validate_part.py)——Q1（DataPart）/ Q2（csv_tests）
两段对比「现行精确签名 vs 提议鲁棒指纹」，输出上文两张实测表的数据。
