# plan.md — parse 阶段「严格/容错解析器解耦」分阶段执行计划

> 给 Claude Code 按序自动执行。承接已完成的 **Phase 1**（SQL `scan_sql` 超集分句器 +
> `Grade.I_strict` 字段 + `grades.jsonl` 序列化 + `tests/test_parse/test_sql_strict.py`），
> 按 [`docs/parser_strict_tolerant_design.md`](docs/parser_strict_tolerant_design.md) §7 落地顺序，
> 依次完成 **Phase 2–5**；**Phase 6**（真实语料全量验证）由用户最终执行。

## 执行前必读（依据）

- [`docs/parser_strict_tolerant_design.md`](docs/parser_strict_tolerant_design.md) — 主方案（逐格式表、scan_sql 详设、改动清单、落地顺序）
- [`resources/研究内容讨论.md`](resources/研究内容讨论.md) — §解析器问题 / §2(a)(b) / §3 / 双通道·I(x)·对齐三元组
- [`docs/json_csv_schema_handling.md`](docs/json_csv_schema_handling.md) — §3.2（恢复加载器）/ §3.3（三个硬化点）
- Phase 1 产物：`src/parse/sql_strict.py`、`src/parse/sql_parser.py`、`src/parse/grade.py`（`I_strict` 已加）、`main.py`（已序列化 `I_strict`）

---

## 执行协议（总则，必须遵守）

1. **一次只做一个 Phase，严格按序** 2 → 3 → 4 → 5。
2. **每个 Phase 完成后**：
   1. 本地跑该 Phase 单测：`uv run python test_data/generate.py`（如需样本）+ `uv run python -m pytest tests/test_parse/ -v`（或该 Phase 指定文件）。
   2. 单测全绿 → 更新 [`docs/guides.md`](docs/guides.md) 对应章节（见各 Phase「文档同步」）→ 输出该 Phase **完成报告**（改了哪些文件 / 单测结果 / guides.md 改了哪些 §）→ 继续下一 Phase。
3. **任何失败立即停止、不继续**：单测出现 assertion/error，或实现受阻 / 与依据冲突 → **停下**，报告失败现象 + 定位 + 建议，等待人工。**不得跳过、不得带病推进。**
4. **环境不可用**：若本地无法 `uv run`（无 uv / 依赖装不上）→ **不视作代码失败**；报告中标注「本地单测未执行，留待用户远程」，可继续编码，但必须如实说明未验证。
5. **真实语料验证不由你做**：45k 全量 / GB 级真实文件的验证由用户最终远程执行（Phase 6）；你只跑本地合成样本单测。
6. **全局硬约束**（违反即返工）：
   - 整文件 load 优先 **流式读入**（ijson / 逐行 / `csv.reader` / 状态机扫到 EOF；GB 文件为硬性要求）。
   - 原值默认不落；parse 阶段 `grades.jsonl` 只落统计 / 计数 / path / I / I_strict，**不含样本**。
   - 严格 = `canonical_core(recovery=OFF)`、容错 = `canonical_core(recovery=ON, 仪表化)`，**共享同一内核**，保证 `strict_ok(x) ⟺ tolerant(x).deviations==0`（退化曲线起点 $I(x_{clean})=1$ 的命门）。
   - 遵守 [`CLAUDE.md`](CLAUDE.md)：数据结构用 TypedDict（`Grade` 除外，沿用 dataclass）；日志 `log = get_logger(__name__)` 走 `pii_detect` 命名空间、**不用 print**；`except` 吞错处记日志（WARNING 实质失败 / DEBUG 可接受回退）。

### 启动提示词（贴一次即可自动跑完 2→5）

```
读 plan.md 并严格按其「执行协议」从 Phase 2 开始依次执行到 Phase 5：每个 Phase 完成后跑本地单测
（uv run python -m pytest tests/test_parse/ -v），全绿才更新 docs/guides.md 对应章节并继续下一 Phase；
任何单测失败或实现受阻立即停止并报告，不要继续。Phase 6 是用户的远程全量验证，你做完 Phase 5 即停下交回。
```

> 也可逐 Phase 手动驱动：把对应 Phase 的「任务提示词」单独贴入。

---

## Phase 2 — JSON / JSONL / CSV 严格谓词 + 容错 + I_strict

**依据**：design §2.1 / §2.2 / §2.3、§7 步骤 2 与步骤 3（JSON/CSV 部分）；json_csv 文档 §3.2、§3.3.1。

**改动文件**：`src/parse/json_parser.py`、`src/parse/csv_parser.py`、`tests/test_parse/`（新增/更新 json、csv 用例）。

**实现要点**：

- **JSON**（`json_parser.py`）：
  1. 容错路径 **封顶 tier2**：堵 `:50`（Level-2）与 `:176`（`_json_tolerant`）的 `tier=1 if I>=0.99`；`tier1 ⟺ ijson 全程零异常消费到 EOF`（零偏离）。
  2. **JSONL-as-`.json`**：顶层数组取不到记录（`good==0`）时探测「逐行独立对象」，是则转 JSONL 逐行 `json.loads` 迭代（与 `parse_jsonl` 一致），消除静默零产出。
  3. partial-array 把 **崩溃点之后** 的记录纳入 $L$ 计数（修「早期崩溃后续不计」）。
  4. 回填 `I_strict`：$C$=ijson 严格消费成功记录、$P$=json5/partial 救回、$L$=残片/崩溃后；`I_strict=C/N`、`I=(C+P)/N`、tier 由二者定。
- **JSONL**：`I_strict = 好行/总行`（`bad==0 ⟺ I_strict==1 ⟺ tier1`），回填。
- **CSV/TSV**（`csv_parser.py`）：
  1. 拆 `_parse_delimited`（`:24`）→ strict 谓词（**quote-aware** `csv.reader` + 列数全等）+ tolerant（列漂移/skip）。
  2. 修 **CSV $I\equiv1.0$**：`I_strict = 列全等 ? 1.0 : modal_consistent/total`；`I=(C+P)/N`（$C$=众数列且 reader 无错的行、$P$=偏离众数但成行、$L$=reader 丢的行）。
  3. `_sniff_delimiter`（`:80`）改 quote-aware（用 `csv.reader` 数列，禁 `split(sep)`）。
  4. 回填 `I_strict`。
- **范围界定**：CSV **表头探测（§3.3.2）与类型格（§3.3.3）属 extract 阶段 schema 去重，不在本 Phase**；本 Phase 只做 parse 必需的 quote-aware + 列一致 + I_strict。

**本地验证**：
```
uv run python test_data/generate.py && uv run python -m pytest tests/test_parse/ -v
```
预期：JSON 尾逗号/单引号样本 → tier2（不再 tier1）；JSONL-as-`.json` 不再零产出；CSV 列漂移样本 `I_strict<1`、列全等样本 `I_strict==1`；全绿。

**文档同步（guides.md）**：
- §3.1 `Grade` 表：加 `I_strict` 行（种子门 $C/N$；`tier1⟺I_strict==1`）。
- §3.2 各 parser 策略表（`:106-114`）：更新 json / csv 行的 strict / tolerant / I(x) 列（quote-aware、`I=(C+P)/N`、`I_strict`）。
- §12 已知问题：划掉「CSV I≡1.0」「JSON 早期崩溃后续不计」「JSONL-as-.json 零产出」。

**任务提示词**：
```
按 plan.md 执行协议做 Phase 2（只做 Phase 2）。读 docs/parser_strict_tolerant_design.md §2.1/2.2/2.3、
docs/json_csv_schema_handling.md §3.2/§3.3.1。改 src/parse/json_parser.py（容错封顶 tier2、JSONL-as-.json
转 JSONL 迭代、partial 计 L、回填 I_strict）与 src/parse/csv_parser.py（拆 strict/tolerant、修 I≡1.0、
_sniff_delimiter 与列计数全部 quote-aware csv.reader、回填 I_strict）；CSV 表头探测/类型格不做（属 extract）。
新增/更新 tests/test_parse 覆盖：JSON 容错样本不再 tier1、JSONL-as-.json 非零产出、CSV 列漂移 I_strict<1、列全等 I_strict==1。
本地跑 uv run python test_data/generate.py && uv run python -m pytest tests/test_parse/ -v；全绿才更新
docs/guides.md §3.1/§3.2/§12 并报告；任何失败立即停下报告，不做 Phase 3+。
```

---

## Phase 3 — xlsx strict-only + I_strict 全格式贯通 + 跨格式一致性

**依据**：design §2 表（xlsx 行）、§7 步骤 3 收尾。

**改动文件**：`src/parse/xlsx_parser.py`、`tests/test_parse/`。

**实现要点**：

- **xlsx 形式化为 strict-only**：`openpyxl(read_only)` 打开成功 + sheet 可枚举 + 表头可读 → `I_strict=1.0` / tier1；打开失败 / 损坏 → tier3。二进制无容错中间态，`strict==tolerant`。回填 `I_strict`。
- **全格式贯通自检**：确认 json / jsonl / csv / tsv / sql / xlsx **六格式都回填了 `I_strict`**，且 `grade_from_summary` 读、`main.py` 落（Phase 1 已具），无遗漏。
- **跨格式一致性单测**：每格式构造一个干净样本，断言命门属性 `strict_ok ⟺ I_strict==1 ⟺ deviations==0`。

**本地验证**：
```
uv run python -m pytest tests/test_parse/ -v
```
预期：各格式干净样本 `I_strict==1`、含噪样本 `I_strict<1`；全绿。

**文档同步（guides.md）**：§3.1 / §3.2 补 xlsx 的 `I_strict`；§3.3 I(x) 横向对比补 `I_strict` 与 `deviations==0` 命门。

**任务提示词**：
```
按 plan.md 执行协议做 Phase 3（只做 Phase 3）。把 src/parse/xlsx_parser.py 形式化为 strict-only
（打开+sheet+表头可读 → I_strict=1.0/tier1，失败 → tier3，回填 I_strict）。自检 json/jsonl/csv/tsv/sql/xlsx
六格式都回填 I_strict 且 main.py 已序列化。新增跨格式一致性单测：干净样本 strict_ok ⟺ I_strict==1 ⟺ deviations==0。
本地 uv run python -m pytest tests/test_parse/ -v 全绿才更新 docs/guides.md §3.1/§3.2/§3.3 并报告；失败即停。
```

---

## Phase 4 — 抽 `strict_ok` / `tolerant_parse` 两入口 + 共享内核（含 SQL GB 流式收尾）

**依据**：design §0.3 / §4 / §5 / §7 步骤 4。

**改动文件**：新增 `src/parse/recovery.py`（或在 `grade.py` 增两入口）；各 parser 重构为「canonical_core + recovery 开关」；`src/parse/sql_strict.py`、`src/parse/sql_parser.py`（惰性消费收尾）。

**实现要点**：

- **两入口**：`strict_ok(path,fmt,enc) -> StrictVerdict{clean,reason,n_unit}`、`tolerant_parse(path,fmt,enc) -> ParseResult{units, raw_spans, report:RecoveryReport}`（类型见 design §5）。
- 各 parser 重构成 **共享内核 + recovery 开关**（严格=OFF、容错=ON 仪表化），保证 `strict_ok(x) ⟺ tolerant(x).deviations==0`。
- **SQL GB 流式收尾（Phase 1 余项）**：`parse_sql_text` 改为 **惰性消费** `scan_sql_statements(_chunks())`，只累加 $C/P/L$ + `has_create/has_insert` + 有界样本，**不再 `list()` 物化全部语句**（消除 GB SQL 内存爆掉的隐患）。
- 单测：两入口契约 + 一致性属性 + SQL 大文件流式（拼接大样本断言惰性迭代 / 内存有界，至少断言不物化全量）。

**本地验证**：
```
uv run python -m pytest tests/test_parse/ -v
```
预期：两入口单测 + 一致性属性全绿；SQL 流式不退化。

**文档同步（guides.md）**：§3 增「严格/容错两入口 + 共享内核」小节；§12 划掉「SQL 全量物化 statements」余项。

**任务提示词**：
```
按 plan.md 执行协议做 Phase 4（只做 Phase 4）。读 design §0.3/§4/§5。抽出 strict_ok / tolerant_parse 两入口
（新增 src/parse/recovery.py 或 grade.py），各 parser 重构为 canonical_core + recovery 开关，保证
strict_ok(x) ⟺ tolerant(x).deviations==0。并把 sql_parser.parse_sql_text 改为惰性消费
scan_sql_statements(_chunks())、只累加 C/P/L + has_create/insert + 有界样本、不再 list() 物化全部语句。
新增单测：两入口契约、一致性属性、SQL 流式。本地 pytest 全绿才更新 docs/guides.md §3/§12 并报告；失败即停。
```

---

## Phase 5 — `recovery.py` 共享容错，消除 schema_partition 重复

**依据**：design §4 / §7 步骤 5；研究讨论 §2(a)；json_csv 文档（`_stream_json_records` 镜像）。

**改动文件**：`src/extract/schema_partition.py`（改调共享 recovery）；可能 `src/extract/extractor.py`（SQL VALUES 解析收口）。

**实现要点**：

- 把 `schema_partition._stream_json_records`（`:141`）/ `_detect_explicit_keys` 内 **重复的 json/json5 兜底** 改调 Phase 4 的共享容错函数——parse 与 extract 用 **同一份**（反漂移）。
- SQL VALUES 解析（`_collect_sql_rows_into` vs `extractor._collect_sql_rows` 两份「逻辑一致」）收口到共享内核。
- 单测：同一容错函数在 parse 与 extract 产出一致（如 JSONL-as-`.json` / 尾逗号 JSON 两阶段产记录数一致）。

**本地验证**：
```
uv run python -m pytest tests/ -v
```
（parse + extract 全套）预期：阶段间一致性单测全绿。

**文档同步（guides.md）**：§4.2（`partition_file`）/ §12 注明容错恢复已收口为单一共享函数，阶段间不再各写一份。

**任务提示词**：
```
按 plan.md 执行协议做 Phase 5（只做 Phase 5）。把 src/extract/schema_partition.py 的 _stream_json_records /
_detect_explicit_keys 内重复 json/json5 兜底改调 Phase 4 的共享容错函数；SQL VALUES 解析两份「逻辑一致」收口。
新增阶段间一致性单测（JSONL-as-.json / 尾逗号 JSON 在 parse 与 extract 产记录数一致）。本地
uv run python -m pytest tests/ -v 全绿才更新 docs/guides.md §4.2/§12 并报告。做完 Phase 5 即停，交回用户做 Phase 6。
```

---

## Phase 6 — 用户最终验证（远程全量，**不自动执行**）

**由用户执行**，Claude 在 Phase 5 通过后停止并交回：

```bash
ssh root@172.17.66.200 "cd data/header_parser/zlf/PII_detect/detect/ && \
  uv run python main.py parse <corpus_root> -o output --restart && \
  head output/grades.jsonl"
```

**预期**（design §7）：
- **Tier2 显著上升**（原 T2≈700 偏低；修复后可恢复文件不再被冲进 T3 → 这是 $I$ 实现的 sanity check）；
- `grades.jsonl` 的 `I_strict` 分布合理（tier1 全为 1.0，tier2 < 1.0）；
- `noisy_truncated.sql` → tier2、`clean_schema.sql` → tier1。

---

## 附：Phase ↔ design §7 落地顺序对照

| 本计划 | design §7 步骤 | 状态 |
| :-- | :-- | :-- |
| Phase 1 | 1. SQL `scan_sql` + `Grade.I_strict` 字段 + 序列化 | ✅ 已完成 |
| Phase 2 | 2. JSON tier1 泄漏 + JSONL-as-.json；CSV strict 拆分 + I≡1.0 | ⬜ |
| Phase 3 | 3. `I_strict` 全格式贯通（字段/序列化 Phase 1 已具）+ xlsx | ⬜ |
| Phase 4 | 4. 两入口 + 共享内核（+ SQL GB 流式余项） | ⬜ |
| Phase 5 | 5. `recovery.py` 共享，消除 schema_partition 重复 | ⬜ |
| Phase 6 | 6. 远程全量复跑（Tier2 上升 sanity check） | ⬜ 用户执行 |
