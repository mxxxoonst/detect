# 架构审计报告：未用代码 / 旁路 / 白算

> 审计日期：2026-06-26 ｜ 范围：「数据预处理 → SchemaUnit → 合成原数据」活跃路径
> 方法：只读静态分析（Grep/Glob/Read 追 import 与调用点），未运行流水线。

## 活跃路径定义

- **预处理 → IR SchemaUnit**：`main.cmd_extract`/`cmd_pipeline` 的 **`--ir` 分支**
  （`compact=True`，走 `finalize_ir_from_units`，**跳过** vocab_table）→
  阶段1 `_stream_grades`(sniff+grade_parse) → 阶段2 `stream_schema_units(compact=True)`
  → `schema_dedup` → `finalize_ir_from_units`。
- **IR SchemaUnit → 合成**：`src/synth/compare.py` →
  `render_unit` / `render_llm_fill`(填值) / `render_llm`(整篇) / `validate_render`。

## 已核验的三个关键事实（✓ = 本次 grep 复核确认）

1. ✓ **`parsers/` 目录已不存在**（Glob 无结果）。CLAUDE.md / 文档中关于 `parsers/` 的整段**已过时**。
2. ✓ **`parse/recovery.py` 完全未接线**：生产链路用 `grade_parse` + `parse/json_recovery.py`；
   `parse/recovery.py`（Phase-4 设计的 `strict_ok`/`tolerant_parse` 双入口）**仅被
   `tests/test_parse/test_recovery.py` import**。注意它与生产在用的 `json_recovery.py` 是**两个不同模块**。
3. ✓ **`extractor.extract_all` + `extract_five_infos` 是死簇**：仅在 `extractor.py` 内自引用
   （`extract_five_infos`→`extract_all`）+ 文档提及，**无任何生产调用者、无测试**。

> 一处框架纠正：整篇渲染 `render_llm` **不是被绕过**，而是 `compare.py` 的**默认**路径；
> 填值模式 `render_llm_fill` 是 `--fill` opt-in。两条都活跃、都有测试。

---

## A 活跃（IR→合成主干，概览）

- **预处理→IR**：`_stream_grades` → `_run_extract_stream(ir_only=True)` →
  `stream_schema_units(compact=True)`(extractor.py:129) → `build_schema_unit` 的 compact 分支
  (schema_unit.py:114-128：`_walk_fold`/`_backbone_and_skeleton`/
  `_most_common_skeleton_as_path_list`/`_compact_skeleton`/`structure_signature`) →
  `schema_dedup`(dedup_csv/dedup_json/representative_sizes/rewrite_units_deduped) →
  `finalize_ir_from_units`→`_aggregate_global_view`。
- **parse**：`grade_parse`→json/csv/sql/xlsx parser、`json_recovery.*`、
  `sql_strict.iter_sql_file_statements`。
- **partition**：`partition_file`→各 `_partition_*`、`UnionSchemaClusterer.add/compatible`、
  `skeleton.structure_signature/norm_type/leaf_types/compatible`。
- **synth**：`compare.render_compare` → `render_unit`(+`_build_tree`/`_insert`/`_gen_value`/
  `_default_value`/`_render_*`/`surface_format`)、`validate_render`、
  **填值** `render_llm_fill`/`build_value_prompt`/`_parse_value_matrix`/`make_fill_value_fn`、
  **整篇** `render_llm`/`build_prompt`、`OpenAICompatClient`（`--llm openai`）。
- **utils**：`jsonl`(append/iter/count_lines)、`logger`、`encoding`、`file_utils`、`text_utils` 子集。

---

## B 仅遗留 / 旁路（可达但不喂 IR/合成）

| 组件 | 证据 | 备注 |
|---|---|---|
| `extractor.finalize_from_units` (extractor.py:189) | 唯一调用 main.py:290（**非 IR** else 分支） | IR 走 `finalize_ir_from_units` |
| `vocab_table.build_vocab_table` 及整模块 (vocab_table.py:29) | 调用者 finalize_from_units / extract_all / 测试 | IR 显式跳过（main.py:285-288） |
| `sniff/profiler.profile_corpus` (profiler.py:14) | 仅 `cmd_sniff`(main.py:61) | pipeline/extract 用 `_phase0_from_grades` 派生，不调它 |
| `schema_unit` 非 compact 整段 (schema_unit.py:130-191) | 仅 `compact=False`；IR 恒 `compact=True` | 含下方拓扑/PII/fields 组装/occ 回填 |
| `_build_topology_folded`+`_expand_with_ancestors`+`_parent_path` (schema_unit.py:399/384/375) | 仅非 compact 拓扑(行132) | IR 用 `_compact_skeleton` 的 `depth=path.count(".")` 替代 |
| `_pii_for_field` (schema_unit.py:440) + `key_name_implies_pii`/`infer_pii_type`/`is_free_text_field` | 仅非 compact fields(行161) | compact 不产 pii_seed（信息五 IR 不落） |

---

## C 死代码（无任何生产调用者；test-only 或全无引用）

| 组件 | 证据 | 备注 |
|---|---|---|
| `parse/recovery.py` 整模块（strict_ok/tolerant_parse/_cpl_from_grade） | ✓ 仅 test_recovery.py import | Phase-4 双入口未接管线 |
| `extractor.extract_five_infos` (extractor.py:208) | ✓ 无任何调用者（生产+测试） | 文档称"测试仍用"不实 |
| `extractor.extract_all` (extractor.py:22) | ✓ 唯一调用者是已死的 extract_five_infos；无测试 | 与上构成死簇 |
| `vocabulary.py` 全部（build_vocabulary/vocab_stats） | 仅 test_vocabulary.py | schema_unit 注释明示"不依赖 build_vocabulary" |
| `topology.py` 全部（build_topology/_walk_topology） | 仅 test_topology.py | 生产用 schema_unit._build_topology_folded |
| `pii_seed.detect_pii_seeds` + `_collect_values_for_path` | 无任何调用者（含测试） | 生产只用同模块 key/infer/is_free_text |
| `skeleton.collect_skeletons` | 仅 test_skeleton.py | 生产直接用 structure_signature |
| `vocab_table.profile_similarity` | 仅 test_vocab_table.py | docstring 自承"占位" |
| `value_profile._make_pattern` | 仅 test_value_profile.py | profile_value 内联算 pattern |
| `sql_strict.scan_sql_file` | 仅 test_sql_strict.py | docstring 自承"仅供小文件/测试" |
| `text_utils.avg_line_len/column_stability/regex_search` | 全无引用（连测试都无） | column_stability 已被 column_profile 取代 |

---

## D 计算但丢弃（IR/compact 路径确执行、产物未落入返回的 SchemaUnit）

| 组件 | 证据 | 触发条件 |
|---|---|---|
| `skeleton_counts = dict(sig_counter.most_common(50))` (schema_unit.py:104) | 无条件算；compact 返回(行120-128)不含该键 | **所有 IR**（恒白算，轻量） |
| `value_profile` 统计：profile_value 的 char_dist/scripts(value_profile.py:171-180) + aggregate_profiles 的 len_dist/top_patterns/unique_patterns/avg_char_dist/avg_scripts(:281-307) | `_compact_skeleton` 只取 `vp["samples"]`(schema_unit.py:221-222)，其余全弃；仅 `pattern` 经 `_select_samples` 作去重键 | **IR + `--keep-samples`**（逐值重算，**最大算力浪费**） |
| `_walk_fold` 的 `dtype_seen` 填充 (schema_unit.py:97-100,267-273) | 仅 `_skeleton_from_dtype`(fold 模式)消费 | **IR + template(默认 field-mode)** |
| `_walk_fold` 的 `template_values` 填充 | compact 仅 sample_mode≠off 才用(schema_unit.py:218) | **IR 且无 `--keep-samples`** |
| JSON/JSONL occurrence：`UnionSchemaClusterer.occurrence`(skeleton.py:190)+`field_counts` + schema_partition `occ_map` | partition["occurrence"] 仅 schema_unit.py:136 读，**在 compact 返回(行120)之后** | **IR 且有 JSON/JSONL 单元**（CSV/SQL 语料下 occurrence 恒空，moot）。⚠ 同时文档/代码不符：guides §4.7 称"IR 已带 occurrence 真值"，实际 compact skeleton 不含 occurrence |

> 补充（非架构死代码，数据相关）：`render.py` 的 JSON/JSONL/嵌套分支与 `_realistic_value`
> 在"48 CSV + 2 SQL 全 flat"语料上不触发，但对 json/jsonl 单元可达且有测试，属"本语料未触达"。

---

## 可裁剪 / 可优化清单（按收益排序）

### 第一梯队 — 纯删 C（零生产影响，只需同删对应测试）
1. **`parse/recovery.py` 整模块** — LOC 最大；仅 test 依赖。⚠ 它是文档力推的"双入口"设计，
   删=放弃该 API 表面，先确认设计是否仍要保留。
2. **`vocabulary.py` + `topology.py` 整文件** — 各只有自身测试；最干净。
3. **`extractor.extract_all` + `extract_five_infos`** — 死簇，无测试，删除安全
   （不影响 `build_vocab_table`，后者另有非 IR 调用）。
4. **零引用碎片**：`pii_seed.detect_pii_seeds`(+`_collect_values_for_path`)、
   `text_utils.avg_line_len`/`column_stability`/`regex_search` — 连测试都没有，最安全。
5. **test-only 小函数**：`skeleton.collect_skeletons`、`vocab_table.profile_similarity`、
   `value_profile._make_pattern`、`sql_strict.scan_sql_file` — 删需连带改测试。

### 第二梯队 — D 类省算（GB 数据 IR 吞吐收益，改动须不碰非 IR 路径）
6. **IR+`--keep-samples` 下绕开 `aggregate_profiles` 全量统计**（最高算力收益）：compact 取样只需
   "按 pattern 去重选代表值"，可直接调 `_select_samples`（只依赖 profile_value 的 `pattern`），
   跳过 char_dist/scripts/len_dist/avg_* 的逐值与聚合计算。影响面：仅 compact 分支。
7. **compact 下 `_walk_fold` 条件化**：template 模式不填 `dtype_seen`；sample_mode=="off"
   不填 `template_values`（默认 `--ir` 无 keep-samples 时 `_walk_fold` 可省到只数签名）。
8. **`skeleton_counts` 在 compact 不物化**（schema_unit.py:104 移进非 compact 分支）— 收益小、零风险。
9. **occurrence 决断**：要么 compact 真把 `partition["occurrence"]` 写进 IR skeleton（兑现 guides §4.7），
   要么对 JSON/JSONL 跳过 `clusterer.occurrence()`/`field_counts` 计算。当前是"算了又丢 + 文档不符"。

### 文档层（与代码不符，建议同步）
- CLAUDE.md / guides.md 中 **`parsers/`**（已不存在）、**`extract_five_infos"测试仍用"`**、
  **`IR 带 occurrence`** 三处过时。
