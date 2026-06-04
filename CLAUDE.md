# CLAUDE.md — PII Detect 项目上下文

## 项目概述

本项目是一个**多源异构文件的数据预处理与信息抽取流水线**，核心功能为：从混合格式文件（JSON/CSV/TSV/SQL/SQLite/TXT/日志）中嗅探真实格式、容错解析并提取五类结构信息及 PII 种子。

- **语言**: Python 3.12+
- **包管理**: uv (pyproject.toml)
- **核心依赖**: chardet, ijson, json5, pytest
- **数据规模**: ~45,473 文件, GB 量级（真实数据在云服务器上，当前不可下载）
- **工作目录**: `D:\Python_project\detect`

## 关键约束

- **流式/抽样**：禁止整文件 load，一律流式读或只读头部
- **编码混合**：GBK/UTF-8 混存，chardet 探编码，`errors='replace'` 解码
- **扩展名不可信**：格式判据是内容嗅探而非文件扩展名，扩展名仅作弱先验
- **禁止持久化 PII 原值**：value 画像只存统计摘要（长度分布、字符类分布、模式模板），不存原始值
- **SQLite 独立路径**：`.db` 走 sqlite3 二进制路径，不可文本解析

## 项目结构

```
D:\Python_project\detect\
├── main.py                       # CLI 入口 (argparse 子命令) + resolve_input() 容错路径解析
├── pyproject.toml                # uv 包管理 + 依赖声明
├── src/
│   ├── __init__.py
│   ├── constants.py              # 阈值常量、PII 正則、魔数定义
│   ├── sniff/                    # [阶段0] 内容嗅探
│   │   ├── __init__.py
│   │   ├── sniffer.py            # sniff_file(path) → (fmt, enc, conf)
│   │   ├── voting.py             # vote_format(lines, text) → {fmt: score}
│   │   └── profiler.py           # profile_corpus(root, files=None) → 交叉表
│   ├── parse/                    # [阶段1] 容错分级解析
│   │   ├── __init__.py
│   │   ├── grade.py              # Grade dataclass + grade_parse() 路由
│   │   ├── json_parser.py        # JSON/JSONL: strict(ijson) + tolerant(json5)
│   │   ├── csv_parser.py         # CSV/TSV: strict(列一致) + tolerant(skip bad lines)
│   │   ├── sql_parser.py         # SQL 文本: regex 抽 CREATE/INSERT 头部
│   │   └── sqlite_parser.py      # SQLite: sqlite3.connect 读 schema
│   ├── extract/                  # [阶段2] Schema 单元化提取 (仅 tier1)
│   │   ├── __init__.py
│   │   ├── extractor.py          # extract_all() 主入口 + extract_five_infos() (extract_all 的薄包装)
│   │   ├── schema_types.py       # 共享 TypedDict: SchemaPartition / SchemaUnit / FieldInfo / VocabTable
│   │   ├── schema_partition.py   # partition_file() — 文件内 Schema 分片
│   │   ├── schema_unit.py        # build_schema_unit() — 单遍折叠遍历组装五类信息
│   │   ├── vocab_table.py        # build_vocab_table() — 跨表同义倒排
│   │   ├── skeleton.py           # 信息一: 结构骨架 (structure_signature, 供分片聚类 + B 计数)
│   │   ├── vocabulary.py         # 信息二: 字段名词表 (build_vocabulary, 仅旧 extract_five_infos 用)
│   │   ├── value_profile.py      # 信息三: value 画像 (统计摘要，不存原值)
│   │   ├── topology.py           # 信息四: 拓扑 (build_topology, 仅旧 extract_five_infos 用)
│   │   └── pii_seed.py           # 信息五: PII 种子 (key 名推断 + free_text 标记)
│   └── utils/
│       ├── logger.py             # 日志模块: setup_logger(), get_logger()
│       ├── encoding.py           # chardet 探编码 + safe_decode
│       ├── file_utils.py         # is_binary(), 读头, walk_files
│       └── text_utils.py         # 括号平衡, 列稳定性, 非空行, JSON 试探
├── parsers/                      # [独立] LLM 驱动的解析器系统 (Gateway 模式)
│   ├── txt_parser.py             # 网关: 格式检测 → 路由到 json/csv/sql 解析器
│   ├── json_parser.py            # 流式 JSON 解析 + Schema 发现 (采样 5K/50K)
│   ├── csv_parser.py             # Pandas CSV 解析 + NUL 字节清洗
│   └── sql_parser.py             # SQL 拆解: INSERT 语句 → Raw CSV → 委托 CsvParser
├── tests/
│   ├── conftest.py               # pytest fixtures (共 5 个，详见下方)
│   ├── fixtures/                 # 单元测试夹具文件 (6 个手工精确构造的样本)
│   │   ├── explicit_keys.json    # {users:[...], orders:[...]} 显式包装 key
│   │   ├── array_mixed.json      # 顶层数组含两种骨架
│   │   ├── users.jsonl           # 5 条 JSONL
│   │   ├── stable.csv            # 5 列稳定 CSV
│   │   ├── noisy_cols.csv        # 列数不稳定 CSV
│   │   └── two_tables.sql        # CREATE+INSERT 两张表
│   ├── test_sniff/               # 阶段0 测试
│   ├── test_parse/               # 阶段1 测试
│   └── test_extract/             # 阶段2 测试 (134 用例，全部通过)
│       ├── test_skeleton.py
│       ├── test_vocabulary.py
│       ├── test_value_profile.py
│       ├── test_topology.py
│       ├── test_pii_seed.py
│       ├── test_schema_partition.py  # 分片测试 (30 用例)
│       ├── test_schema_unit.py       # 构建测试 (53 用例，含折叠对齐 + A/B 双方案)
│       └── test_vocab_table.py       # 词汇表测试 (30 用例)
├── test_data/
│   ├── generate.py               # 测试数据生成器 (~27 文件，覆盖格式×质量×编码矩阵)
│   └── samples/                  # 生成的测试样本 (集成测试 + CLI 端到端调试用)
├── output/                       # 流水线运行结果输出 (gitignored)
├── todo_list.md                  # Schema 单元化改造任务清单 (含待完成项，见文件)
└── CLAUDE.md                     # 本文档
```

### tests/conftest.py fixtures 说明

| Fixture | 作用 | 消费者 |
|---------|------|--------|
| `samples_dir` | 返回 `test_data/samples/` 路径字符串 | test_sniff/, test_parse/ |
| `make_temp_file(tmp_path)` | 工厂函数：在隔离临时目录创建测试文件 | test_parse/ |
| `fixtures_dir` | 返回 `tests/fixtures/` 的 Path 对象 | test_schema_partition.py |
| `three_table_db(tmp_path)` | 在 tmp_path 创建含 users/orders/products 三表的 SQLite DB | test_schema_partition.py::TestSqlite |
| `single_table_db(tmp_path)` | 在 tmp_path 创建含 persons 单表的 SQLite DB（含 id_card 字段） | PII 相关测试 |

SQLite fixture 使用 `tmp_path`（pytest 每测试独立隔离目录）动态创建，避免二进制文件在测试间相互污染。

## 架构设计

### 核心数据流：三阶段流水线 (src/)

```
corpus_root/  →  [阶段0] sniff_all  →  [阶段1] grade_parse  →  [阶段2] extract_all
                     │                        │                       │
                 交叉表+分布             Grade(tier, I)      ┌── partition_file
                 real_format          tier1/tier2/tier3    │        ↓ SchemaPartition
                                       /noise/free_text    ├── build_schema_unit
                                                           │        ↓ SchemaUnit
                                                           └── build_vocab_table
                                                                    ↓ VocabTable
```

**CLI 输出文件（`output/` 目录）**：
- `extract_report.json`：全局聚合摘要（B/A/AB比值、PII 计数、partition 统计）
- `schema_units.json`：每个分片的完整 SchemaUnit 列表（骨架、字段、画像、拓扑）
- `vocab_table.json`：`vocab_table`（语义类倒排表）+ `uncertain_vocab`（待 LLM 裁决项）

#### 阶段0：内容嗅探 (sniff/)

- **目标**：解开 ~40K txt 文件的真面目
- **核心函数**: `sniff_file(path)` → `(real_format, encoding, confidence)`
- **流程**: 魔数检测 → 二进制检测 → 编码探测 → SQL 优先检查 → 多候选加权投票
- **多候选投票**: `vote_format()` 对 7 种格式并行打分
  - JSON: 括号开头 + 闭合 (0.6+0.3)
  - JSONL: 逐行 parse 成功率 > 80% (0.9)
  - CSV/TSV: 分隔符列数跨行稳定 (0.8+0.1 表头加成)
  - SQL: CREATE/INSERT 关键词命中 (0.7)
  - Log: 时间戳/级别模式命中率 > 60% (0.85)
  - Free text: 长句 + 标点 + 无强结构 (0.5)
- **置信度阈值**: 分数 < 0.5 → 归类为 `free_text`
- **关键常量**: `SNIFF_HEAD_BYTES=65536`, `SNIFF_LINES=20`, `SQLITE_MAGIC=b"SQLite format 3\x00"`
- **profiler 更新**: `profile_corpus(root, files=None)` — 可选 `files` 参数接收预收集的文件列表，与 CLI 的 `resolve_input()` 协作避免重复遍历

#### 阶段1：容错分级解析 (parse/)

- **核心桥梁**: `Grade` dataclass — 承载 tier, I(x), parsed, fmt, error, n_form, n_struct
- **路由函数**: `grade_parse(path, real_format, enc)` → Grade
- **四类输出**:
  | 类别 | tier | 说明 | 下游 |
  |------|------|------|------|
  | tier1 | 1 | 干净种子 | → 阶段2 提取 |
  | tier2 | 2 | 可恢复噪声 | → 记录 n_form/n_struct |
  | noise_sample | 特殊 | 日志弱结构 | → 注噪参照 |
  | free_text | 特殊 | 自由文本 | → PII 自举 |
  | tier3 | 3 | 不可解析 | → 错误记录 |
- **I(x) 可恢复性**: 按格式特性估算可恢复比例
  - JSON (Level 2 崩溃): `lines_consumed = total_lines × bytes_good / file_size` → `estimated_total = total_lines / (lines_consumed / good)`，I = `good / estimated_total`
  - JSON (json5 fallback, 大文件): `avg_obj_bytes = head_bytes / recovered` → `total_units = fsize / avg_obj_bytes`，I = `recovered / total_units`
  - JSONL: `good_lines / (good_lines + bad_lines)`
  - CSV: `good_rows / total_rows`
  - SQL: `complete_statements / total_statements`（完整语句定义已扩展到 CREATE TABLE/INDEX、INSERT INTO、DROP/ALTER TABLE）
  - SQLite: 几乎总是 1.0 (二进制完整性)
- **各解析器策略**:
  - JSON: 三层策略（`_ijson_count_items` 内含 `_PosTracker` 字节追踪）
    - Level 1: ijson 全量流式成功 → tier1, I=1.0
    - Level 2: ijson 中途崩溃但读出部分元素 → 字节比例估算 `estimated_total`，I≥0.99 → tier1，否则 tier2
    - Fallback: good==0 → `_json_tolerant` (json5 容错，小文件 I=1.0，大文件按 avg_obj_bytes 外推)
    - `_classify_json_error()`: 将错误分类为 `trailing_comma / single_quotes / comments / unclosed_string / incomplete / other`
  - JSONL: 逐行 `json.loads`, 统计成功/失败行数
  - CSV: 嗅探分隔符(`,;|`) → 严格列一致(tier1) → 容忍列漂移(tier2)
  - TSV: 固定 `\t` 分隔符
  - SQL: 读头 64KB → regex 分语句 → I(x) = 完整语句数/总语句数；I<1.0 时调用 `_check_unclosed_quotes()` 检测引号不配对语句，写入 Grade.note
  - SQLite: `sqlite3.connect` → 读 `sqlite_master` schema

#### 阶段2：Schema 单元化提取 (extract/)

**主入口**: `extract_all(tier1_grades)` → `(schema_units, vocab_table, global_view)`

执行顺序：**partition_file → build_schema_unit → build_vocab_table**

##### 共享数据结构 (schema_types.py — 全部 TypedDict)

> 用 TypedDict 而非 dataclass：结构以 dict 形态流动并序列化成 JSON，TypedDict
> 在保持 `obj["key"]` 运行态不变的前提下提供静态类型契约。

**SchemaPartition**（partition_file 产出 → build_schema_unit 输入）
```python
{
    'source_file'  : str,
    'format'       : str,            # json/jsonl/csv/tsv/sql/sqlite
    'partition_id' : str,            # 同文件多 schema 时区分，如 "users" / "sig_a3f2"
    'record_iter'  : Iterator[dict], # ⚠ 惰性迭代器，只可消费一次
    'noisy'        : bool,           # CSV 列不稳定时为 True，build_schema_unit 据此跳过拓扑
    'field_paths'  : set[str],       # 由 build_schema_unit 消费后回填
    'occurrence'   : dict[str, float],  # 占位符 1.0，由 build_schema_unit 回填
}
```

**SchemaUnit**（build_schema_unit 产出 → build_vocab_table 输入）
```python
{
    'id'               : str,   # 全局唯一，格式 "sch_{N:05d}"，单次 pipeline 内自增
    'source_file'      : str,
    'format'           : str,
    'partition_id'     : str,
    'skeleton'         : list[tuple],  # [(path, dtype) | (path, dtype, {multi_type,...})]，折叠模板路径
    'skeleton_count_B' : int,   # 唯一骨架签名数 B（始终来自逐记录 structure_signature）
    'skeleton_counts'  : dict,  # {sig: count} 前 50 个骨架
    'topology'         : dict,  # {折叠path: {depth, parent, siblings}}，裁剪到主干；noisy=True 时为 {}
    'fields'           : dict,  # {折叠path: FieldInfo}
    'record_count'     : int,
}
# FieldInfo 结构:
# {
#   'field_id'      : str,    # "f_{su_seq:05d}_{field_seq:02d}"
#   'key_name'      : str,    # 折叠路径末段字段名
#   'occurrence'    : float,  # 占位符 1.0（真值待 optional_field_grouping）
#   'required'      : bool,   # occurrence >= 0.9（当前恒 True）
#   'value_profile' : dict,   # ⚠ 非原值；同折叠路径所有下标值聚合后画像
#   'pii_seed'      : tuple | None,  # ("high_conf"/"needs_llm", pii_type) 或 None
# }
```

**VocabTable**（build_vocab_table 产出）
```python
dict[
    str,           # semantic_class，如 "<PERSON_NAME>" / "<PHONE_NUMBER>"
    dict[
        str,       # key_variant，如 "name" / "姓名" / "user_name"
        list[str]  # [schema_unit_id, ...]
    ]
]
```

##### partition_file：文件内 Schema 分片 (schema_partition.py)

`partition_file(grade)` → `(list[SchemaPartition], PartitionStats)`

| 格式 | 分片策略 | method 字段 |
|------|----------|-------------|
| JSON (显式包装 key) | `{key→list[dict]}` 顶层检测，每个 key 一个 partition | `"explicit_key"` |
| JSON (顶层数组) | `structure_signature` 骨架聚类，每种骨架一个 partition | `"skeleton_cluster"` |
| JSONL | 骨架聚类（逐行） | `"skeleton_cluster"` |
| CSV/TSV | 整文件单 partition；列数标准差 > 0.5 → `noisy=True` | `"single"` |
| SQL 文本 | 按 CREATE/INSERT 表名分片 | `"table_name"` |
| SQLite | `sqlite_master` 读表名，每表 `SELECT * LIMIT N` | `"table_name"` |

- SQLite 使用只读 URI：`sqlite3.connect("file:path?mode=ro", uri=True)`
- SQL/CSV 在 schema_partition.py 内部流式扫描分桶，不落地中间文件

##### build_schema_unit：SchemaUnit 构建 (schema_unit.py)

`build_schema_unit(partition, mode="template")` → `SchemaUnit`

- 消费 `record_iter`（一次性，`islice(iter, SAMPLE_PER_FILE)`）
- 分配全局唯一 ID：`_UNIT_COUNTER = itertools.count(1)`，`reset_unit_counter()` 仅供测试使用
- **单遍折叠遍历**（`_walk_fold`）：list 下标折叠成 `[]`，五类信息共用同一套折叠模板路径；
  同一遍同时产出 `sig_counter`（逐记录签名 → 保 B/AB_ratio）、`template_values`（元素级值聚合）、
  `dtype_seen`（类型计数，null 不计入）
- **两套字段主干方案**（`mode`）：`"template"`（B，默认）裁剪到 most_common 签名主干；
  `"fold"`（A）取全部折叠路径并集（保留数组元素级异构，dtype 取最高频 + 多型标记）。
  CLI `--field-mode {template,fold}` 切换
- occurrence 暂为占位符 1.0（真值待 `optional_field_grouping` 落地，见 todo_list.md）
- 骨架签名包含裸类型标记（`<int>` 等，非合法 JSON），B 方案解析前用 `re.sub` 加引号归一化
- 回填 `partition['field_paths']` 和 `partition['occurrence']`（供后续统计）

##### build_vocab_table：全局词汇表 (vocab_table.py)

`build_vocab_table(schema_units)` → `(VocabTable, uncertain_list)`

三证据联合同义聚类（Union-Find）：

| 证据 | 内容 | 强度 |
|------|------|------|
| C — PII 类型一致 | `pii_seed` 同类型 → 合并入同一语义类 | 强 |
| B — value 画像相似 | `profile_similarity() ≥ 0.7`（同 type + 长度比 min/max≥0.4） | 强 |
| A — 字符串相似 | 归一化编辑距离（去下划线、小写）< 0.45 → 冲突检测 | 弱（校正用） |

- B+C 先聚类，A 发现冲突后进入 `uncertain_list` 待 LLM 裁决
- 语义类命名：PII 字段用 `<PII_TYPE>`（大写），普通字段用最高频 key_name
- 倒排结构：`VocabTable[semantic_class][key_variant] = [schema_unit_id, ...]`

##### 兼容函数 (extractor.py)

- `extract_five_infos(tier1_grades, mode="template")`：兼容接口，现为 `extract_all()` 的薄包装——跑新管线后把各 SchemaUnit 拍平为旧的全局扁平五类信息 dict（无溯源，不建议新代码使用）
- `extract_all(tier1_grades, mode="template")`：新主入口，调用 partition_file → build_schema_unit → build_vocab_table 三段管线，返回 `(schema_units, vocab_table, global_view)`
- `global_view` 字段：`shape_templates_B`, `naming_templates_A`, `AB_ratio`, `pii_seeds_count`, `total_records_sampled`, `top_skeletons`, `partition_stats`, `uncertain_vocab`

### 独立解析器系统 (parsers/) — Gateway 模式

`parsers/` 是一个**独立参考代码目录**，用于摸索和完善 `src/parse/` 的解析逻辑，同时承担 LLM 驱动的 ETL 转换任务（将原始文件转换为标准化 CSV）。依赖外部框架: `core.base.BaseParser`, `core.llm_engine.LLMMappingEngine`, `core.format_detector.FormatDetector`（不在本仓库中）。

每个 parser 均支持两种运行模式：
- **`process()`**: 完整 ETL 流程（Schema 发现 → LLM 映射 → 转换输出 CSV）
- **`detect(source_type=_NO_ARG)`**: 仅做字段 Schema 探查，将结构化 JSON 行追加写入 `config['paths']['Detect_path']`，不触发 LLM 映射

探查结果统一格式：`{"file_name": ..., "source_type": ..., "field_name": [...], "sample_values": [...]}`

#### 入口: TxtParser (网关)

`parsers/txt_parser.py` — `TxtParser.process()` / `TxtParser.detect()`:

1. 使用 `FormatDetector` 探测文件真实格式 → `(fmt_type, meta)`
2. 动态路由:
   - `"json"` → 实例化 `JsonParser`, 委托 `process()` 或 `detect(source_type="txt")`
   - `"csv"` → 实例化 `CsvParser`, 传入探测到的分隔符 `meta.get("delimiter")`, 委托 `process()` 或 `detect(source_type="txt")`
   - `"sql"` → 实例化 `SqlParser`, 委托 `process()` 或 `detect(source_type="txt")`

#### JsonParser (JSON 解析)

`parsers/json_parser.py` — `JsonParser.process()`:

1. **Schema 发现** (`_discovery_phase`): 使用 `stream_read_json()` 流式读取，对前 5000 条 record 用 `flatten_json()` 展开嵌套 key，统计每个 key 的出现次数和示例值；调用 `_save_discovery_report()` 生成按频率降序排列的人可读 txt 报告
2. **LLM 映射**: 调用 `LLMMappingEngine.generate_mapping()` 生成字段映射规则，保存为 `mapping_rules.json`
3. **ETL 转换**: 重新全量读文件，按映射规则逐行转换，输出标准化 CSV（目标字段固定为 17 列 PII 字段）

**`detect()` / `_schema_detect()`**: 采样前 50000 条，收集前 10 条原始 sample rows，返回结构化 dict 后追加写入 `detect_path`。

#### CsvParser (CSV 解析)

`parsers/csv_parser.py` — `CsvParser.process()`:

1. **安全读取** (`_read_csv_safe(only_header, nrows)`): 使用 Pandas `read_csv`，`on_bad_lines='skip'`；`only_header=True` 时 `nrows=0` 快速读表头
2. **NUL 字节清洗** (`_read_cleaned_data`): 当 CSV 解析器报 NUL 字节错误时，将文件读到内存 `replace('\0', '')` 后重新解析
3. **LLM 语义映射**: 调用 `LLMMappingEngine.generate_csv_header_mapping()` 做字段映射
4. **ETL 转换** (`_transform_and_save`): 支持单列直接映射 (int index) 和多列组合映射 (list of indices)；过滤空值/零值/null 后填充空格

**`detect()` / `_save_discovery_report()`**: `_save_discovery_report()` 读前 10 行返回结构化 dict（`file_name, source_type, field_name, sample_values`），`detect()` 再将其追加写入 `detect_path`。

#### SqlParser (SQL 拆解)

`parsers/sql_parser.py` — `SqlParser.process()`:

1. **SQL 拆解** (阶段1): 逐行流式读 SQL 文件，识别三类行：
   - `CREATE TABLE` → 调用 `_parse_create_table(f_iter, table_name)` 消耗后续列定义行，过滤 SQL 保留字行（`PRIMARY/KEY/UNIQUE/CONSTRAINT/FOREIGN/INDEX/FULLTEXT/CHECK/PARTITION/SPATIAL`），缓存列名到 `self.schemas`
   - `INSERT INTO` → `insert_header_pattern` 提取表名和可选列名，调用 `_get_writer_info()` 获取/创建 writer，同行内有 VALUES 则立即调用 `_write_rows()`
   - VALUES 跨行续行 → 行以 `(` 开头则继续写入当前 writer；`;` 结束符重置 `current_table`
2. **Writer 管理** (`_get_writer_info`): 带断点续传逻辑——句柄已关闭时以追加模式重新打开；文件路径为 `raw_extracted_sql/{project_name}/{table_name}.csv`；仅在新建或空文件时写表头
3. **Record 解析** (`_parse_values_part`): 用 `csv.reader(quotechar="'")` 解析 SQL VALUES 中的元组；`csv.field_size_limit(2147483647)` 防止超长字符串报错
4. **委托 CsvParser** (阶段2): 遍历 `self.generated_files`，逐个交给 `CsvParser.process()` 做标准化清洗；`finally` 块中清理所有文件句柄

**`detect(source_type)`**: 同样拆解 SQL → Raw CSVs，但对每个 Raw CSV 调用 `CsvParser._save_discovery_report()`，在结果 dict 中追加 `table_name` 字段后写入 `detect_path`。

### 日志系统

`src/utils/logger.py` — Python 标准库 `logging` 封装:

```python
def setup_logger(name="pii_detect", level=INFO, stream=True, file=None) -> Logger
```

- 创建 logger, 支持同时输出到 `sys.stdout` 和可选的 UTF-8 日志文件
- 格式: `"HH:MM:SS [LEVEL] message"`
- 避免重复添加 handler (幂等)
- 模块级 `logger` 单例: 子模块可直接 `from src.utils.logger import logger` 使用
- `get_logger(name)`: 创建 `pii_detect.{name}` 命名子 logger

### CLI 输入容错机制

`main.py:resolve_input(root, log)` — 宽限输入解析:

1. **单文件**: 直接返回 `[path]`
2. **目录**: 递归遍历所有文件, 有则返回
3. **空目录回溯**: 向上遍历父目录, 重复尝试直到找到文件或到达文件系统根
4. **全程无文件**: 返回空列表, 记录 error 日志

所有四个 CLI 命令 (`sniff`, `parse`, `extract`, `pipeline`) 均在启动时调用 `resolve_input()`, 路径为空时终止。

## PII 检测关键词

中英双语覆盖 (`src/constants.py`):

```
name, phone, email, mail, id_card, idcard, ssn, social.security,
address, addr, passport, birth, birthday, gender, sex,
mobile, tel, telephone, contact,
身份证, 姓名, 电话, 邮箱, 地址, 密码, 手机,
生日, 性别, 年龄, 籍贯, 民族, 住址,
card_no, cardno, bank, account, credit,
ip_addr, mac, imei, uuid, token, secret, password, passwd, pwd
```

## 二进制检测逻辑

`is_binary(raw)` (`src/utils/file_utils.py`):

- NULL 字节 > 1/256 比例 → 强二进制信号
- 控制字符 (0x00-0x08, 0x0B-0x0C, 0x0E-0x1F, 0x7F) 占比 > 30% → 二进制
- **注意**：UTF-8 中文字节 (>=0x80) 不计入控制字符，避免中文文本误判为二进制

## 命令参考

```bash
# 生成测试数据
uv run python test_data/generate.py [--output test_data/samples] [--seed 42]

# 独立子命令 (支持文件/目录作为输入)
uv run python main.py sniff    <corpus_root> -o output -f output/sniff.log
uv run python main.py parse    <corpus_root> -o output -f output/parse.log
uv run python main.py extract  <corpus_root> -o output -f output/extract.log
uv run python main.py pipeline <corpus_root> -o output -f output/pipeline.log

# 测试
uv run python -m pytest tests/ -v
```

CLI 标志:
- `root`: 语料库根目录或单个文件路径
- `-o, --output-dir`: JSON 报告输出目录 (默认: `output`)
- `-f, --output-file`: 日志文件路径 (默认: `<output-dir>/<command>.log`)
- `--field-mode {template,fold}`（仅 `extract`/`pipeline`）: 字段主干方案，`template`(B，默认，裁剪到主导签名) / `fold`(A，全路径并集，保留数组元素级异构)

`extract` / `pipeline` 命令产出三个文件：
- `extract_report.json` — 全局摘要统计
- `schema_units.json` — SchemaUnit 列表（每个分片的五类信息）
- `vocab_table.json` — 语义类倒排表 + uncertain_vocab

## 已知问题与边界

1. **日志文件含逗号时间戳**时 (如 `[2024-01-01 00:00:00,123]`), CSV 投票可能压制日志投票 (分数 0.9 vs 0.85)，导致 `.log` 文件被误判为 CSV
2. **短 GBK 文件**: chardet 可能将 GBK 误判为 `cp1250`/`windows-1250`（代码页邻接），需在真实数据上验证
3. **非 .sql 扩展名的 SQL 文件**: 如果 INSERT 语句中逗号分隔值与 CSV 模式竞争，SQL (0.7) 可能输给 CSV (0.9)，需要观察真实分布后再调权重
4. **随机字节文件** (`os.urandom`) 可能因巧合不触发二进制检测被误判为 TSV；真实二进制文件 (exe/png) 含 NULL 字节不会误判
5. **`parsers/` 依赖外框架**: `core.base`, `core.llm_engine`, `core.format_detector` 不在当前仓库中, 直接 import 会失败; 该目录为参考实现，与 `src/` 流水线无调用关系
6. **`extract_five_infos()` 是 `extract_all()` 的薄包装**: 前者把各 SchemaUnit 拍平为旧的全局扁平 dict（折叠路径、跨分片合并、无溯源），后者是新主入口（Schema 单元化、带溯源）；两者输出格式不同，不可混用
7. **骨架签名非 JSON**: `structure_signature()` 输出含裸类型标记（`<int>` 等），不是合法 JSON；`schema_unit.py` 的 B 方案在解析前用 `re.sub` 做归一化，`skeleton.py` 本身不改动
8. **fields 字段主干随 mode 变化**: B 方案（默认）裁剪到主导签名，会丢数组元素级异构与少数派字段；A 方案（`--field-mode fold`）取全路径并集保留之。两者 occurrence 均为占位符 1.0
