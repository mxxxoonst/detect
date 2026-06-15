# PII Detect

多源异构文件的**数据预处理与信息抽取流水线**：从混合格式文件（JSON / JSONL / CSV / TSV / SQL /
SQLite / TXT / 日志）中**嗅探真实格式 → 容错分级解析 → 提取五类结构信息及 PII 种子**。

```
corpus_root → [阶段0 嗅探 sniff] → [阶段1 分级解析 parse] → [阶段2 Schema 提取 extract]
```

- **语言 / 包管理**：Python 3.12+ / [uv](https://github.com/astral-sh/uv)
- **数据规模**：~45,473 文件、GB 量级（真实数据在云服务器，开发用 `test_data/samples/` 小样本）
- **设计原则**：内容嗅探优先于扩展名 · 全程流式不整文件 load · 阶段间 JSONL 落盘 + 断点续跑 ·
  默认不持久化 PII 原值

## 快速开始

```bash
cd F:\zlf\paper_dataPipeline\detect
uv sync                                            # 安装依赖
uv run python test_data/generate.py               # 生成测试样本到 test_data/samples/
uv run python main.py pipeline test_data/samples -o output   # 跑全流水线
uv run python -m pytest tests/ -v                 # 跑测试
```

> ⚠ **本地编辑 + 远程执行**：代码实际跑在远程服务器，GB 级数据也只在远程；本地无 `.venv`、无数据，
> 直接运行会失败。运行/验证一律走 SSH 远程执行，本地仅编辑代码、读小样本、跑不依赖远程数据的单测。

四个子命令（`sniff` / `parse` / `extract` / `pipeline`）的输入输出与标志详见 `quick_start.md`。

## 项目目录（git 跟踪）

```
detect/
├── main.py                  # CLI 入口：argparse 子命令 + resolve_input() 容错路径解析
├── pyproject.toml           # uv 包管理 + 依赖声明（chardet/ijson/json5/pytest）
├── uv.lock                  # 依赖锁定
├── .python-version          # Python 版本固定（3.12+）
│
├── CLAUDE.md                # 项目上下文：稳定的约束/命令/结构/约定/禁止事项
├── README.md                # 本文件：项目总览 + 目录导览
├── quick_start.md           # 快速上手：各阶段作用、输入/输出、命令行用法
├── todo_list.md             # 演进中的模块设计、占位接口、待办与未来方向
├── docs/
│   └── guides.md            # 详细实现指南：业务逻辑 / API / 数据流 / 各阶段算法
│
├── src/                     # ── 主流水线实现 ──
│   ├── constants.py         # 阈值常量、PII 正则、魔数定义
│   ├── sniff/               # [阶段0] 内容嗅探
│   │   ├── sniffer.py       #   sniff_file(path) → (real_format, encoding, confidence)
│   │   ├── voting.py        #   vote_format() 多候选加权投票解开 txt 真实格式
│   │   └── profiler.py      #   profile_corpus() 目录级交叉表 + 格式分布 + 低置信清单
│   ├── parse/               # [阶段1] 容错分级解析（tier1/2/3 + I 可恢复性）
│   │   ├── grade.py         #   Grade dataclass + grade_parse() 按格式路由
│   │   ├── json_parser.py   #   JSON/JSONL: strict(ijson 流式) + tolerant(json5)
│   │   ├── csv_parser.py    #   CSV/TSV: strict(列一致) + tolerant(列漂移度量)
│   │   ├── sql_parser.py    #   SQL 文本: regex 抽 CREATE/INSERT 头部
│   │   └── sqlite_parser.py #   SQLite: sqlite3 只读 URI 读 schema
│   ├── extract/             # [阶段2] Schema 单元化提取（仅 tier1）
│   │   ├── extractor.py     #   编排：内存版 extract_all() + 流式版 stream/finalize
│   │   ├── schema_types.py  #   共享 TypedDict（SchemaPartition/SchemaUnit/FieldInfo/…）
│   │   ├── schema_partition.py # 文件内 Schema 分片
│   │   ├── schema_unit.py   #   单遍折叠遍历组装五类信息
│   │   ├── vocab_table.py   #   跨表同义倒排词表
│   │   ├── skeleton.py      #   信息一：结构骨架
│   │   ├── vocabulary.py    #   信息二：字段名词表（旧 extract_five_infos 用）
│   │   ├── value_profile.py #   信息三：value 画像（统计摘要，不存原值）
│   │   ├── topology.py      #   信息四：拓扑（旧 extract_five_infos 用）
│   │   └── pii_seed.py      #   信息五：PII 种子
│   └── utils/               # 公共工具
│       ├── logger.py        #   pii_detect 根命名空间日志
│       ├── encoding.py      #   chardet 探编码 + safe_decode
│       ├── file_utils.py    #   读头/二进制检测/遍历（全流式）
│       ├── text_utils.py    #   括号平衡/列稳定性/JSON 试探等文本启发式
│       └── (jsonl.py)       #   阶段间 JSONL 落盘 + 断点续跑（容忍截断尾行，尚未入库）
│
├── parsers/                 # [独立参考] LLM 驱动解析器系统（Gateway 模式）
│   ├── txt_parser.py        #   依赖不在本仓库的外部框架，import 会失败
│   ├── json_parser.py       #   与 src/ 无调用关系，仅作参考实现
│   ├── csv_parser.py
│   └── sql_parser.py
│
├── test_data/               # 测试数据
│   ├── generate.py          #   测试数据生成器（格式×质量×编码 矩阵，~27 文件）
│   ├── samples/             #   生成的样本（干净/噪声/截断/空 × UTF-8/GBK/BOM，含扩展名不符）
│   └── TrueDataPart/        #   真实数据片段示例（SchemaUnit.jsonl）
│
├── tests/                   # pytest 测试
│   ├── conftest.py
│   ├── fixtures/            #   测试夹具（json/csv/sql/jsonl 样本）
│   ├── test_sniff/          #   阶段0 测试（sniffer/voting）
│   ├── test_parse/          #   阶段1 测试（grade）
│   └── test_extract/        #   阶段2 测试（partition/unit/skeleton/topology/value/vocab/pii）
│
└── .idea/                   # JetBrains IDE 工程配置
```

> `output/`（流水线运行结果）已被 `.gitignore` 排除，不在版本控制内。
> 标注待入库的 `docs/`、`src/utils/jsonl.py` 为新增文件，尚未提交，结构上属于本仓库。

## 文档导航

| 想了解 | 看这里 |
|--------|--------|
| 怎么跑、各阶段输入输出 | `quick_start.md` |
| 稳定约束 / 命令 / 结构 / 禁止事项 | `CLAUDE.md` |
| 详细实现：算法 / API / 数据流 / 已知问题 | `docs/guides.md` |
| 演进中的设计与待办 | `todo_list.md` |
