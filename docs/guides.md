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

单文件真实格式判定。**设计取舍**：真实语料中 json/csv/jsonl/sql/tsv/xlsx 等结构化文件 ~99% 后缀与内容一致（噪声留给阶段1 容错解析暴露，不在嗅探期纠格式），故对这些扩展名**直接信任**；仅 `.txt`/`.log`/无扩展名等无强先验文件才做内容投票。流程（短路顺序）：

1. 扩展名命中 `_TRUSTED_EXT`（json/jsonl/ndjson/csv/tsv/sql/xlsx）→ 直接返回该格式，conf=1.0。
   - `xlsx` 为二进制：`("xlsx","binary",1.0)`，不探编码。
   - 其余文本类：`_detect_text_encoding()` 先看 BOM（UTF-16/32），否则 chardet → 返回 `(fmt, enc, 1.0)`。
2. 其余扩展名走 `_sniff_by_content()`：
   - 读前 1KB，`is_binary()`（无 BOM 时）判二进制 → `("binary_unknown","binary",0.6)`。
   - 读 64KB 头部（`SNIFF_HEAD_BYTES`）→ BOM/chardet 探编码 → `safe_decode(errors="replace")` → 取前 20 非空行（`SNIFF_LINES`）。
   - 多候选加权投票 `vote_format()`，取最高分；`conf < ACCEPT_THRESHOLD(0.5)` → 归 `free_text`（纯空白文件无非空行 → 全 0 分 → 同样落 `free_text`）。

`real_format` 取值域：`json|jsonl|csv|tsv|sql|xlsx|log|free_text|binary_unknown`。

> **0 字节文件不入嗅探**：统一约定在各命令处理起点按文件大小跳过 0 字节文件，`sniff_file` 不再处理 `empty`（已删两个 `empty` 返回分支）。落地两处：parse/extract/pipeline 在 sniff 前按 `st_size==0` 跳过并计 `skipped_empty`（`main.py:150`）；`sniff` 命令在 `profile_corpus` 遍历起点按 `file_size==0` 跳过（同样计 `skipped_empty`）。故 `empty` 不再是任何输出里的格式取值。
> `.db`/`.sqlite` 二进制路径已移除（真实样例太少）；若遇到 `.db` 文件会落到内容投票路径并多半判 `binary_unknown`。

### 2.2 `voting.py` · `vote_format(lines, full_text) → {fmt: score}`

7 候选并行打分（分数可叠加，非互斥）。权重表：

| 格式 | 判据 | 加分 |
|------|------|------|
| `json` | strip 后首字符是 `{`/`[` | +0.6；括号闭合再 +0.3 |
| `jsonl` | 每行能独立 `json.loads` 的比例 > 0.8 | +0.9 |
| `csv`/`tsv` | 分隔符（`\t`/`,`/`;`/`\|`/`:`）众数列数 ≥2 且命中众数的行占比 ≥0.7（`column_profile`，容忍少数 value 内嵌分隔符的漂移行） | +0.8；首行像表头再 +0.1 |
| `sql` | 关键词起一行（任意行）+ 文件含 `;` 结尾语句 → 强信号；否则仅 `SQL_KEYWORD_PATTERN` 命中或语句起行 | 强 +0.95；弱 +0.7 |
| `log` | 行首时间戳 + 级别双命中（`LOG_TS_PREFIX`+`LOG_LEVEL`）比例 >0.6 → 强信号；否则仅 `LOG_PATTERN` 命中比例 >0.6 | 强 +0.95；弱 +0.85 |
| `free_text` | 平均行长 >40 + 句末标点 + 无强结构信号 | +0.5 |

> **CSV/TSV 的"众数列数"判据本身即一种容错解析**（`column_profile`）：不要求每行列数严格一致，而以"最常见列数 + 命中占比 ≥0.7"做多数表决，从而**容忍少数因 value 内嵌分隔符（如带逗号的引号字段）而漂移的行**，无表头 CSV 也适用。这与项目"嗅探期不纠格式、噪声留给阶段1 暴露"的总基调一致——嗅探阶段先用容错判据把"伪装成 .txt 的表格"稳健路由到 csv/tsv，真正的列漂移度量与可恢复性分级交由阶段1 `csv_parser` 的 `n_struct`（列漂移变异系数）承担（见 §3.2/§3.3）。同理 SQL/log 的强弱信号分档也是对噪声的容错权衡。

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
| `I` | 官方退化曲线 $I=(C+P)/N$（通道一占比）；非结构化（log/free_text）为 `None` |
| `I_strict` | **种子门** $C/N$（严格内核直接消费成功的单元占比）；`tier1 ⟺ I_strict==1 ⟺ P==0 ∧ L==0`。容错救回（json5/列漂移/partial）使 `I_strict<1`，**封顶 tier2**，绝不漏进种子库。非结构化/未实现为 `None` |
| `parsed` | 解析摘要（dict），非原始数据 |
| `fmt` / `encoding` / `path` | 格式 / 编码 / 文件路径 |
| `error` | 失败原因（tier3） |
| `n_form` | JSON 错误分类（trailing_comma/single_quotes/…） |
| `n_struct` | CSV 列漂移度量（变异系数） |
| `note` | 备注 |

`grade_parse(path, real_format, enc)`：按 `real_format` 路由到对应 parser（延迟 import 避免循环依赖），统一回填 `path`。`log`/`free_text` 直接构造弱结构 Grade；`binary_unknown`/`empty` 及其他不可解析格式 → tier3。

### 3.2 各 parser 策略

| parser | 入口 | strict（种子门 $C$） | tolerant（$P$/$L$ 切分） | I(x) / I_strict |
|--------|------|--------|----------|-----------|
| `json_parser.py` | `parse_json` | ijson 流式**全程零异常**消费到 EOF → tier1, `I_strict=1` | 崩溃前 $C$、崩溃点后估 $L$（partial-array，封顶 tier2）；good==0 先探 JSONL-as-`.json`，否则 json5 救回（注释/单引号/尾逗号）全计 $P$（封顶 tier2，`I_strict=0`） | `I=(C+P)/N`，`I_strict=C/N` |
| | `parse_jsonl` | 逐行 `json.loads`，`bad==0` → tier1, `I_strict=1` | 坏行跳过计 $L$（无修复态，$P=0$） | `I=I_strict=good/(good+bad)` |
| `csv_parser.py` | `parse_csv`/`parse_tsv` | **quote-aware** `csv.reader` + 列数全等 → tier1, `I_strict=1` | 列漂移按众数重对齐：偏离众数仍成行 → $P$，reader 丢的行 → $L$（封顶 tier2，记 `n_struct`） | `I=(C+P)/N`，`I_strict=列全等?1:C/N`（修旧 `I≡1.0`） |
| `sql_parser.py` | `parse_sql_text` | `scan_sql` 状态机流式扫到 EOF，全语句平衡+可归类 → tier1, `I_strict=1` | regex 在语句边界抽 schema：损坏但抽出计 $P$，截断/抽不出计 $L$ | `I=(C+P)/N`，`I_strict=C/N` |
| `xlsx_parser.py` | `parse_xlsx` | **strict-only**：openpyxl `read_only` 打开+sheet 可枚举+表头可读 → tier1, `I_strict=1` | openpyxl 缺失/打开失败/无 sheet → tier3, `I_strict=0`（二进制无容错中间态，`strict==tolerant`） | `I=I_strict∈{0,1}` |

- **I(x)** 是统一标尺；`I_strict=C/N` 为种子门，`tier1 ⟺ I_strict==1`。每种格式的视野/精度详见 §3.3 横向对比。
- **quote-aware**：CSV 切列与 `_sniff_delimiter` 嗅探均用 `csv.reader`（尊重引号内分隔符/换行/`""` 转义），禁 `split(sep)`/`count(sep)`——列数是最基本结构不变量。
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

- **xlsx（`xlsx_parser.py`）**：二元 `I∈{0,1}`，无 tier2——有 sheet → 1.0/tier1，openpyxl 缺失或打开失败 → 0.0/tier3。

#### 三大族 + 对比表

| 格式 | I 公式 | 化简为 | 视野 | 精度 | tier1 门槛 | 可恢复性载体 |
|------|--------|--------|------|------|-----------|--------------|
| JSON L2 | `good/est_total` | **`bytes_good/file_size`** | 整文件 | 估算 | I≥**0.99** | I |
| JSON json5 | `recovered/total_units` | **`head_bytes/fsize`** | 64KB 头 | 估算 | I≥**0.99** | I |
| JSONL | `good/(good+bad)` | 本身 | 整文件 | **精确** | I**==1.0** | I |
| SQL | `complete/total` | 本身 | 64KB 头 | 精确* | I**==1.0** | I（*=DDL 纯度，非解析成功率）|
| CSV/TSV | `good_rows/total_rows` | **恒≡1.0** | 整文件 | 退化 | 列零漂移 | **`n_struct`**（非 I）|
| xlsx | 二元 | `{0,1}` | sheet | 二元 | 有 sheet | 二元 |

1. **字节比例外推族**（JSON 两路径）：I≈消耗字节/总字节，分母是估算值 → 用 0.99 容差。
2. **精确计数比族**（JSONL、SQL）：分母精确 → 严格 ==1.0。
3. **退化/旁路族**（CSV 的 I 恒 1.0、梯度在 `n_struct`；xlsx 二元）。

> 由此 §3.2 那种"I≥0.99→tier1"的统一说法仅适用于 JSON 两路径；JSONL/SQL 是 ==1.0，CSV 看列漂移，
> xlsx 看有无 sheet。下游若按 `I` 字段统一排序可恢复性，需注意 CSV/SQL 的语义偏差（见 §12.6/§12.7）。

#### I_strict 命门：tier 由严格门把关（Phase 2/3 后）

解耦后**真正决定 tier 的是 `I_strict=C/N`（种子门），不再是上表那些口径各异的 `I`**：

```
tier1 ⟺ I_strict == 1  ⟺ deviations == 0  ⟺ 严格内核全程零偏离消费到 EOF
```

`I=(C+P)/N` 仍是官方退化曲线（通道一占比），但容错救回的 `P` 不再能把文件抬进 tier1——
任何 json5/列重对齐/partial 修复都使 `I_strict<1`、**封顶 tier2**。命门属性
`strict_ok(x) ⟺ I_strict==1 ⟺ deviations==0` 在六格式上一致成立（Phase 4 抽出
`strict_ok`/`tolerant_parse` 两入口后由共享内核保证逐字节同源）：

| 格式 | $C$ 定义（严格门通过的单元） | tier1 ⟺ |
|------|------|------|
| JSON | ijson 全程零异常消费到 EOF 的元素 | `I_strict==1`（json5/partial 一律封顶 tier2） |
| JSONL | 逐行 `json.loads` 成功的行 | `bad==0` |
| CSV/TSV | quote-aware reader 无错 ∧ 列全等 | 列零漂移 |
| SQL | `scan_sql` 平衡且可归类的语句 | 零截断 ∧ 全平衡 |
| xlsx | 打开+sheet+表头可读（二元，`strict==tolerant`） | 可读 |

> 二进制 xlsx 无容错中间态，命门退化为「能否打开读表头」的二元判定（`I=I_strict∈{0,1}`）。

### 3.4 严格/容错两入口 + 共享内核（`src/parse/recovery.py`）

解耦的是**契约/角色**，不是两遍扫文件。各格式 `parse_X()` 即**共享内核**：一遍流式/状态机扫描
同时产出严格门计数 $C$ 与容错救回计数 $P/L$，封进同一个 `Grade`。`recovery.py` 在其上导出两入口：

| 入口 | 契约 | 角色 | 准则 |
|------|------|------|------|
| `strict_ok(path,fmt,enc)` | `StrictVerdict{clean, reason, n_unit}` | 种子门 oracle（离线建 Tier1 种子库） | 对 clean 高精度 / fail-closed |
| `tolerant_parse(path,fmt,enc)` | `ParseResult{units, raw_spans, report:RecoveryReport}` | IR 产生器（训练/推理 tier 标定） | 宁可少救不可错救 |

- `RecoveryReport{C,P,L,N,I,I_strict,tier,deviations}`，`deviations = P+L = N-C`。
- **命门（健全性）**：两入口都从同一 `Grade` 派生 $C/P/L$，故
  `strict_ok(x).clean ⟺ tolerant_parse(x).report.deviations==0 ⟺ I_strict==1` 逐字节同源恒成立——
  退化曲线起点 $I(x_{clean})=1$ 不歪。
- `_cpl_from_grade()` 从 `n_detail.{c_count,p_count,l_count,n_total}` 还原 $C/P/L$；干净 tier1 无明细时 $C=N$。
- **类型用 TypedDict**（`StrictVerdict`/`RecoveryReport`/`ParseResult`），仅 `Grade` 沿用 dataclass。
- **SQL GB 流式**：`sql_strict.iter_sql_file_statements()` 惰性逐条 yield 语句，`parse_sql_text` 单遍累加
  $C/P/L$ + `has_create/insert` + scan 计数，**不 `list()` 物化全部语句**（GB SQL dump 内存恒定）。
  `scan_sql_file()`（物化版）仅留给小文件/测试。

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

B 证据聚类已从 O(n²) 全字段两两比改为 **`(type, len_band)` 分桶 blocking**（`_profile_bucket_key`，近线性）——
真实语料上跨全部 unit 的字段数可达百万，旧全配对是阶段2 Pass2 **历史卡死的直接原因**，现已止血。
`profile_similarity` 保留为 helper（占位语义），真实多维加权度量见 `todo_list.md` 待完成项 7。
> ⚠ 注：vocab_table 是**全局跨表分析产物**，**不属于** IR 单元（§4.7）；建 IR 数据集时此步整段不跑。

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

## 4.7 IR 数据集单元（喂给鲁棒编码器的输入投影）

研究目标是训练**结构鲁棒编码器**：把噪声异构记录编码成表征供 PII 检测 / schema 理解。喂给编码器的
**每单元 IR** 不是 §4.4 的全部五类信息，而是其中「输入 x」那一子集。SchemaUnit 是承载体，**IR 单元 =
SchemaUnit 的投影**，逐文件流式产出（`stream_schema_units` → `schema_units.jsonl`），**不做全局 join**。

| 槽位 | 来源 | 取舍 |
|------|------|------|
| 身份/溯源 `id`/`source_file`/`partition_id`/`format`/`record_count` | SchemaUnit 自带 | 保留：廉价且必需（per-unit 去重 / 加权 / 溯源） |
| **结构** `skeleton` + `skeleton_counts` | 信息一 | 核心 x。**嵌套已编码在折叠路径里**（`orders[].amt`），拓扑是它的派生视图 |
| **拓扑** `topology` | 信息四 | 与骨架冗余：编码器若直接吃路径串可省；需显式喂树结构再留 |
| **值证据** `fields[path].samples` | 信息三的**样本通道** | **直接用值样本**（见下），不用统计画像作输入 |
| ~~字段名词表 vocab_table~~ | 信息二 | **删**：全局跨表 join，非单元 IR；也是 Pass2 历史卡死那步（§4.5） |
| PII 种子 `pii_seed` | 信息五 | 可选**弱标签通道**（是 y 不是 x），由 key 名+样本派生、可后算；建 IR 时可不带 |

**值证据 = 值样本（本研究决定）**：不以 §4.6 的统计画像（`len_dist`/`avg_char_dist`/…）作编码器输入，而是
**直接保留样本值**——让编码器从原始样本自学「这像 email / 身份证 / 时间戳」，比预聚合分布更利于鲁棒表征。
落地复用 `aggregate_profiles` 的样本通道（§4.6），**建 IR 必须显式开 `--keep-samples`**（默认 `off` 不产样本、不适用于 IR）：
- `sample_mode="raw"`（`--keep-samples`）：按 `pattern` 去重留 ≤5 个代表样本（优先覆盖不同 pattern 类别）。
- `sample_mode="masked"`（`--keep-samples --mask-samples`，**推荐**）：保留长度/分隔符/字符宏类、内容字符打码
  （`user@ex.com`→`****@*****.***`），既给编码器形态信号又守 PII 红线（owner 授权下可换 `raw`）。

**与 pipeline 的关系**：删信息二后，阶段2 退化为纯逐文件流式——`stream_schema_units` 产出的
`schema_units.jsonl` **就是 IR 数据集**；`finalize_from_units` 的全局 `vocab_table`/`global_view` 是语料
分析产物、非编码器输入，建 IR 时整段不跑，跨单元聚类的固有成本随之消失。

**训练 vs 推理（同一套解析器、不同门控）**：
- 建训练集：只取 tier1（`I_strict==1`）干净种子投影成 IR（干净 x，噪声靠合成增广）。
- 推理：输入是真实噪声，走**容错通道**把 (C+P) 恢复进 IR、L 残片标 `[RAW]`，**不走 tier1 过滤**
  （严格门只为选训练种子，不参与单样本编码）。

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

- **`grades.jsonl`**：阶段1 每行一文件 `{path, fmt, encoding, tier, I, conf, n_form, n_struct, n_detail, note, error}`。
  - `n_detail`：tier2 失败的**结构化**诊断（供噪声分布聚类），按格式不同：JSON `{kind:partial_array, reason, offset}`、JSONL `{kind:jsonl_parse_error, bad_count, samples:[{lineno,err,len}]}`、CSV `{kind:col_drift|header_col_mismatch|read_interrupted, drift, modal_cols, header_cols, col_hist}`、SQL `{dialect, dialect_status, dialect_scores}`（所有 tier 都有；`dialect_status∈{confident,ambiguous,weak,unknown}`，后三者供人工排查）+ tier2 追加 `{kind:sql_incomplete, complete, total, unclosed_quotes}`。**只存错误串/偏移/长度/列分布，不落原始内容（守 PII 红线）**。
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
6. ~~**CSV 的 I 恒为 1.0，与 tier 语义不符**~~（**Phase 2 已修**，`csv_parser.py`）：引入 `I_strict=列全等?1:C/N`
   （$C$=众数列且 reader 无错的行），种子门不再恒 1；`I=(C+P)/N` 仍可能为 1（所有行可恢复时），但 tier 由 `I_strict` 把关，
   列漂移 CSV 必 `I_strict<1`→封顶 tier2。下游排序可恢复性请用 `I_strict`（严格）+ `n_struct`（漂移幅度）。
   ~~**JSON tier1 泄漏**~~（**Phase 2 已修**）：容错路径（json5/partial）封顶 tier2，`tier1 ⟺ I_strict==1`，修过的文件不再漏进种子库。
   ~~**JSONL-as-`.json` 静默零产出**~~（**Phase 2 已修**）：`parse_json` good==0 时 `_looks_like_jsonl` 探测逐行独立对象，是则转 JSONL 迭代（非零产出）。
   ~~**JSON 早期崩溃后续不计**~~（**Phase 2 已修**）：partial-array 把崩溃点之后的记录纳入 $L$ 计数（`l_count`）。
7. ~~**SQL 的 I 是"DDL 纯度"而非"解析成功率"**~~（**Phase 1/4 已重构**，`sql_parser.py`）：`I` 改为语句级
   $(C+P)/N$（`scan_sql` 状态机平衡判定 + regex 抽 schema），不再是关键词纯度；`I_strict=C/N` 为种子门。
   ~~**SQL 全量物化 statements**~~（**Phase 4 已修**）：`parse_sql_text` 改为惰性消费
   `iter_sql_file_statements`，单遍累加 $C/P/L$，不 `list()` 物化全部语句（GB SQL dump 内存恒定）。
   ~~**只采前 64KB**~~：`scan_sql` 流式扫到 EOF，尾部截断不再漏判（方言探测仍只读 64KB 头，弱元数据不参与严格判定）。
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
