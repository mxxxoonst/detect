# 容错解析改造:span 级 StructuredSegment | RawSegment 生成器设计

> 本文是**编码器输入构造层**的设计,服务于"结构退化条件下字段语义类型标注"任务的**双通道 / 压缩输入消融**;与现有 `grade_parse` / `partition_file` 的关系见正文。

---

## 一、现状读解:边界算出来了,却被当计数扔掉

当前管线把"哪里是结构、哪里是坏区"这条边界算了出来,却只用来累加整数计数,边界本身随即被丢弃。具体有两个关键事实。

### 1.1 parse 和 extract 是两遍独立读文件

- **parse 侧** `grade_parse`(`src/parse/` 下 `json_parser` / `csv_parser` / `sql_parser`)读一遍,只产出文件级的 C/P/L 计数 + `n_detail`。
- **extract 侧** `partition_file`(`src/extract/schema_partition.py`)又读一遍,产出 `SchemaPartition.record_iter`。

两遍口径靠**手工对齐**,极易漂移。已知 bug:`_partition_sql` 是**逐行**的,而 parse 侧 SQL 已走 `iter_sql_file_statements` 做**语句级**解析,两边口径不一致——值内换行 / 多行 INSERT / `),` 都会导致误报。

### 1.2 每个解析器已摸到"恢复区 / 坏区"边界,但只数不留

每个解析器在解析过程中其实都已经触达了恢复区与坏区的边界,但仅把它聚合成计数,边界与原文 span 被丢弃:

| 解析器 | 关键函数 | 已知信息 | 被丢弃的信息 |
| --- | --- | --- | --- |
| JSON | `_ijson_count_items` | `last_good_pos`(最后一个好元素的字节偏移)、`crashed` | 崩溃点之后的原文,以及每个 item 的 span |
| CSV | `_read_rows` | 每行 `raw_lens` / `core_lens`、`read_error`、众数 `width` | 行原文与偏移;且 `read_error` 一抛整个 reader 就停,后续行全丢 |
| SQL | `iter_sql_file_statements` | 每条 `st.text` / `.balanced` / `.terminated` / `.truncated` | 仅缺 span 偏移——最接近 span 级,改造最省 |

### 1.3 结论

`StructuredSegment | RawSegment` 的划分边界,就是现有 **C/P(进 seg1)对 L(进 seg2)** 那条线——它**已经被算出来了**,只是被聚合成了整数。

因此改造的本质 = 把这条线**物化**成"按文件顺序、带 span 和锚点的段流",让 parse 和 extract **共用同一个真源**。

---

## 二、统一真源:新增 `src/parse/segments.py`

引入一个单遍读文件、按文件位置顺序产出段流的统一入口。三个 TypedDict 类型放入 `schema_types.py`:

```python
from typing import Iterator, Literal, Optional, TypedDict, Union


class Span(TypedDict):
    start: int          # 字节偏移(含)
    end: int            # 字节偏移(不含)


class Anchor(TypedDict):
    kind: Literal["array_item", "row", "statement", "table", "root"]
    path: str           # 如 "$[]" / "table:users" / "col:email"
    parent_unit: str    # partition_id,聚簇键
    index: int          # 在父单元内的序号


class StructuredSegment(TypedDict):
    kind: Literal["structured"]
    span: Span
    anchor: Anchor
    surface: str        # 规范化恢复表面(canonicalized recovered surface)
    struct_tags: dict   # 结构标注(structure annotation)
    unit: dict          # 折叠字段→类型槽 = SchemaUnit 片段
    conf: float         # c_struct:C → 1.0,P → <1.0


class RawSegment(TypedDict):
    kind: Literal["raw"]
    span: Span
    anchor: Anchor
    text: str           # 坏区原文切片
    reason: Literal["truncated", "unbalanced", "col_overflow", "unterminated_string"]


Segment = Union[StructuredSegment, RawSegment]
```

统一入口:

```python
def iter_segments(path, fmt, enc) -> Iterator[Segment]:
    """按 fmt 路由,按文件位置顺序 yield,单遍读。"""
    ...
```

两侧消费方式:

- `grade_parse` 从段流累加 C/P/L:`conf == 1 → C`,`conf < 1 → P`,`Raw → L`。
- `partition_file` 把同 `anchor.parent_unit` 的 `StructuredSegment` 聚成 `SchemaPartition`。

一次读、两边消费,**口径不再漂**。

---

## 三、规范化恢复表面(canonicalized recovered surface)定义

seg1 的 `surface` **不是**抽象 IR `[(path, dtype)]`,而是把恢复出的结构**重渲染**成"保留语法骨架、抹掉值内容"的规范串。三条规则:

1. **保留所有承载结构的标点与分隔符**:`{ } [ ] ( ) : , "` 及分隔符——**形式层噪声活在这里**,必须留给编码器看见。
2. **值内容替换为类型槽**:`<str>` / `<int>` / `<float>` / `<bool>` / `<null>`,守 PII 红线,不落原值。
3. **归一非结构性表面噪声**:连续空白 → 单空格、去 BOM / 编码伪字符、记录内键序规范化。

示例:

| 输入 | 规范化恢复表面 | 说明 |
| --- | --- | --- |
| JSON `{"name":"Alice","age":30}` | `{"name":<str>,"age":<int>}` | 结构标点全保留,值入槽 |
| CSV 行 `Alice,30,NYC` | `<str>,<int>,<str>` | 分隔符保留 |
| CSV 漂移行 `Ali,ce,30,NYC`(4 列 / 众数 3) | `<str>,<str>,<int>,<str>` | **多出的逗号留着**,漂移可见 |
| SQL `INSERT INTO users VALUES (1,'ok')` | `INSERT INTO <tbl> VALUES (<int>,<str>)` | 关键字与括号保留 |

**对比**:当前 `SchemaUnit` 的 `[("name","str"),("age","int")]` 把括号 / 冒号 / 逗号全丢了——这正是规范化恢复表面要补回的信息。

---

## 四、结构标注(structure annotation)定义

仿 **GraphCodeBERT** 的 "token + 边" 形式。

### (a)token 级标签序列(与 surface token 对齐)

- `role`:`open_bracket` | `close_bracket` | `delimiter` | `key` | `kv_sep` | `value_slot`
- `depth`:嵌套深度
- `field_path`:仅 `value_slot` 才非空
- `node_type`:`object` | `array` | `row` | `tuple` | `stmt`

### (b)边集

`edges[(i, j, etype)]`,`etype`:`contain` | `sibling` | `kv_bind` | `same_field`。

其中 **`same_field` 边是跨记录对齐(L_consist)的挂载点**。

### (c)形式层专属标签

synth 注噪时额外给 `damaged: [token_idx]` 当标签(推理时由 RAW 头预测)。

### 对齐示例:`{"name":<str>,"age":<int>}`

```text
idx  token   role           depth  field_path  node_type
 0   {       open_bracket    0      -           object
 1   "name"  key             1      -           object
 2   :       kv_sep          1      -           object
 3   <str>   value_slot      1      name        object
 4   ,       delimiter       1      -           object
 5   "age"   key             1      -           object
 6   :       kv_sep          1      -           object
 7   <int>   value_slot      1      age         object
 8   }       close_bracket   0      -           object

edges:
  (0, 8, contain)     # { 容纳 }
  (1, 3, kv_bind)     # "name" 绑定 <str>
  (5, 7, kv_bind)     # "age"  绑定 <int>
  (1, 5, sibling)     # 同级键
```

---

## 五、锚点挂载(anchor mounting)定义

恢复过程**边走边建结构树** `root → 容器 → 记录 → 字段`,维护一个**结构上下文栈**单遍扫:

- **恢复成功区域**:入树,作当前容器的子节点,`anchor = {kind, path, parent_unit, index}`。
- **救不回区域(L)**:`anchor = 栈顶最近可靠容器 + 断点位置`。坏区内容虽不可解析,但其**结构邻域被锚住**,这正是 seg2 的 **re-anchor / boundary 头**的监督来源。

### 示例 1:JSON(数组中间元素截断)

```text
[
  {"id":1,"name":"a"},        # idx 0  → Structured, anchor $[0]
  {"id":2,"na                 # idx 1  → 截断坏区
  {"id":3,"name":"c"}         # idx 2  → Structured, anchor $[2]
]

RawSegment.anchor = {
  kind: "array_item",
  path: "$[]",                # 锚在数组元素位,夹在 idx0 / idx2 之间
  parent_unit: "<file>#root",
  index: 1,
}
```

### 示例 2:SQL(坏 INSERT)

```text
INSERT INTO users VALUES (1,'ok');   # Structured, anchor table:users
INSERT INTO users VALUES (2,'unter   # 截断坏区 → Raw

RawSegment.anchor = {
  kind: "table",
  path: "table:users",        # 锚到 users 表
  parent_unit: "<file>#users",
  index: 1,
}
```

---

## 六、三个解析器逐个改造点(精确到函数 / 变量)

### 6.1 SQL(最省,先做)

`iter_sql_file_statements` 已逐条产 `st.*`。改造:

1. 在 `sql_strict` 的 statement 结构上加 `start` / `end` 字节偏移。
2. 新增 `_iter_sql_segments`,按现有 `parse_sql_text` 的 C/P/L 判定分流:
   - `balanced ∧ terminated ∧ in-scope` → **Structured**(regex 抽 table + cols,`conf = 1`)。
   - `¬balanced ∧ ¬truncated` → **Structured**(`conf < 1`)+ 尾部不可解析部分切 **Raw**。
   - `truncated` → **Raw**(`anchor = 表名`)。
3. 顺带修 `_partition_sql`:**废弃**逐行 `startswith("(")` / `findall(r"\((.*?)\)")` 那套,改为消费同一个 `iter_segments`——值内换行 / 多行 INSERT 误报一起消失。

### 6.2 JSON

`_ijson_count_items` 已有 `tracker.pos` / `last_good_pos` / `crashed`。改造:

1. 把 `ijson.items` 换成 `ijson.parse` 事件流,顶层 `start_map` / `end_map` 记每元素 span,完成的 map → **Structured**(`conf = 1`)。
2. 崩溃时,读 `[last_good_pos, EOF]` 原文,用 `stream_concatenated_json_records` 的逻辑尝试**重同步**(找下一个 `},\n{` 或顶层 `{`):同步成功则继续 **Structured**(`conf < 1`),中间不可同步段 → **Raw**(`anchor = $[]`)。
3. `_concatenated_recovery` / `tolerant_json_records` 同构处理。

### 6.3 CSV(改动最大,reader 一抛就停)

`_read_rows` 现在 `read_error` 一置位循环就结束。改造:

1. 先轻扫定众数 `width`。
2. 自己按物理行读、逐记录喂单行 `csv.reader`:
   - `_row_clean` 为真 → **Structured**(`conf = 1`)。
   - `raw ≠ width` 但可重对齐 → **Structured**(`conf < 1`)+ `struct_tag` 标 `col_overflow` / `underflow`。
   - 单行 reader 抛错 → **Raw**(带 `reason`,`anchor = table`),然后 `continue` 读下一行——**不让一行坏掉杀死整个文件**。
3. **xlsx 没有容错侧(二进制)**:每行 → Structured;读不了的 sheet 跳过、**不产 Raw**。此**非对称需在文档写明**。

---

## 七、落地顺序与边界

1. 先加类型 + `iter_segments` 骨架,**SQL 通道**接通(改动最小,且白赚 `_partition_sql` 修复)。
2. `grade_parse` 改为消费段流累加 C/P/L,产出的 `grades.jsonl` **完全不变**——用现有 5339 条做**回归对拍**,确保不破坏 C/P/L。
3. 再接 **JSON**,最后 **CSV**(CSV 逐行容错改动最大,留到最后)。
4. `partition_file` 改为消费 `StructuredSegment` 聚簇,parse / extract 两遍读**合并成一遍**。

---

## 八、待决问题

- 本设计服务于 **"双通道 vs 压缩成 SchemaUnit + RAW vs 纯 RAW 单通道"** 的消融实验臂。
- seg1 采用**规范化恢复表面(保留语法)而非抽象 IR**,是为了回应"模板化掉括号 / 分隔符导致信息不足"的质疑。
- **结构理解正确性的验证**(boundary / re-anchor / field-binding 等结构探针指标)是**开放项**,待与任务定义一起确定。
