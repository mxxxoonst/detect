# 严格解析器 / 容错解析器 — 设计与实现方案（parse 阶段）

> 姊妹篇：[`docs/json_csv_schema_handling.md`](./json_csv_schema_handling.md)（extract 阶段 schema 分片/去重）。
> 上游依据：[`resources/研究内容讨论.md`](../resources/研究内容讨论.md) §「解析器问题」与双通道/$I(x)$/对齐三元组。
>
> 本文解决一个明确缺口：当前每个格式只有 **一个函数 `parse_X() → Grade(tier=…)`**，
> 把「严格定标签」与「容错产 IR」两个角色压进同一遍、同一个 `tier` 出口；
> 严格侧（种子门）因此 **不健全**，容错侧在冒充 oracle（尤以 SQL 为甚）。

---

## 0. 两份契约 + 共享内核原则

把解耦的对象定清楚：**解耦的是契约/角色，不是非得两遍扫文件。**

### 0.1 严格解析器 = 谓词（oracle / 种子门）

```
strict_ok(path, fmt, enc) -> StrictVerdict{ clean: bool, reason: str, n_unit: int }
```

- **语义**：零修复判定。整文件在该格式的 *规范解析器* 下零异常消费到 EOF——
  无 json5、无 skip-bad、无 regex 近似、无列漂移容忍、无尾部截断。
- **消费者**：离线种子库构建器（对全语料跑一次）→ Tier1 干净种子库。
- **正确性准则**：对「clean」**高精度（precision）**。拿不准 → 判不干净（保守 / fail-closed）。
  误判干净会污染训练标签；误判不干净只缩小种子池（廉价）。
- `clean ⟺ I_strict = 1 ⟺ tier1`。

### 0.2 容错解析器 = 转换器（IR 产生器）

```
tolerant_parse(path, fmt, enc)
    -> ParseResult{ units: list[SchemaUnit],         # 通道一
                    raw_spans: list[RawSpan],        # 通道二 [RAW]
                    report: RecoveryReport }         # C/P/L/I/I_strict/tier/置信度
```

- **语义**：最大化可恢复量 + 切通道二。json5 / skip-bad / regex 抽取 / partial-array / 列重对齐。
- **消费者**：训练期（注噪种子 $x'$ → tier1/tier2 分类 + IR + 通道切分）；推理期（真实样本 → 编码器输入）。
- **正确性准则**：恢复召回/质量，但 **宁可少救不可错救**（[研究讨论 §3(b)](../resources/研究内容讨论.md)）：
  $P$ 只计 *信得过* 的恢复，拿不准的算法上必须归 $L$（通道二），不得硬塞通道一造成静默污染。

### 0.3 共享内核原则（健全性命门）

> **严格 = `canonical_core(recovery=OFF)`；容错 = `canonical_core(recovery=ON, instrumented)`。**

一份内核、两种模式。由此：

1. **不可漂移**：与 [研究讨论 §2(a)](../resources/研究内容讨论.md)「训练/推理/各阶段必须逐字节相同的容错函数」一致；
   也是 [`schema_partition.py:141`](../src/extract/schema_partition.py) `_stream_json_records`
   注释已踩过的坑（json/json5 兜底两处各写一遍要人肉同步）。
2. **严格判定是容错的免费副产品**：`clean ⟺ 容错跑下来零偏离(deviations==0)`——
   零偏离即「容错的行为与严格内核逐字节一致」。
3. **退化曲线起点正确**（命门）：干净种子库由严格建一次；训练时注噪 $x\to x'$，再由容错判 tier。
   要让 $I(x)$ 有意义，**容错对未注噪 $x$ 的「零偏离」判定必须等于严格当初准它入库的判定**，
   否则 $I(x_{clean})\neq 1$、退化曲线起点就是歪的。共享内核 ⟹ `strict_ok(x) ⟺ tolerant(x).deviations==0` 恒成立。

### 0.4 不需要「两遍扫 GB 数据」

部署时机天然不同，物理上是两个入口、两条管线，但共享内核：

- **严格** 对全语料三分 **一次** → {Tier1 种子 / Tier2 真噪 / Tier3 弃}。
- **容错** 只施加在 **种子子集（注噪后训练）** 与 **真实样本（推理/Tier2 标定）** 上，不重扫全量。

---

## 1. 统一度量：C / P / L → I_strict / I / tier

每文件按 **单元（unit）** 计数（unit 的粒度见下表），$N = C + P + L$：

| 量 | 定义 | 含义 |
| :-- | :-- | :-- |
| $C$ clean | 严格内核直接消费成功的单元 | 干净 |
| $P$ repaired | 容错救回、且 *可信* 的单元（进通道一） | 修复 |
| $L$ lost | 救不回 / 不可信 → 降级通道二 $[RAW]$ | 丢失 |
| $I_{strict}=C/N$ | **种子门** | `tier1 ⟺ I_strict==1 ⟺ P==0 ∧ L==0` |
| $I=(C+P)/N$ | **官方 $I(x)$**（退化曲线） | 通道一占比 |

映射等式：

- $1 - I_{strict} = (P+L)/N$ ＝ 形式层总破坏；
- $1 - I = L/N$ ＝ 不可恢复（通道二）占比；
- $I - I_{strict} = P/N$ ＝ **容错的修复力（healing power）**。

tier 由二者共同决定（不再由单一容错路径铸造）：

```
tier = 1  if I_strict == 1           # 干净种子
     = 2  elif I > 0                  # 可恢复噪声（C+P 有产出）
     = 3  else                        # 不可解析
```

> 单元粒度：JSON=顶层数组元素；JSONL=行；CSV/TSV=数据行；SQL=语句；xlsx=sheet 行。

---

## 2. 逐格式：严格内核 + 容错叠加

整合 [研究讨论 §解析器问题](../resources/研究内容讨论.md) 两表 + [json_csv_schema_handling.md](./json_csv_schema_handling.md) 的容错细化。

### 2.1 JSON（`src/parse/json_parser.py`）

| | 内容 |
| :-- | :-- |
| 严格内核 | `ijson.items(f,"item")` 流式 **全程零异常消费到 EOF**；顶层形状符合（数组）。无 json5、无 partial。 |
| 容错叠加 | ① `json5.loads` 修尾逗号/单引号/注释 → 这些对象计 $P$；② ijson 崩溃前的好记录计 $C$，崩溃点之后估算为 $L$（[`json_parser.py:37-61`](../src/parse/json_parser.py) partial-array 已有雏形）。 |
| 救不回 → $L$ | json5 也抛的残片；崩溃点之后无法消费的部分。 |

**要修的现状 bug：**

- **tier1 泄漏**：[`json_parser.py:50`](../src/parse/json_parser.py)（Level-2）与
  [`json_parser.py:176`](../src/parse/json_parser.py)（`_json_tolerant`）都有 `tier = 1 if I >= 0.99`，
  **json5 修过的文件能拿 tier1 漏进种子库**。修：容错路径 **封顶 tier2**，`tier1 ⟺ 严格零偏离`。
- **JSONL-as-`.json` 静默丢文件**（[json_csv 文档 §2.1.6](./json_csv_schema_handling.md)）：
  顶层数组取不到记录时探测「逐行独立对象」，是则转 JSONL 迭代（逐行 `json.loads`，
  与 [`json_parser.py:108`](../src/parse/json_parser.py) `parse_jsonl` 一致）。严格与容错两侧都要加这个探测。
- **早期崩溃后续不计**（[研究讨论 §当前问题](../resources/研究内容讨论.md)）：partial-array 估算需把崩溃点后的好记录纳入 $L$ 计数而非丢弃。

> 记录的恢复加载原理（`raw_decode` 逐元素「保留干净前缀」、`[`/`]` 降维成元素流）见
> [json_csv 文档 §3.2](./json_csv_schema_handling.md)；它与生产 ijson partial 是同一语义（$C/P$ vs $L$）。

### 2.2 JSONL（`src/parse/json_parser.py:108`）

| | 内容 |
| :-- | :-- |
| 严格内核 | 每行 `json.loads` 全过（`bad==0`）——[`json_parser.py:134`](../src/parse/json_parser.py) 已近似，抽成谓词即可。 |
| 容错叠加 | 逐行 skip bad：好行 $C$，坏行 $L$（无中间「修复」态，故 $P$ 一般为 0）。 |

### 2.3 CSV / TSV（`src/parse/csv_parser.py`）

| | 内容 |
| :-- | :-- |
| 严格内核 | `csv.reader`（**quote-aware**）+ 列数全等 + 表头可检出。`strict_ok ⟺ reader 无错 ∧ 列数全等 ∧ header 可判`。 |
| 容错叠加 | skip / 按 **众数列** 重对齐：偏离众数但仍成行的记录 → $P$；reader 直接丢的行 → $L$。 |

**要修的现状 bug / 硬化点：**

- **CSV $I\equiv1.0$**（[研究讨论 §当前问题](../resources/研究内容讨论.md)：good/total 无条件同步自增）：
  改为 `I_strict = 1.0 if 列全等 else modal_consistent/total`；$I=(C+P)/N$。
- **拆函数**：[`csv_parser.py:24`](../src/parse/csv_parser.py) `_parse_delimited` 当前严格（[`:60-62`](../src/parse/csv_parser.py)）
  与容错（[`:64-77`](../src/parse/csv_parser.py)）同体，tier 是唯一区分 → 拆成 strict 谓词 + tolerant 转换。
- **quote 感知切列**（[json_csv 文档 §3.3.1](./json_csv_schema_handling.md)）：禁 `split(sep)`，
  列数是最基本结构不变量；[`csv_parser.py:80`](../src/parse/csv_parser.py) `_sniff_delimiter` 嗅探也须 quote 感知。
- **表头探测**（[json_csv 文档 §3.3.2](./json_csv_schema_handling.md)）：类型不一致检验（多数列）+ 全字符串回退（词法/唯一性）+ 低置信弃权。
- **类型格**（[json_csv 文档 §3.3.3](./json_csv_schema_handling.md)）：`email⊏string`、`phone⊏numeric-ish⊏string`、`int⊏num`；主导类型+容差；高信号列复用 `value_profile` 的 pattern。

### 2.4 SQL（`src/parse/sql_parser.py` + 新增 `src/parse/sql_strict.py`）—— 最大缺口

当前 **没有严格侧**：[`sql_parser.py:50`](../src/parse/sql_parser.py) `I = complete/total`（CREATE/INSERT
关键词纯度），[`:51`](../src/parse/sql_parser.py) `tier = 1 if I == 1.0`；
[`:79`](../src/parse/sql_parser.py) `_check_unclosed_quotes` 裸数引号奇偶（不认 `''`/`\'`/`$$`）、只当
tier2 元数据不影响 tier、且只读 64KB 头（[`:22`](../src/parse/sql_parser.py)）对尾部截断全盲。
后果：带未闭合引号/截断的 SQL 仍 `I=1.0→tier1` 错进种子库。

| | 内容 |
| :-- | :-- |
| 严格内核 | **超集分句器状态机 `scan_sql`**（§3 详设）：引号/注释/括号/`$$`/`--` 感知，**全语句平衡、扫到 EOF 无截断、每条可归类** ⟹ `strict_ok`。 |
| 容错叠加 | 现有 regex（[`sql_parser.py:11`](../src/parse/sql_parser.py) `CREATE_INSERT_RE`）**降级**为抽取器，跑在 `scan_sql` 切出的语句边界上：损坏语句里 regex 仍能抽出的 表名+列清单 → schema 进通道一（$P$）；连头都抽不出 → $L$。 |
| 度量修正 | $I$ 改为 **语句级可恢复性** $(C+P)/N$，不再是关键词纯度；引号/括号闭合由状态机 **精确** 判定取代裸奇偶。 |
| 方言探测 | [`sql_parser.py:137`](../src/parse/sql_parser.py) `_detect_sql_dialect` 保留为弱元数据，**不参与** 严格判定。 |
| PII 红线 | 绝不持久化语句原文；抽出 表名/列名 等结构事实后即丢弃中间文本。 |

---

## 3. SQL 超集分句器 `scan_sql` 详设

目标：方言无关地把 SQL 文本切成语句边界，同时报告每条是否「平衡且完整」。
**流式**（不读全文件、不止 64KB），单遍状态机。

```
状态: NORMAL | SQUOTE | DQUOTE | BTICK | LINE_CMT | BLOCK_CMT | DOLLAR(tag)
转移要点:
  NORMAL:  '  -> SQUOTE        "  -> DQUOTE        `  -> BTICK
           -- -> LINE_CMT      /* -> BLOCK_CMT     $tag$ -> DOLLAR(tag)
           ;  -> 在 NORMAL 且括号 depth==0 时才切句
           ( )-> 维护 depth
  SQUOTE:  '' 视为转义留在串内; \' 视为转义(可配置方言); 单独 ' -> 回 NORMAL
  DQUOTE/BTICK: 同理处理成对/转义
  LINE_CMT: 直到 \n 回 NORMAL
  BLOCK_CMT: 直到 */ 回 NORMAL
  DOLLAR(tag): 直到再次遇到同名 $tag$ 回 NORMAL
缓冲: MAX_STMT_BUF 上限(如 1<<20)防御异常长语句
返回(每文件): (C, P, L, n_form, n_struct, detail)
  - 扫到 EOF 时若仍处于非 NORMAL 状态 / 括号 depth>0 / 末句无分隔符正常结束
    => 该(尾)语句判为截断 => 计 L, n_form 记 truncated/unbalanced
  - 平衡且能归类的语句 => C(严格模式) 或 P(容错抽取成功)
```

**严格模式** = `scan_sql` 全程零异常、零截断、所有语句平衡且可归类 ⟹ `strict_ok=True`。
**容错模式** = 在 `scan_sql` 边界上跑 `CREATE_INSERT_RE` 抽 schema：抽出计 $P$，抽不出计 $L$。

验证基准：`test_data/samples` 中 `noisy_truncated.sql`（含 `'Broken quote, 29.99);` 未闭合）
应从 **tier1 落到 tier2**（$I_{strict}<1$）。

---

## 4. 反漂移：共享内核 / 共享容错（DRY）

[研究讨论 §2(a)](../resources/研究内容讨论.md) 的硬约束：容错恢复（含通道切分）必须 **唯一一份**，
被 parse 算 $I$、extract 分片、将来组装编码器输入全调。落点：

- 新增 `src/parse/recovery.py`（或按格式归并）：承载 *规范内核 + recovery 开关*，
  导出 `strict_ok()` 与 `tolerant_parse()` 两入口。
- 用它 **替换** [`schema_partition.py:141`](../src/extract/schema_partition.py) `_stream_json_records`
  内重复的 json/json5 兜底，与 parse 阶段共用同一函数。
- 同源还有 SQL VALUES 解析（`schema_partition._collect_sql_rows_into` 与
  `extractor._collect_sql_rows` 现各写一份「逻辑一致」）——一并收口到共享内核。

---

## 5. 数据契约改动

- [`grade.py`](../src/parse/grade.py) `Grade` 增字段：
  - `I_strict: Optional[float] = None`（种子门，序列化进 `grades.jsonl`，
    [`grade_from_summary`](../src/parse/grade.py) 读回）；
  - 可选 `c_count/p_count/l_count`（或并入 `n_detail`）。
- 新类型（建议入 `schema_types.py` 或 `parse/` 内）：
  - `StrictVerdict{ clean, reason, n_unit }`；
  - `RawSpan{ anchor_path, byte_range }`——通道二锚点。
    **注意边界**：`covers_fields`（该 $[RAW]$ 覆盖了哪些干净字段 path，供 $\mathcal{L}_{consist}$/latent-path 头）
    **不由解析器产出**——推理期无从得知；它来自训练期注噪算子的对齐三元组
    （[研究讨论 §问题](../resources/研究内容讨论.md) line 135）。解析器只产 `anchor_path`。
  - `RecoveryReport{ C,P,L,N,I,I_strict,tier, per_unit_conf }`。
- **per-unit 恢复置信度**：容错对每个 $P$ 单元输出 $[0,1]$ 置信度 → 通道一质量信号，
  喂/初始化 $c_{struct}$（[研究讨论 §3](../resources/研究内容讨论.md)）。
- 两入口（`strict_ok` / `tolerant_parse`）：严格供种子库构建器；容错供训练/推理数据加载。

---

## 6. 代码改动清单（按文件）

| 文件 | 改动 |
| :-- | :-- |
| `src/parse/grade.py` | 加 `I_strict`（+可选 C/P/L）；`grade_from_summary` 读；`grades.jsonl` 落。 |
| `src/parse/sql_strict.py` **(新)** | `scan_sql` 超集分句器状态机（§3）。 |
| `src/parse/sql_parser.py` | $I$ 改语句级 $(C+P)/N$；regex 降级为容错抽取跑在 `scan_sql` 边界；流式扫到 EOF；引号判定换状态机。 |
| `src/parse/json_parser.py` | 容错路径封顶 tier2（堵 `:50`/`:176` tier1 泄漏）；JSONL-as-`.json` 探测；partial 计 $L$。 |
| `src/parse/csv_parser.py` | 拆 strict 谓词 / tolerant；修 $I\equiv1.0$；`_sniff_delimiter` quote-aware；表头探测；类型格。 |
| `src/parse/xlsx_parser.py` | 形式化为 strict-only（打开成功+sheet 可读）。 |
| `src/parse/recovery.py` **(新)** | 共享容错恢复 + 通道切分；导出 `strict_ok`/`tolerant_parse`。 |
| `src/extract/schema_partition.py` | `_stream_json_records` 等改调 `recovery.py`，消除重复 json5 兜底。 |
| `tests/test_parse/` | 逐格式 strict vs tolerant；**一致性属性 `strict_ok(x) ⟺ tolerant(x).deviations==0`**；`noisy_truncated.sql` tier1→tier2。 |

---

## 7. 落地顺序与验证

1. **SQL `scan_sql`**（最大健全性漏洞，修种子库污染）→ 验证 `noisy_truncated.sql` tier1→tier2。
2. **JSON tier1 泄漏堵 + JSONL-as-`.json`；CSV strict 谓词拆分 + $I\equiv1.0$ 修** → 在 `test_data/DataPart`、`csv_tests` 验证。
3. **`Grade.I_strict` 字段 + `grades.jsonl` 序列化 + `grade_from_summary`**。
4. **抽 `strict_ok`/`tolerant_parse` 两入口 + 共享内核重构**（反漂移）。
5. **`recovery.py` 共享**，替换 `schema_partition` 重复 json5。
6. **远程全量复跑**：`ssh root@172.17.66.200 "cd data/header_parser/zlf/PII_detect/detect/ && uv run python main.py parse <corpus> -o output"`；
   预期 **Tier2 显著上升**（[研究讨论 §当前问题](../resources/研究内容讨论.md)：T2≈700 偏低，resync 缺失把可恢复文件冲进 T3 饿死 T2；
   修复后 T2 上升本身即 $I$ 实现的 sanity check）。

---

## 8. 约束（不可违反）

- **本地编辑 + 远程执行**：本地无 `.venv`/数据，运行/验证一律 `ssh root@172.17.66.200`。
- **流式优先**：整文件 load 优先改用 **流式读入**——JSON 走 ijson、JSONL 逐行、CSV `csv.reader`、SQL `scan_sql` 流式扫到 EOF（GB 量级文件为硬性要求）。
- **PII 持久化**：原值默认不落；**唯一持久化的原值是样本值**（`--keep-samples` 按 pattern 去重 ≤5 个 / `--mask-samples` 脱敏，owner 授权，落在 extract/pipeline 的 profile）。
  parse 阶段 `grades.jsonl` **不含样本**，仅统计/计数/path/I/I_strict；通道二 $[RAW]$ 原文仅训练/推理 **内存态**，不落 artifact。
- **共享内核**：`strict_ok` 与 `tolerant_parse` 必须基于同一规范内核，禁两处各写恢复逻辑。

---

## 9. 附录：Claude Code 任务提示词

> 把下面整段贴进一个新的 Claude Code 会话即可分阶段实现（建议一次只放一个 Phase 的指令）。

```
你在 PII detect 项目（F:\zlf\paper_dataPipeline\detect）实现 parse 阶段的「严格/容错解析器解耦」。

先读三份依据，全部按其结论实现，不要自行更改架构决定：
- docs/parser_strict_tolerant_design.md（主方案，含逐格式表、scan_sql 详设、代码改动清单、落地顺序）
- resources/研究内容讨论.md 的「解析器问题」与双通道/I(x)/对齐三元组、§2(a)(b)、§3
- docs/json_csv_schema_handling.md 的 §3.2（恢复加载器）、§3.3（三个硬化点）

硬约束（违反即返工）：
- 本地无 .venv/数据，禁止本地运行；运行/验证一律远程：
  ssh root@172.17.66.200 "cd data/header_parser/zlf/PII_detect/detect/ && uv run python <…>"
- 整文件 load 优先改流式读入（ijson/逐行/csv.reader/SQL 状态机流式扫到 EOF；GB 文件为硬性要求）。
- 原值默认不落；唯一持久化的原值是样本值（--keep-samples/--mask-samples，owner 授权，仅 extract/pipeline 的 profile）。
  parse 阶段 grades.jsonl 不含样本，只落统计/计数/path/I/I_strict。
- 严格=canonical_core(recovery=OFF)、容错=canonical_core(recovery=ON, 仪表化)，共享同一内核，
  保证 strict_ok(x) ⟺ tolerant(x).deviations==0（这是退化曲线起点 I(x_clean)=1 的命门）。
- 遵守 CLAUDE.md：TypedDict 不用 dataclass（Grade 除外，沿用现状）、日志走 pii_detect 命名空间、不用 print。

按 docs/parser_strict_tolerant_design.md §7 顺序，本次只做 Phase 1：
新建 src/parse/sql_strict.py 的 scan_sql 超集分句器（状态机见主方案 §3），
并把 src/parse/sql_parser.py 的 I 改为语句级 (C+P)/N、现有 regex 降级为跑在 scan_sql 边界上的容错抽取、
引号闭合判定换成状态机、读取改流式扫到 EOF（替代 64KB 头）。
新增/更新 tests/test_parse 下的单测覆盖：平衡/未闭合引号/块注释/$$ dollar-quote/尾部截断。
完成后给出远程验证命令，并说明预期 noisy_truncated.sql 从 tier1 落到 tier2。
不要顺手做 Phase 2+。
```
