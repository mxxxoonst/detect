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
├── main.py                       # CLI 入口 (argparse 子命令)
├── pyproject.toml                # uv 包管理 + 依赖声明
├── src/
│   ├── __init__.py
│   ├── constants.py              # 阈值常量、PII 正則、魔数定义
│   ├── sniff/                    # [阶段0] 内容嗅探
│   │   ├── __init__.py
│   │   ├── sniffer.py            # sniff_file(path) → (fmt, enc, conf)
│   │   ├── voting.py             # vote_format(lines, text) → {fmt: score}
│   │   └── profiler.py           # profile_corpus(root) → 交叉表
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
│       ├── __init__.py
│       ├── encoding.py           # chardet 探编码 + safe_decode
│       ├── file_utils.py         # is_binary(), 读头, walk_files
│       └── text_utils.py         # 括号平衡, 列稳定性, 非空行, JSON 试探
├── tests/
│   ├── conftest.py               # pytest fixtures (samples_dir, make_temp_file)
│   ├── test_sniff/               # 阶段0 测试 (14 用例)
│   ├── test_parse/               # 阶段1 测试 (8 用例)
│   └── test_extract/             # 阶段2 测试 (27 用例)
├── test_data/
│   ├── generate.py               # 测试数据生成器 (~27 文件)
│   └── samples/                  # 生成的测试样本
└── output/                       # 流水线运行结果输出
```

## 架构设计

### 数据流：三阶段流水线

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

## PII 检测关键词

中英双语覆盖 (`src/constants.py:9-18`):

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

`is_binary(raw)` (`src/utils/file_utils.py:20-35`):

- NULL 字节 > 1/256 比例 → 强二进制信号
- 控制字符 (0x00-0x08, 0x0B-0x0C, 0x0E-0x1F, 0x7F) 占比 > 30% → 二进制
- **注意**：UTF-8 中文字节 (>=0x80) 不计入控制字符，避免中文文本误判为二进制

## 命令参考

```bash
# 生成测试数据
uv run python test_data/generate.py [--output test_data/samples] [--seed 42]

# 独立子命令
uv run python main.py sniff   <corpus_root> [-o output]    # 仅阶段0
uv run python main.py parse   <corpus_root> [-o output]    # 阶段0+1
uv run python main.py extract <corpus_root> [-o output]    # 阶段0+1+2 (仅tier1)
uv run python main.py pipeline <corpus_root> [-o output]   # 完整三阶段

# 测试
uv run python -m pytest tests/ -v
```

## 已知问题与边界

1. **日志文件含逗号时间戳**时 (如 `[2024-01-01 00:00:00,123]`), CSV 投票可能压制日志投票 (分数 0.9 vs 0.85)，导致 `.log` 文件被误判为 CSV
2. **短 GBK 文件**: chardet 可能将 GBK 误判为 `cp1250`/`windows-1250`（代码页邻接），需在真实数据上验证
3. **非 .sql 扩展名的 SQL 文件**: 如果 INSERT 语句中逗号分隔值与 CSV 模式竞争，SQL (0.7) 可能输给 CSV (0.9)，需要观察真实分布后再调权重
4. **随机字节文件** (`os.urandom`) 可能因巧合不触发二进制检测被误判为 TSV；真实二进制文件 (exe/png) 含 NULL 字节不会误判
