# PII Detect — 详细指南（业务逻辑 / API / 数据流）

> 本文是实现层参考，按需查阅。稳定的约束/命令/结构/约定见 `../CLAUDE.md`；
> 演进中的设计、占位接口与待办见 `../todo_list.md`。

---

## 1. 总览与数据流

三阶段管线，前一阶段产出即后一阶段输入：

```
corpus_root (目录/单文件)
   │  main.py:resolve_input() 宽限解析 → 文件路径列表
   ▼
[阶段0 嗅探 sniff]   每文件 sniff_file() → (real_format, encoding, confidence)
   │                  目录级 profile_corpus() → 交叉表 + 格式分布 + 低置信清单
   ▼
[阶段1 分级解析 parse]  grade_parse(path, fmt, enc) → Grade(tier, I, parsed, …)
   │                    逐文件追加 grades.jsonl（落盘）；分拣 tier1/tier2/tier3/noise/free_text
   ▼  阶段2 流式读 grades.jsonl，仅 tier1
[阶段2 Schema 提取 extract]  stream_schema_units(逐文件分片+构建→追加 schema_units.jsonl)
        → finalize_from_units(两遍流式聚合) → (vocab_table, global_view)
```

- 各阶段由 `main.py` 的子命令编排（`cmd_sniff/cmd_parse/cmd_extract/cmd_pipeline`）。
- **阶段间通过 JSONL 落盘流式传递**（`grades.jsonl` / `schema_units.jsonl`），**不在内存累积全量
  Grade / SchemaUnit**，内存恒定 O(单文件)，适配 ~45k 文件 / GB 级。详见 §4.1 与 §9。
- 阶段间逻辑数据载体仍是 `Grade`（阶段2 从 `grades.jsonl` 行经 `grade_from_summary` 重建最小 Grade，
  partition 再从磁盘重读原文件取记录）；阶段2 内部用 TypedDict（见 §5）。
- **断点续跑**：重跑同一命令自动跳过 `grades.jsonl` / `schema_units.jsonl` 中已处理的文件；崩溃截断的
  尾行由 `iter_jsonl` 容错跳过；`--restart` 强制清空重来。

---

## 2. 阶段0 — 内容嗅探（`src/sniff/`）

### 2.1 `sniffer.py` · `sniff_file(path) → (real_format, encoding, confidence)`

单文件真实格式判定，**内容优先于扩展名**。流程（短路顺序）：

1. 读前 16 字节，命中 `SQLITE_MAGIC`（`b"SQLite format 3\x00"`）→ `("sqlite","binary",1.0)`。
2. 读前 1KB，`is_binary()` 判二进制 → `.db` 扩展名给 `db_nonsqlite`(0.8)，否则 `binary_unknown`(0.6)。
3. 文本类：读 64KB 头部（`SNIFF_HEAD_BYTES`）→ chardet 探编码 → `safe_decode(errors="replace")` → 取前 20 非空行（`SNIFF_LINES`）。
4. 无非空行 → `("empty", enc, 1.0)`。
5. `.sql` 扩展名 + 命中 `create table|insert into|drop table` → `("sql", enc, 0.95)`（弱先验加速）。
6. 否则多候选加权投票 `vote_format()`，取最高分；`conf < ACCEPT_THRESHOLD(0.5)` → 归 `free_text`。

`real_format` 取值域：`json|jsonl|csv|tsv|sql|sqlite|log|free_text|db_nonsqlite|binary_unknown|empty`。

### 2.2 `voting.py` · `vote_format(lines, full_text) → {fmt: score}`

7 候选并行打分（分数可叠加，非互斥）。权重表：

| 格式 | 判据 | 加分 |
|------|------|------|
| `json` | strip 后首字符是 `{`/`[` | +0.6；括号闭合再 +0.3 |
| `jsonl` | 每行能独立 `json.loads` 的比例 > 0.8 | +0.9 |
| `csv`/`tsv` | 分隔符（`\t`/`,`/`;`/`\|`）列数跨行稳定（首行 ≥2 列、列数标准差 <0.5） | +0.8；首行像表头再 +0.1 |
| `sql` | 命中 `SQL_KEYWORD_PATTERN` | +0.7 |
| `log` | 行命中 `LOG_PATTERN`（时间戳/级别）比例 > 0.6 | +0.85 |
| `free_text` | 平均行长 >40 + 句末标点 + 无强结构信号 | +0.5 |

### 2.3 `profiler.py` · `profile_corpus(root, files=None)`

目录批量画像。`files` 给定时直接用（与 `resolve_input` 协作避免重复遍历）。产出：

```python
{
  "cross_table":    {"ext|fmt": count},      # 扩展名×真实格式 交叉表
  "format_dist":    {fmt: count},
  "low_confidence": [(path, fmt, conf), …],  # conf < LOW_CONF_THRESHOLD(0.7)，最多 200 条
  "total_files":    int,
}
```

---

## 3. 阶段1 — 容错分级解析（`src/parse/`）

### 3.1 `Grade`（`grade.py`，dataclass）

阶段间核心数据结构：

| 字段 | 含义 |
|------|------|
| `tier` | `1`=干净种子 / `2`=可恢复噪声 / `3`=不可解析 / `"noise_sample"`=日志类弱结构 / `"free_text"`=自由文本 |
| `I` | 可恢复性 0~1；非结构化（log/free_text）为 `None` |
| `parsed` | 解析摘要（dict），非原始数据 |
| `fmt` / `encoding` / `path` | 格式 / 编码 / 文件路径 |
| `error` | 失败原因（tier3） |
| `n_form` | JSON 错误分类（trailing_comma/single_quotes/…） |
| `n_struct` | CSV 列漂移度量（变异系数） |
| `note` | 备注 |

`grade_parse(path, real_format, enc)`：按 `real_format` 路由到对应 parser（延迟 import 避免循环依赖），统一回填 `path`。`log`/`free_text` 直接构造弱结构 Grade；`binary_unknown`/`db_nonsqlite`/`empty` → tier3。

### 3.2 各 parser 策略

| parser | 入口 | strict | tolerant | I(x) 计算 |
|--------|------|--------|----------|-----------|
| `json_parser.py` | `parse_json` | ijson 流式读全部元素，无崩溃 → tier1 | 崩溃前部分元素 → 按字节比例估总量；good==0 → json5 容错（注释/单引号/尾逗号） | `good/estimated_total` |
| | `parse_jsonl` | 逐行 `json.loads` 统计 good/bad | 坏行跳过 | `good/(good+bad)` |
| `csv_parser.py` | `parse_csv`/`parse_tsv` | 嗅探分隔符（`,;\|`），列数全一致 → tier1 | 列数漂移 → tier2 记 `n_struct` | `good_rows/total_rows` |
| `sql_parser.py` | `parse_sql_text` | 读头 64KB，按 `;` 分语句，regex 匹配 CREATE/INSERT | 引号不配对计数入 note | `complete/total statements` |
| `sqlite_parser.py` | `parse_sqlite` | `sqlite3.connect("file:path?mode=ro", uri=True)` 读 `sqlite_master`；有表 → tier1 | 无表 → tier3 | 1.0 / 0.0 |

- **I(x)** 是统一标尺但**每种格式的定义、精度、tier 边界并不一致**——详见 §3.3 横向对比。
- JSON 错误分类 `_classify_json_error` → `n_form`，供下游统计噪声类型分布。

### 3.3 I(x) 估算算法横向对比

把每个 parser 的 I(x) 算到底层算术式后，发现它们分属**三大族**，且部分公式代数化简后收敛为同一个量。

#### 各格式推导

- **JSON 部分崩溃（strict Level 2，`json_parser.py:40-50`）**：表面用 `total_lines` 反推
  `estimated_total`，代入化简（忽略取整/下界）后：

  ```
  estimated_total ≈ good · file_size / bytes_good
  ⇒  I ≈ bytes_good / file_size      （崩溃前消耗的字节占比）
  ```

  即 I 实质是"ijson 崩溃前吃进的字节比例"。`count_lines()` 的整文件流式扫描在代数上**完全抵消**，
  只影响取整粒度——GB 文件上是一次结果被约掉的额外读盘（见 §12.6）。护栏 `max(good+1, …)` 防止
  ijson 读缓冲让 `bytes_good≈file_size` 时假成 I=1.0：强制 `estimated_total≥good+1`，封顶到
  `good/(good+1)<1` → 正确 tier2。因为是**估算值**，tier1 用 **0.99 容差**。

- **JSON json5 容错（`json_parser.py:158-170`）**：同族化简 `I ≈ head_bytes / fsize`（64KB 头覆盖整文件的比例）。
  小文件（`head_bytes ≥ fsize·0.99`）短路成 I=1.0；**视野仅 64KB 头**，与 Level 2 的整文件视野不同。

- **JSONL（`json_parser.py:122`）**：`I = good/(good+bad)`，整文件精确行计数，无外推 → 严格 **==1.0** 才 tier1。

- **SQL（`sql_parser.py:46`）**：`I = complete/total`，但**只看前 64KB**，且 I 的语义是
  "DDL/DML 语句纯度"——一堆合法 `SELECT` 也会拉低 I → tier2，**与"解析成功率"不是同一回事**。

- **CSV/TSV（`csv_parser.py:33-66`）**：`good_rows` 与 `total_rows` 在同一循环体**无条件同步自增**，
  故 **I 在所有非空路径恒 ≡ 1.0**（含 tier2 分支）。CSV 的可恢复性梯度**不在 I 里**，而在
  `n_struct`（列漂移变异系数 `_column_drift`）。

- **SQLite（`sqlite_parser.py:24`）**：二元 `I∈{0,1}`，无 tier2。

#### 三大族 + 对比表

| 格式 | I 公式 | 化简为 | 视野 | 精度 | tier1 门槛 | 可恢复性载体 |
|------|--------|--------|------|------|-----------|--------------|
| JSON L2 | `good/est_total` | **`bytes_good/file_size`** | 整文件 | 估算 | I≥**0.99** | I |
| JSON json5 | `recovered/total_units` | **`head_bytes/fsize`** | 64KB 头 | 估算 | I≥**0.99** | I |
| JSONL | `good/(good+bad)` | 本身 | 整文件 | **精确** | I**==1.0** | I |
| SQL | `complete/total` | 本身 | 64KB 头 | 精确* | I**==1.0** | I（*=DDL 纯度，非解析成功率）|
| CSV/TSV | `good_rows/total_rows` | **恒≡1.0** | 整文件 | 退化 | 列零漂移 | **`n_struct`**（非 I）|
| SQLite | 二元 | `{0,1}` | schema | 二元 | 有表 | 二元 |

1. **字节比例外推族**（JSON 两路径）：I≈消耗字节/总字节，分母是估算值 → 用 0.99 容差。
2. **精确计数比族**（JSONL、SQL）：分母精确 → 严格 ==1.0。
3. **退化/旁路族**（CSV 的 I 恒 1.0、梯度在 `n_struct`；SQLite 二元）。

> 由此 §3.2 那种"I≥0.99→tier1"的统一说法仅适用于 JSON 两路径；JSONL/SQL 是 ==1.0，CSV 看列漂移，
> SQLite 看有无表。下游若按 `I` 字段统一排序可恢复性，需注意 CSV/SQL 的语义偏差（见 §12.6/§12.7）。

---

## 4. 阶段2 — Schema 单元化提取（`src/extract/`）

执行三段：**`partition_file` → `build_schema_unit` → `build_vocab_table`**，由 `extract_all()` 编排。

### 4.1 两个阶段2 入口：内存版 vs 流式版

**内存版 `extract_all(tier1_grades, mode)`**（旧主入口，`extract_five_infos` 与测试仍用）：

```
tier1 grades ──► partition_file()  逐文件分片 → list[SchemaPartition]
             ──► build_schema_unit() 逐分片组装 → list[SchemaUnit]（含五类信息，全量驻留内存）
             ──► build_vocab_table() 跨单元同义聚类 → (VocabTable, uncertain)
             ──► _aggregate_global_view() → global_view
返回 (schema_units, vocab_table, global_view)
```

**流式版（CLI `extract`/`pipeline` 实际走这条，内存恒定 + 断点续跑）**：

```
grades.jsonl(tier1) ──► stream_schema_units()  逐文件 partition→build_unit→即时 append schema_units.jsonl
                    ──► finalize_from_units()   两遍流式读 schema_units.jsonl:
                            ① _aggregate_global_view(惰性迭代) → global_view（增量计数，内存恒定）
                            ② build_vocab_table(惰性迭代)     → (VocabTable, uncertain)（KeyEntry 入内存）
```

- 流式版只把"单文件的分片/unit"留在内存，写完即弃；`schema_units.jsonl` 每行一个 SchemaUnit。
- `finalize` 两遍读盘各自独立（`units_iter_factory()` 每次返回新迭代器）；全局聚合内存恒定，
  词表聚类内存为 O(字段条目数)——跨单元同义对齐的固有代价。
- 续跑：`stream_schema_units` 用 `done_source_files`（来自已写 `schema_units.jsonl` 的 `source_file`）跳过；
  ID 计数器用 `set_unit_counter(max_seq+1)` 续接，避免 `sch_NNNNN` 冲突。
- ⚠ JSON 重载会把 `pii_seed` 元组变成 list、`skeleton` 元组变 list——下游按 `pii[0]/pii[1]` 索引，
  对 tuple/list 一致，无影响。

`global_view` 关键字段：`shape_templates_B`（唯一骨架数）、`naming_templates_A`（唯一字段名数）、
`AB_ratio`、`pii_seeds_count`、`total_records_sampled`、`schema_unit_count`、`partition_total`、`uncertain_vocab`。

### 4.2 `partition_file(grade) → (list[SchemaPartition], PartitionStats)`

文件内 Schema 分片，按格式路由：

| 格式 | 分片策略 | `method` |
|------|----------|----------|
| JSON（显式包装 key） | `{key→list[dict]}` 顶层检测，每 key 一个 partition | `explicit_key` |
| JSON（顶层数组）/ JSONL | `structure_signature` 骨架聚类，每签名一桶 | `skeleton_cluster` |
| CSV/TSV | 整文件单 partition；列数标准差 >0.5 或首行 <2 列 → `noisy=True` | `single` |
| SQL 文本 | 流式扫描，按 CREATE/INSERT 表名分桶 | `table_name` |
| SQLite | `sqlite_master` 读表名，每表 `SELECT * LIMIT SAMPLE_PER_FILE` | `table_name` |

- 每桶最多采样 `SAMPLE_PER_FILE=1000` 条，`record_iter` 惰性流式。
- **JSON 读取宽容度与阶段1 对齐**（`_stream_json_records(path, encoding)` / `_detect_explicit_keys`）：
  ① ijson 二进制流式（UTF-8）快路径，崩溃前已产出则直接返回已得部分（防整文件重复）；
  ② 兜底按 `grade.encoding` 读文本 → `json` 严格 → `json5` 容错，恢复 **GBK 等非 UTF-8 编码 + JSON5 语法**。
  动机：避免"只靠容错才进 tier1 的 JSON"（GBK、脏 JSON）在分片阶段静默零产出（仍计 tier1 却无 schema 贡献）。
- 演进中的分片问题（误 split、64KB explicit-key 上限、`structure_signature` 顺序敏感）见 `todo_list.md`。

### 4.3 `build_schema_unit(partition, mode="template", sample_mode="off") → SchemaUnit`

消费 `record_iter`（一次性，`islice(iter, SAMPLE_PER_FILE)`），**单遍折叠遍历**组装五类信息：

- **路径折叠**：list 下标统一折叠成 `[]`（模板路径），五类信息共用同一套路径。`orders[0].amt`/`orders[1].amt` → `orders[].amt`。
- 单遍同时产出：`sig_counter`（逐记录签名 → B/AB_ratio）、`template_values`（元素级值聚合 → value 画像）、`dtype_seen`（每路径类型计数，null 不计入）。
- **ID 分配**：`unit_id = "sch_{N:05d}"`、`field_id = "f_{su_seq:05d}_{seq:02d}"`，单 run 内自增。
- **occurrence** 当前为占位符 `1.0`（`required` 恒 True），真值待 `optional_field_grouping`（见 todo）。
- **`sample_mode`**（`"off"`/`"raw"`/`"masked"`，CLI `--keep-samples`/`--mask-samples`）透传给字段
  画像聚合：`aggregate_profiles([profile_value(v) for v in values], values, sample_mode)`，详见 §4.6。

**两套字段主干方案**（`mode`，CLI `--field-mode`）：

| 维度 | `template`（B，默认） | `fold`（A） |
|------|----------------------|-------------|
| 字段主干 | `most_common` 签名展开的模板叶子路径（子集） | 全部折叠 leaf 路径并集（含元素级异构、少数派可选字段） |
| skeleton dtype | 单型 | 最高频 + 多型标记 `(path, dtype, {multi_type, dominant_ratio})` |
| topology | 裁剪到主干 | 主干=并集 |
| 取舍 | 干净主导形状、字段集有界 | 找回异构数组字段，但异构数组合并成"全 optional"超级 schema |

设计细节与决策见 `todo_list.md` 待完成项 5。

### 4.4 五类信息

| # | 信息 | 产出位置 | 说明 |
|---|------|----------|------|
| 一 | 结构骨架 | `skeleton.py:structure_signature` | 标量替换为类型标记 `<int>`/`<str>`，key 保留 → 可计数签名串（**非合法 JSON**） |
| 二 | 字段名词表 | 折叠路径汇入全局 `VocabTable` | `{key_name: {折叠路径}}`；A=唯一字段名数 |
| 三 | value 画像 | `value_profile.py:profile_value` + `aggregate_profiles` | 见 §4.6。**只存统计摘要，`profile_value` 即时丢原值**；样本保留默认关、仅 `--keep-samples` 显式开 |
| 四 | 拓扑 | `schema_unit.py:_build_topology_folded` | `{path: {depth, parent, siblings}}`；depth 仅按 `.` 深度（`[]` 不计层级） |
| 五 | PII 种子 | `pii_seed.py` | key 名命中 PII 关键词 → `high_conf` + 推断类型；长文本字段 → `needs_llm` |

### 4.5 `build_vocab_table(schema_units) → (VocabTable, uncertain_list)`

跨所有 SchemaUnit 做 key 语义对齐，产出跨表同义倒排表。三证据联合：

- **B**（value 画像相似，`profile_similarity`，阈值 0.7）+ **C**（PII 类型一致）→ Union-Find 粗聚类；
- **A**（字符串相似度，`SequenceMatcher`）→ 簇内校正 / 冲突检测（<0.45 标 uncertain）。

`profile_similarity` 当前为占位实现且**已错配**：它读单值键 `type`，而聚合后的 value_profile 没有
该键 → B 证据恒返回 0.0；其上的 `_initial_clusters_by_bc` 又是 O(n²) 全字段两两比且无日志，是阶段2
Pass2 **卡死的直接原因**。真实多维加权 + 分块/LSH 近线性化 + 立即止血护栏见 `todo_list.md` 待完成项 7。

---

## 4.6 信息三 · value 画像（`value_profile.py`）

> 单值画像 `profile_value(value)` 即时丢原值，只返回特征 dict；字段级由 `aggregate_profiles()` 汇总。
> **完备性（MECE）与语言精度分层解决**：完备性交给 Unicode 划分 + `other` 残差（与语言无关、可校验），
> 语言/脚本精度交给开放词表式 scripts 直方图（只枚举关心的 8 个脚本，其余诚实归 `Other`）。
> 两条轴共用同一个 `_macro_class`/`_script_of`，不存在两套分类器漂移。

### `profile_value(value) → dict`（单值，单趟遍历，NFC 归一）

字符串走 `_str_profile`，**一趟**同时产出三件套：

| 键 | 内容 |
|----|------|
| `len` | NFC 归一后字符数 |
| `char_dist` | 7 个**互斥穷尽宏桶**百分比（基于 `unicodedata.category` 首字母）：`number`/`letter`/`mark`/`punct`/`symbol`/`space`/`other`——`*_pct` 之和**恒为 1**（含 Emoji/重音/未分配，无漏网）。`isspace()` 优先归 `space` |
| `pattern` | 同源游程压缩模式模板（与 `char_dist` 同一分类函数）。token：数字`D`、标记`M`、标点`P`、符号`S`、空白` `、其它`?`；**字母再按粗脚本细分**——Han→`C`、Latin→`L`、其它脚本→`X`。如 `"abc123"`→`L{3}D{3}`、`"张三"`→`C{2}` |
| `scripts` | 仅对 `letter` 部分统计的脚本直方图（占比），轻量覆盖 8 脚本 `Han/Hiragana/Katakana/Hangul/Latin/Cyrillic/Arabic/Greek`，其余归 `Other`（bisect 命中 `_SCRIPT_RANGES` 区间表） |

- 数值/bool → `value_range_hint`（`bool`/`year_like`/`age_like`/`large_int`/`int`/`float`）；
  null → `{type:"null", len:0}`；list/dict 只记结构（`len`/`elem_type`/`keys[:20]`）。

### `aggregate_profiles(profiles, raw_values=None, sample_mode="off") → dict`（字段级）

| 键 | 内容 |
|----|------|
| `sample_count` | 该字段采样值条数 |
| `len_dist` | `min/max/mean/median/std`（`statistics.median`/`pstdev`） |
| `top_patterns` / `unique_patterns` | pattern 频次 top-10 + 去重类别数 |
| `avg_char_dist` | 7 宏桶逐键均值（固定键，下游可直接当向量比较） |
| `avg_scripts` | scripts 直方图均值（开放词表，取并集键、缺失计 0） |
| `samples` | **仅 `sample_mode!="off"` 且给定 `raw_values` 时落地** |

**样本保留（守 PII 红线，默认关）**：`sample_mode`
- `"off"`（默认）：不产 `samples`，`profile_value` 永不持原值——红线默认成立。
- `"raw"`（`--keep-samples`）：按 `pattern` 去重（标量回退 `type`），每类留**首个**代表，
  **优先覆盖不同 pattern 类别**，最多 `SAMPLE_MAX=5`（类别 >5 任取 5 种）；null 不取样。⚠ owner 授权的显式落原值例外。
- `"masked"`（`--keep-samples --mask-samples`）：在 `"raw"` 基础上 `_mask` 脱敏——**保留分隔符/空白与长度、
  内容字符（字母/数字/符号/标记）→ `*`**，不落原始字符。如 `user@example.com`→`****@*******.***`、
  `2024-01-02`→`****-**-**`、`张三`→`**`；非字符串标量保位数结构、字母数字打码（`bool`→`<bool>`）。

> `profile_similarity`（§4.5）的真实度量将复用 `avg_char_dist`（合法概率分布，可做余弦/JSD）、
> `len_dist`、`top_patterns`（Jaccard）、`avg_scripts`——见 `todo_list.md` 待完成项 7.3。

---

## 5. 共享数据结构（`schema_types.py`，TypedDict）

> 用 TypedDict 而非 dataclass：结构以 dict 形态流动并序列化 JSON，TypedDict 在保持 `obj["key"]`
> 运行态不变的前提下给出静态契约（IDE 补全 + mypy 查 key 拼写）。

| 类型 | 产出 → 输入 | 关键字段 |
|------|-----------|----------|
| `SchemaPartition` | `partition_file` → `build_schema_unit` | `source_file`, `format`, `partition_id`, `record_iter`(⚠惰性一次性), `noisy`, `field_paths`, `occurrence` |
| `PartitionStats` | `partition_file` 副产物 | `partition_count`, `partition_ids`, `method` |
| `SchemaUnit` | `build_schema_unit` → `build_vocab_table` | `id`(sch_NNNNN), `skeleton`, `skeleton_count_B`, `skeleton_counts`, `topology`, `fields`, `record_count` |
| `FieldInfo` | `SchemaUnit.fields` 的值 | `field_id`, `key_name`, `occurrence`, `required`, `value_profile`(⚠非原值), `pii_seed` |
| `VocabTable` | `build_vocab_table` 产出 | `VocabTable[semantic_class][key_variant] = [schema_unit_id, …]`（任意键倒排，类型别名） |
| `KeyEntry` | `build_vocab_table` 内部 | 字段判别线索：key_name/path/schema_unit_id/field_id/value_profile/pii_seed |

---

## 6. 两个提取入口的区别（勿混用）

- `extract_all()`：**新主入口**，Schema 单元化、带溯源（每字段可追到来源文件/表）。
- `extract_five_infos()`：**兼容薄包装**，跑 `extract_all` 后把每个 SchemaUnit 拍平成旧的全局扁平五类信息 dict
  （路径用折叠模板路径；value_profiles/topology 跨分片合并，**同名路径后者覆盖**；**无溯源**）。新代码勿用。

两者输出格式不同，禁止混用。

---

## 7. 日志系统（`utils/logger.py`）

- `setup_logger(name="pii_detect", level=INFO, stream=True, file=None)`：stdout + 可选 UTF-8 文件双输出，
  格式 `"HH:MM:SS [LEVEL] message"`，**按 handler 类型幂等**（可在已有 stream handler 上后续追加 file handler，并刷新 level）。
- `get_logger(name)` → `pii_detect.{name}` 命名子 logger，自身无 handler，向上 propagate 到 `pii_detect` 根。
- **命名空间打通**：各子模块顶部 `log = get_logger(__name__)`；CLI 在 `main.py:_configure_logging()` 一处
  `setup_logger("pii_detect", level=…, file=…)` 配置根 logger 的级别与 file handler。因此**只需 CLI 一处挂 file handler
  即可收齐全流水线（sniff/parse/extract 全部子模块）日志**，同时终端打印。
- **级别约定**：INFO=阶段起止/进度/全局摘要/WARNING/ERROR；DEBUG=逐文件吞错、投票得分、分片方法、骨架解析（`-v` 开）。
- `main.py` 内 `print` 已全部收口为 `log.*`（报表行、阶段标题、输出路径）。

---

## 8. CLI 输入容错（`main.py:resolve_input`）

`root` 宽限解析：

1. 单文件 → 返回 `[path]`；
2. 目录 → 递归 `walk_files` 遍历；
3. 空目录 → 向上回溯父目录，逐层查找直到找到文件或到文件系统根；
4. 全程无文件 → 返回 `[]` 并记 error，命令终止。

四命令均在启动时调用，路径为空即终止。

---

## 9. 产出文件清单

| 命令 | 流式中间产物（追加写，续跑载体） | 汇总产出（结束时整体写） | 日志 |
|------|------|------|------|
| `sniff` | — | `sniff_report.json` | `sniff.log` |
| `parse` | `grades.jsonl` | `parse_report.json` | `parse.log` |
| `extract` | `grades.jsonl` + `schema_units.jsonl` | `extract_report.json` + `vocab_table.json` | `extract.log` |
| `pipeline` | `grades.jsonl` + `schema_units.jsonl` | `pipeline_report.json` + `vocab_table.json` | `pipeline.log` |

- **`grades.jsonl`**：阶段1 每行一文件 `{path, fmt, encoding, tier, I, conf, n_form, n_struct, note, error}`。
- **`schema_units.jsonl`**：阶段2 每行一个 SchemaUnit（**取代旧的整文件 `schema_units.json`**，流式追加、崩溃不损坏）。
- `*_report.json` / `vocab_table.json` 是小体量汇总，结束时一次性写出。
- 续跑：保留中间产物重跑同命令即自动接续；`--restart` 删除中间产物从头来。

---

## 10. PII 检测关键词（`constants.py:PII_KEY_PATTERN`）

中英双语覆盖：`name/phone/email/mail/id_card/ssn/social.security/address/passport/birth/gender/
mobile/tel/contact`、`身份证/姓名/电话/邮箱/地址/密码/手机/生日/性别/年龄/籍贯/民族/住址`、
`card_no/bank/account/credit/ip_addr/mac/imei/uuid/token/secret/password/pwd`。

- key 名命中 → `high_conf`，`infer_pii_type` 推断具体类型；
- 字段值平均长度 > `FREE_TEXT_AVG_LEN_THRESHOLD(100)` 的自由文本 → `needs_llm`（留待强模型）。

---

## 11. 二进制检测（`file_utils.py:is_binary`）

- NULL 字节占比 > 1/256 → 强二进制信号；
- 控制字符（0x00-08,0x0B-0C,0x0E-1F,0x7F）占比 > `MAX_BINARY_RATIO(0.3)` → 二进制；
- **UTF-8 中文字节（≥0x80）不计入控制字符**，避免中文文本误判为二进制。

---

## 12. 已知问题与边界

1. **日志含逗号时间戳**（`[2024-01-01 00:00:00,123]`）→ CSV 投票(0.9) 可能压制 log 投票(0.85)，`.log` 误判为 CSV。
2. **短 GBK 文件**：chardet 可能误判为 `cp1250`/`windows-1250`（代码页邻接）。分片阶段已忠实用 `grade.encoding`
   读取（记录可恢复），但若 sniff 探错编码，value 文本仍乱码——残留在 sniff 编码探测层，结构/key/PII 判定不受影响（key 多为 ASCII）。
3. **非 .sql 扩展名的 SQL**：INSERT 逗号值与 CSV 模式竞争，SQL(0.7) 可能输给 CSV(0.9)。
4. **JSON 误 split / 64KB explicit-key 上限 / 大顶层 object 丢弃**：见 `todo_list.md` 待完成项 3（Tier A/B 方案存档）。
5. **签名基数爆炸 → 阶段2 Pass2 卡死**：`structure_signature` 作分桶 key 时，可选字段/可空/类型漂移会把
   同一张逻辑表炸成 2ᵏ 个签名桶（实测尾段 ~458 unit/文件，~80×）；叠加 `vocab_table._initial_clusters_by_bc`
   的 O(n²) 全字段两两比 + `profile_similarity` 键错配恒返回 0（无日志、纯空转）→ 终端静默卡死。
   根因诊断、无损收敛（fold-union 而非砍长尾）、分块/LSH 近线性化与**立即止血护栏**见 `todo_list.md` 待完成项 7。
6. **CSV 的 I 恒为 1.0，与 tier 语义不符**（`csv_parser.py`）：`good_rows`/`total_rows` 同步自增，
   tier2 的 CSV 仍报 `I=1.0`，违反 `Grade` docstring "tier2 ⇒ 0<I<1"。劣化信号正确落在 `n_struct`，
   但下游若仅按 `I` 排序会把列漂移的脏 CSV 当成完美数据。修法：tier2 用 `1-n_struct` 之类映射进 I，或
   下游显式区分"按 I（JSON/JSONL）"与"按 n_struct（CSV）"两条可恢复性轴。
7. **SQL 的 I 是"DDL 纯度"而非"解析成功率"**（`sql_parser.py`）：与 JSON/JSONL 的 I 同名不同义，
   一个全是合法 `SELECT` 的分析型 SQL 会被判 tier2；且只采前 64KB，I 实为头部统计量。
8. **JSON Level 2 的 `count_lines()` 整文件扫描冗余**：其结果在 `estimated_total` 公式里代数抵消
   （化简为 `good·file_size/bytes_good`），GB 文件上是一次几乎零信息增益的额外读盘，可直接用
   `I = bytes_good/file_size` + 同样的 `max(good+1,…)` 护栏替代。

---

## 13. 扩展指南

### 新增格式

1. `sniff/voting.py:vote_format()` 加投票逻辑；
2. `sniff/sniffer.py:sniff_file()` 加优先检查（如有魔数/扩展名先验）；
3. `parse/` 新建 parser，实现 strict + tolerant；
4. `parse/grade.py:grade_parse()` 加路由分支；
5. `extract/schema_partition.py:partition_file()` 加分片分支；
6. `test_data/generate.py` + `tests/` 加样本与用例。

### 新增 PII 类型

1. `constants.py:PII_KEY_PATTERN` 加关键词；
2. `extract/pii_seed.py:infer_pii_type` 加 `(pattern, pii_type)` 映射。

---

## 14. `parsers/` 独立解析器系统（参考实现，勿 import）

`parsers/` 是独立于 `src/` 的 LLM 驱动解析器系统，**Gateway 模式**，依赖不在本仓库的外部框架
（`core.base.BaseParser`、`core.llm_engine.LLMMappingEngine`、`core.format_detector.FormatDetector`），
直接 import 会失败。与 `src/` 流水线无调用关系，仅作参考：

- `txt_parser.py` — Gateway 入口：FormatDetector 检测 → 路由到 Json/Csv/SqlParser；
- `json_parser.py` — 流式 JSON + flatten 嵌套 key，采样做 Schema 发现，LLM 映射后输出标准化 CSV；
- `csv_parser.py` — Pandas 安全读（含 NUL 清洗回退）+ 单列/多列组合映射；
- `sql_parser.py` — 流式拆 CREATE/INSERT → 按表名分写 CSV → 委托 CsvParser 清洗。
