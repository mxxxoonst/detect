# PII Detect — 快速上手指南

> 多源异构文件的数据预处理与信息抽取流水线：从混合格式文件（JSON/CSV/TSV/SQL/SQLite/TXT/日志）中
> 嗅探真实格式、容错解析并提取五类结构信息及 PII 种子。
> 本文聚焦**各模块/阶段的作用与输入/输出**，以及**命令行运行方式**；实现细节见 `CLAUDE.md`。

## 环境准备

- Python 3.12+
- uv 包管理器: `pip install uv`

```bash
cd F:\zlf\paper_dataPipeline\detect
uv sync              # 安装依赖 (chardet, ijson, json5, pytest)
```

## 生成测试数据

真实数据在云服务器上暂不可下载，开发阶段使用生成的测试文件：

```bash
uv run python test_data/generate.py
```

- **输入**：无（可选 `--output`、`--seed`）
- **输出**：`test_data/samples/` 下约 27 个样本，覆盖 **格式**（JSON/JSONL/CSV/TSV/SQL/SQLite/日志/自由文本）×
  **质量**（干净/噪声/截断/空）× **编码**（UTF-8/GBK/BOM）矩阵，含扩展名不符与二进制样本。

---

## 流水线总览

三阶段逐级转化，前一阶段的输出是后一阶段的输入：

```
corpus_root → [阶段0 嗅探] → [阶段1 分级解析] → [阶段2 信息提取]
```

| 阶段 | 作用 | 输入 | 输出 |
|------|------|------|------|
| **阶段0 嗅探** (`sniff/`) | 解开扩展名不可信的文件真实格式 | 文件路径 / 语料库目录 | 逐文件 `(real_format, encoding, confidence)`；目录级**交叉表 + 格式分布 + 低置信清单** |
| **阶段1 分级解析** (`parse/`) | 容错解析并按可恢复性分拣 | 文件 + 阶段0 的 `(fmt, enc)` | 逐文件 `Grade`（tier、可恢复性 I、解析摘要）；分拣为 tier1/tier2/tier3/noise/free_text |
| **阶段2 信息提取** (`extract/`) | 对干净种子做 Schema 单元化抽取 | **tier1** 的 `Grade` 列表 | `schema_units`（每分片五类信息）、`vocab_table`（语义类倒排表）、`global_view`（全局摘要） |

> 五类信息 = ①结构骨架 ②字段名词表 ③value 画像（仅统计摘要，不存原值）④拓扑 ⑤PII 种子。

---

## 命令行运行

### 命令一览

| 命令 | 作用 | 输入 | 产出文件（均在 `-o` 目录） |
|------|------|------|------|
| `sniff` | 仅阶段0 | `root` | `sniff_report.json` |
| `parse` | 仅阶段1 | `root` | `parse_report.json` |
| `extract` | 阶段2（自动串联阶段0+1 以筛出 tier1） | `root` | `extract_report.json`、`schema_units.json`、`vocab_table.json` |
| `pipeline` | 三阶段全流程 | `root` | `pipeline_report.json`、`schema_units.json`、`vocab_table.json` |

每个命令同时写日志文件 `<output-dir>/<command>.log`（默认）。

```bash
# 全流水线
uv run python main.py pipeline test_data/samples -o output

# 独立各阶段
uv run python main.py sniff   test_data/samples -o output
uv run python main.py parse   test_data/samples -o output
uv run python main.py extract test_data/samples -o output

# 排查单文件 / 看 DEBUG 细节
uv run python main.py pipeline test_data/samples -o output -v
```

### 参数说明

| 参数 | 适用命令 | 说明 |
|------|----------|------|
| `root` | 全部 | 语料库根目录或单个文件路径；空目录会自动向上回溯父目录查找文件 |
| `-o, --output-dir` | 全部 | JSON 报告与日志的输出目录（默认 `output`） |
| `-f, --output-file` | 全部 | 日志文件路径（默认 `<output-dir>/<command>.log`） |
| `-v, --verbose` | 全部 | 开启 DEBUG 级日志（逐文件吞错、投票得分、分片方法等细节；默认 INFO） |
| `--field-mode {template,fold}` | `extract`/`pipeline` | 字段主干方案：`template`(B，默认，裁剪到主导签名) / `fold`(A，全路径并集) |

- 输入容错：`root` 指向文件→直接处理；指向目录→递归遍历；目录为空→向上回溯父目录；全程无文件→跳过并记 error。
- 日志统一挂在 `pii_detect` 根命名空间，全流水线（sniff/parse/extract）日志收齐到同一日志文件，同时打印到终端。

---

## 各模块输入 / 输出

### 阶段0 — 内容嗅探 (`src/sniff/`)

| 模块 / 入口 | 作用 | 输入 | 输出 |
|------------|------|------|------|
| `sniffer.py` · `sniff_file(path)` | 单文件真实格式嗅探 | 文件路径 | `(real_format, encoding, confidence)` |
| `voting.py` · `vote_format(lines, text)` | 7 候选格式并行打分 | 头部非空行 + 全文 | `{格式: 分数}` |
| `profiler.py` · `profile_corpus(root, files=None)` | 目录批量画像 | 根目录 / 预收集文件列表 | 交叉表 + 格式分布 + 低置信清单 + 文件总数 |

### 阶段1 — 容错分级解析 (`src/parse/`)

| 模块 / 入口 | 作用 | 输入 | 输出 |
|------------|------|------|------|
| `grade.py` · `grade_parse(path, fmt, enc)` | 按格式路由分发 | 路径 + 格式 + 编码 | `Grade` |
| `json_parser.py` | JSON / JSONL 解析 | `(path, enc)` | `Grade(tier, I, …)` |
| `csv_parser.py` | CSV / TSV 解析 | `(path, enc)` | `Grade` |
| `sql_parser.py` | SQL 文本头部抽取 | `(path, enc)` | `Grade` |
| `sqlite_parser.py` | SQLite 二进制读 schema | `path` | `Grade` |

> `Grade` 关键字段：`tier`(1/2/3/`noise_sample`/`free_text`)、`I`(可恢复性 0~1)、`parsed`(解析摘要)、`fmt`、`encoding`、`error`。

### 阶段2 — Schema 信息提取 (`src/extract/`)

| 模块 / 入口 | 作用 | 输入 | 输出 |
|------------|------|------|------|
| `extractor.py` · `extract_all(tier1_grades, mode)` | 三段管线编排 | tier1 `Grade` 列表 | `(schema_units, vocab_table, global_view)` |
| `schema_partition.py` · `partition_file(grade)` | 文件内 Schema 分片 | 单个 `Grade` | `(list[SchemaPartition], PartitionStats)` |
| `schema_unit.py` · `build_schema_unit(partition, mode)` | 组装单分片五类信息 | `SchemaPartition` | `SchemaUnit`（骨架 / 字段 / 画像 / 拓扑 / PII） |
| `vocab_table.py` · `build_vocab_table(units)` | 跨表同义聚类 | `schema_units` | `(VocabTable, uncertain_list)` |
| `skeleton.py` / `value_profile.py` / `pii_seed.py` | 五类信息子构件 | record / value / key 名 | 结构签名 / value 画像 / PII 种子 |

### 工具 (`src/utils/`)

| 模块 | 作用 | 输入 | 输出 |
|------|------|------|------|
| `encoding.py` | 编码探测 + 安全解码 | bytes | encoding 名 / 解码后字符串 |
| `file_utils.py` | 读文件头、二进制检测、遍历、SQLite 魔数 | 路径 / bytes | bytes / bool / 文件列表 |
| `text_utils.py` | 括号平衡、非空行、列稳定性、JSON 试探 | 文本 | 行 / 布尔 / 度量值 |
| `logger.py` · `setup_logger` / `get_logger` | 日志配置 | name / level / file | `Logger`（`pii_detect` 根，终端 + UTF-8 文件双输出） |

> `parsers/`（LLM 驱动的独立解析器系统）依赖外部框架，与 `src/` 流水线无调用关系，详见 `CLAUDE.md`。

---

## 运行测试

```bash
uv run python -m pytest tests/ -v
```

- **输入**：`tests/` 下 fixtures + 动态构造样本
- **输出**：测试结果（当前 165 用例全部通过），覆盖阶段0 嗅探/投票、阶段1 分级路由、阶段2 分片/单元构建/词汇表。
