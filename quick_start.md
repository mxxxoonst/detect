# PII Detect — 快速上手指南

## 环境准备

- Python 3.12+
- uv 包管理器: `pip install uv`

```bash
cd D:\PII_detect
uv sync              # 安装依赖 (chardet, ijson, json5, pytest)
```

## 生成测试数据

真实数据在云服务器上暂不可下载，开发阶段使用生成的测试文件：

```bash
uv run python test_data/generate.py
```

在 `test_data/samples/` 下生成 27 个覆盖全场景的测试文件：

| 场景 | 文件 | 用途 |
|------|------|------|
| 干净 JSON | `clean_users.json`, `clean_users.jsonl`, `clean_nested.json` | tier1 种子 |
| 噪声 JSON | `noisy_trailing_comma.json`, `noisy_incomplete.json`, `noisy_bom.json` | tier2 测试 |
| GBK JSON | `gbk_users.json` | 编码兼容 |
| 干净 CSV | `clean_users.csv`, `clean_users_pipe.csv` | tier1, 管道分隔符 |
| TSV | `clean_users.tsv` | tab 分隔符 |
| GBK CSV | `gbk_users.csv` | 编码兼容 |
| 噪声 CSV | `noisy_column_drift.csv` | 列数漂移 |
| SQL | `clean_schema.sql`, `noisy_truncated.sql` | CREATE/INSERT 提取 |
| SQLite | `clean_users.db`, `corrupted.db` | 二进制 db vs 伪装 db |
| 日志 | `app_server.log` | 时间戳/级别提取 |
| 自由文本 | `free_text_zh.txt` | PII 句中检测 |
| 扩展名不符 | `actually_json.txt`, `actually_csv.txt` 等 | 内容优先判据 |
| 空文件 | `empty.txt`, `empty.csv`, `empty.json` | 边界 |
| 二进制 | `random.bin` | 二进制检测 |

## 运行流水线

### 完整三阶段流水线

```bash
uv run python main.py pipeline test_data/samples -o output
```

产出 `output/pipeline_report.json`，包含：
- **phase0**: 格式交叉表 `{".ext|format": count}`、真实格式分布、低置信样本列表
- **phase1**: tier1/tier2/noise/free_text 统计
- **phase2**: 五类信息 (skeletons, vocabulary, value_profiles, topology, pii_seeds)

同时生成日志文件 `output/pipeline.log`。

### 独立运行各阶段

```bash
# 仅阶段0: 内容嗅探 → 交叉表
uv run python main.py sniff test_data/samples -o output

# 仅阶段1: 分级解析 → 分拣报告
uv run python main.py parse test_data/samples -o output

# 仅阶段2: 信息提取 → 五类信息 (自动串联阶段0+1)
uv run python main.py extract test_data/samples -o output
```

### CLI 参数说明

| 参数 | 说明 |
|------|------|
| `root` | 语料库根目录或单个文件路径。支持空目录自动向上回溯父目录查找文件 |
| `-o, --output-dir` | JSON 报告输出目录 (默认: `output`) |
| `-f, --output-file` | 日志文件路径 (默认: `<output-dir>/<command>.log`) |

### 容错输入机制 (`resolve_input`)

`root` 参数支持宽限解析：
1. 指向文件 → 直接处理该文件
2. 指向目录 → 递归遍历所有文件
3. 目录为空 → 向上回溯父目录，逐层查找直到找到文件或到达根目录
4. 全程无文件 → 跳过并记录错误日志

## 核心代码用途

### 阶段0 — 内容嗅探

**`src/sniff/sniffer.py`** — `sniff_file(path)`：
单文件嗅探入口。流程：魔数→二进制检测→chardet探编码→.sql优先检查→多候选加权投票。返回 `(real_format, encoding, confidence)`。

**`src/sniff/voting.py`** — `vote_format(lines, text)`：
7 候选并行打分器。对 JSON(括号闭合)、JSONL(逐行 parse)、CSV/TSV(列稳定性)、SQL(关键词)、Log(时间戳模式)、Free text(长句标点) 分别计分。

**`src/sniff/profiler.py`** — `profile_corpus(root, files=None)`：
批量目录嗅探。`files` 参数可选，传入时直接使用预收集的文件列表（与 CLI 的 `resolve_input()` 协作避免重复遍历）。产出 `[扩展名×真实格式]` 交叉表、真实格式分布、低置信(<0.7)抽检清单。

### 阶段1 — 容错分级解析

**`src/parse/grade.py`** — `Grade` dataclass + `grade_parse()`：
`Grade` 是阶段间传递的核心数据结构。`grade_parse(path, real_format, enc)` 按真实范式路由到对应解析器。

**`src/parse/json_parser.py`** — `parse_json()`, `parse_jsonl()`：
JSON 先用 ijson 流式 strict 解析，失败回退 json5 tolerant（容忍尾逗号/单引号/注释）。JSONL 逐行 parse 统计成功率。

**`src/parse/csv_parser.py`** — `parse_csv()`, `parse_tsv()`：
CSV 自动嗅探分隔符(`,;|`)，选择列数最稳定的。Strict 要求全行列数一致→tier1，漂移→tier2(记录 n_struct)。

**`src/parse/sql_parser.py`** — `parse_sql_text()`：
读头 64KB，按 `;` 分语句，regex 匹配含 CREATE/INSERT 的语句。I(x) = 完整语句数/总语句数。

**`src/parse/sqlite_parser.py`** — `parse_sqlite()`：
`sqlite3.connect(file:path?mode=ro, uri=True)`，读 `sqlite_master` 表获取 schema。有表可读→tier1，无表→tier3。

### 阶段2 — 五类信息提取

**`src/extract/extractor.py`** — `extract_five_infos(tier1_grades)`：
阶段2 编排器。遍历 tier1 种子→抽样 record→分别调用五个提取器→汇总产出。

**`src/extract/skeleton.py`** — `structure_signature(record)`：
信息一：结构骨架。将 record 的每个标量 value 替换为类型标记 (`<int>`, `<str>` 等)，key 名保留，生成可计数的签名串。

**`src/extract/vocabulary.py`** — `build_vocabulary(records)`：
信息二：字段名词表。`{field_name: {paths}}` 映射，统计 A = 唯一字段名数。

**`src/extract/value_profile.py`** — `profile_value(value)`, `aggregate_profiles(profiles)`：
信息三：value 画像。计算长度分布、字符类分布 (digit/alpha/CJK/punct/space %)、模式模板 (如 `D{11}` = 11位纯数字，`L{4}D{2}SL{7}SL{3}` = email 模式)。仅存统计摘要，不存原值。

**`src/extract/topology.py`** — `build_topology(records)`：
信息四：拓扑。每个字段路径的 depth, parent 路径, siblings 列表。

**`src/extract/pii_seed.py`** — `detect_pii_seeds(records, vocab)`：
信息五：PII 种子。两类检测——key 名匹配 PII 关键词(高置信)→推断 PII 类型；字段值长文本(free text)→标记 `needs_llm`。

### 工具模块

**`src/utils/encoding.py`**：chardet 编码探测 + `errors='replace'` 安全解码。

**`src/utils/file_utils.py`**：文件头读取、二进制检测（NULL字节+控制字符比例）、文件遍历、SQLite 魔数检测。

**`src/utils/text_utils.py`**：括号平衡、非空行提取、列稳定性计算、句式检测、JSON parse 试探。

**`src/utils/logger.py`**：`setup_logger(name, level, stream, file)` 配置日志。支持 stdout + 文件双输出，格式 `"HH:MM:SS [LEVEL] message"`。模块级 `logger` 单例可直接导入。

### 独立解析器系统 (parsers/)

`parsers/` 是一个独立于核心流水线的 LLM 驱动解析器系统，使用 Gateway 模式。依赖 `core.base.BaseParser`, `core.llm_engine.LLMMappingEngine`, `core.format_detector.FormatDetector`（外部框架，不在本仓库中）。

**`parsers/txt_parser.py`** — Gateway 入口：`FormatDetector` 检测格式 → 路由到 JsonParser / CsvParser / SqlParser。

**`parsers/json_parser.py`** — 流式 JSON: `stream_read_json()` + `flatten_json()` 展开嵌套 key, 采样 5K/50K 条做 Schema 发现, LLM 映射字段后用 `csv.DictWriter` 输出标准化 CSV。

**`parsers/csv_parser.py`** — Pandas CSV: `_read_csv_safe()` 安全读（含 NUL 字节内存清洗回退）, `_transform_and_save()` 支持单列直接映射和多列组合映射。

**`parsers/sql_parser.py`** — SQL 拆解: 流式读 SQL → regex 识别 CREATE TABLE(提取列名) 和 INSERT INTO(解析 VALUES 元组) → 按表名分写原始 CSV → 委托 CsvParser 标准化清洗。

### 常量配置

**`src/constants.py`**：
所有可调参数集中管理：
- `SNIFF_HEAD_BYTES=65536` — 嗅探读头大小
- `ACCEPT_THRESHOLD=0.5` — 投票分数阈值
- `LOW_CONF_THRESHOLD=0.7` — 低置信抽检线
- `SAMPLE_PER_FILE=1000` — 阶段2 抽样数
- `PII_KEY_PATTERN` — 中英 PII 关键词 regex
- `SQL_KEYWORD_PATTERN` — SQL 语句识别 regex
- `LOG_PATTERN` — 日志行识别 regex

## 运行测试

```bash
uv run python -m pytest tests/ -v
```

49 个测试覆盖三个阶段的核心逻辑：
- **test_sniff/**：嗅探准确率 (14)，投票规则 (7)
- **test_parse/**：分级路由 (8)
- **test_extract/**：骨架签名 (4)，词汇表 (3)，value 画像 (7)，拓扑 (2)，PII 种子 (5)

## 添加新格式支持

1. 在 `src/sniff/voting.py` 的 `vote_format()` 中添加新格式的投票逻辑
2. 在 `src/sniff/sniffer.py` 的 `sniff_file()` 中添加对应的优先检查（如有）
3. 在 `src/parse/` 下创建新 parser 文件，实现 strict + tolerant 两个等级
4. 在 `src/parse/grade.py` 的 `grade_parse()` 中添加路由分支
5. 在 `src/extract/extractor.py` 的 `_iterate_records()` 中添加 record 迭代逻辑
6. 在 `test_data/generate.py` 中添加对应测试文件生成
7. 在 `tests/` 下添加对应测试用例

## 添加新 PII 类型

1. 在 `src/constants.py` 的 `PII_KEY_PATTERN` 中添加关键词
2. 在 `src/extract/pii_seed.py` 的 `PII_TYPE_RULES` 中添加 `(pattern, pii_type)` 映射
