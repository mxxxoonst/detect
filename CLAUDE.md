# CLAUDE.md — PII Detect 项目上下文

## 项目概述

本项目是一个**多源异构文件的数据预处理与信息抽取流水线**，核心功能为：从混合格式文件（JSON/CSV/TSV/SQL/SQLite/TXT/日志）中嗅探真实格式、容错解析并提取五类结构信息及 PII 种子。

- **语言**: Python 3.12+
- **包管理**: uv (pyproject.toml)
- **核心依赖**: chardet, ijson, json5, pytest
- **数据规模**: ~45,473 文件, GB 量级（真实数据在云服务器上，当前不可下载）
- **工作目录**: `D:\PII_detect`

## 关键约束

- **流式/抽样**：禁止整文件 load，一律流式读或只读头部
- **编码混合**：GBK/UTF-8 混存，chardet 探编码，`errors='replace'` 解码
- **扩展名不可信**：格式判据是内容嗅探而非文件扩展名，扩展名仅作弱先验
- **禁止持久化 PII 原值**：value 画像只存统计摘要（长度分布、字符类分布、模式模板），不存原始值
- **SQLite 独立路径**：`.db` 走 sqlite3 二进制路径，不可文本解析

## 项目结构

```
D:\PII_detect\
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
│   ├── extract/                  # [阶段2] 五类信息提取 (仅 tier1)
│   │   ├── __init__.py
│   │   ├── extractor.py          # extract_five_infos() 编排器 + record 迭代器
│   │   ├── skeleton.py           # 信息一: 结构骨架 (抹 value 留 key 树)
│   │   ├── vocabulary.py         # 信息二: 字段名词表
│   │   ├── value_profile.py      # 信息三: value 画像 (统计摘要，不存原值)
│   │   ├── topology.py           # 信息四: 拓扑 (depth, parent, siblings)
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
│   ├── conftest.py               # pytest fixtures (samples_dir, make_temp_file)
│   ├── test_sniff/               # 阶段0 测试 (14 用例)
│   ├── test_parse/               # 阶段1 测试 (8 用例)
│   └── test_extract/             # 阶段2 测试 (27 用例)
├── test_data/
│   ├── generate.py               # 测试数据生成器 (~27 文件)
│   └── samples/                  # 生成的测试样本
├── output/                       # 流水线运行结果输出 (gitignored)
├── quick_start.md                # 快速上手指南
└── CLAUDE.md                     # 本文档
```

## 架构设计

### 核心数据流：三阶段流水线 (src/)

```
corpus_root/  →  [阶段0] sniff_all  →  [阶段1] grade_parse  →  [阶段2] extract
                     │                        │                       │
                 交叉表+分布             Grade(tier, I)           五类信息:
                 real_format          tier1/tier2/tier3        skeletons(B)
                                       /noise/free_text         vocabulary(A)
                                                                value_profiles
                                                                topology
                                                                pii_seeds
```

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
- **I(x) 可恢复性**: 方法A (默认) — 按文件行数/大小线性估算期望结构单元数
  - JSON: `recovered_units / lines_div_2`
  - CSV: `good_rows / total_rows`
  - SQL: `complete_statements / total_statements`
  - SQLite: 几乎总是 1.0 (二进制完整性)
- **各解析器策略**:
  - JSON: ijson 流式 strict → json5 tolerant (尾逗号/单引号/注释)
  - JSONL: 逐行 `json.loads`, 统计成功/失败行数
  - CSV: 嗅探分隔符(`,;|`) → 严格列一致(tier1) → 容忍列漂移(tier2)
  - TSV: 固定 `\t` 分隔符
  - SQL: 读头 64KB → regex 分语句 → 统计含 CREATE/INSERT 的语句比例
  - SQLite: `sqlite3.connect` → 读 `sqlite_master` schema

#### 阶段2：五类信息提取 (extract/)

- **前置**: 仅处理 tier1 种子
- **抽样**: 每文件最多 `SAMPLE_PER_FILE=1000` 条 record (截断, 非随机)
- **五项产出**:

  1. **结构骨架 (skeletons) — B**
     - `structure_signature(record)`：抹 value，留 key 树结构
     - 规则: dict key 保留, list 取首元素递归, 标量替换为 `<int>/<str>/<float>/<bool>/<null>`
     - `B = 唯一形状模板数`

  2. **字段词汇表 (vocabulary) — A**
     - `build_vocabulary(records)`: `{field_name: {path1, path2, ...}}`
     - `A = 唯一字段名数`（命名模板）

  3. **Value 画像 (value_profiles)**
     - `profile_value(value)`: 计算单个值的统计特征，**不存原值**
     - 特征: 长度、字符类分布(digit/alpha/CJK/punct/space %)、模式模板(如 `D{11}` = 11位数字)
     - `aggregate_profiles(profiles)`: 同字段多值聚合 (min/max/mean, top_patterns)

  4. **拓扑 (topology)**
     - `build_topology(records)`: 每个字段路径的 depth, parent, siblings

  5. **PII 种子 (pii_seeds)**
     - 高置信: key 名命中 PII 关键词 (name, phone, email, id_card, 身份证, 电话 等)
     - 待 LLM: 字段值平均长度 > 100 chars 且含自然语言标点 (free text 字段)
     - PII 类型推断: person_name, phone_number, email, id_card, address, credential, financial, device_id, demographic

- **A/B 比值**: `naming_templates_A / shape_templates_B` — 命名多样性 vs 结构多样性

### 独立解析器系统 (parsers/) — Gateway 模式

`parsers/` 是一个**独立于核心流水线**的解析器系统，使用 Gateway 模式实现对未知格式 TXT 文件的自动路由和处理。依赖外部框架: `core.base.BaseParser`, `core.llm_engine.LLMMappingEngine`, `core.format_detector.FormatDetector`。

#### 入口: TxtParser (网关)

`parsers/txt_parser.py` — `TxtParser.process()`:

1. 使用 `FormatDetector` 探测文件真实格式 → `(fmt_type, meta)`
2. 动态路由:
   - `"json"` → 实例化 `JsonParser`, 委托 `process()`
   - `"csv"` → 实例化 `CsvParser`, 传入探测到的分隔符 `meta.get("delimiter")`, 委托 `process()`
   - `"sql"` → 实例化 `SqlParser`, 委托 `process()`
3. `detect()` 方法: Schema 字段探查模式, 同样路由但调用各 parser 的 `detect()` 方法

#### JsonParser (JSON 解析)

`parsers/json_parser.py` — `JsonParser.process()`:

1. **Schema 发现** (`_discovery_phase`): 使用 `stream_read_json()` 流式读取, 对前 5000 条 record 用 `flatten_json()` 展开嵌套 key, 统计每个 key 的出现次数和示例值
2. **LLM 映射**: 调用 `LLMMappingEngine.generate_mapping()` 生成字段映射规则
3. **ETL 转换**: 重新全量读文件, 按映射规则逐行转换, 输出标准化 CSV
4. **Schema 探查** (`detect` / `_schema_detect`): 采样前 50000 条, 输出字段名列表 + 前 10 条样本值到探测文件

#### CsvParser (CSV 解析)

`parsers/csv_parser.py` — `CsvParser.process()`:

1. **安全读取** (`_read_csv_safe`): 使用 Pandas `read_csv`, `on_bad_lines='skip'`
2. **NUL 字节清洗** (`_read_cleaned_data`): 当 CSV 解析器报 NUL 字节错误时, 将文件读到内存 `replace('\0', '')` 后重新解析
3. **LLM 语义映射**: 调用 `LLMMappingEngine.generate_csv_header_mapping()` 做字段映射
4. **ETL 转换** (`_transform_and_save`): 支持单列直接映射 (int index) 和多列组合映射 (list of indices)
5. **Schema 探查** (`detect` / `_save_discovery_report`): 读前 10 行, 输出字段名 + 样本值到探测文件

#### SqlParser (SQL 拆解)

`parsers/sql_parser.py` — `SqlParser.process()`:

1. **SQL 拆解** (阶段1): 流式读 SQL 文件, 用 regex 识别 `CREATE TABLE` (提取列名) 和 `INSERT INTO` (解析 VALUES 元组), 按表名分写入 `raw_extracted_sql/{project}/{table}.csv`
2. **Record 解析** (`_parse_values_part`): 用 `csv.reader` 解析 SQL VALUES 中的元组, 处理嵌套括号/引号
3. **委托 CsvParser** (阶段2): 遍历生成的原始 CSV, 逐个交给 `CsvParser.process()` 做标准化清洗
4. **Schema 探查** (`detect`): 同样拆解 SQL → Raw CSV, 但调用 `CsvParser._save_discovery_report()` 输出字段发现报告

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
uv run python main.py sniff   <corpus_root> -o output -f output/sniff.log
uv run python main.py parse   <corpus_root> -o output -f output/parse.log
uv run python main.py extract <corpus_root> -o output -f output/extract.log
uv run python main.py pipeline <corpus_root> -o output -f output/pipeline.log

# 测试
uv run python -m pytest tests/ -v
```

CLI 标志:
- `root`: 语料库根目录或单个文件路径
- `-o, --output-dir`: JSON 报告输出目录 (默认: `output`)
- `-f, --output-file`: 日志文件路径 (默认: `<output-dir>/<command>.log`)

## 已知问题与边界

1. **日志文件含逗号时间戳**时 (如 `[2024-01-01 00:00:00,123]`), CSV 投票可能压制日志投票 (分数 0.9 vs 0.85)，导致 `.log` 文件被误判为 CSV
2. **短 GBK 文件**: chardet 可能将 GBK 误判为 `cp1250`/`windows-1250`（代码页邻接），需在真实数据上验证
3. **非 .sql 扩展名的 SQL 文件**: 如果 INSERT 语句中逗号分隔值与 CSV 模式竞争，SQL (0.7) 可能输给 CSV (0.9)，需要观察真实分布后再调权重
4. **随机字节文件** (`os.urandom`) 可能因巧合不触发二进制检测被误判为 TSV；真实二进制文件 (exe/png) 含 NULL 字节不会误判
5. **`parsers/` 依赖外框架**: `core.base`, `core.llm_engine`, `core.format_detector` 不在当前仓库中, 直接 import 会失败; 该目录为独立系统, 与 `src/` 流水线无调用关系
