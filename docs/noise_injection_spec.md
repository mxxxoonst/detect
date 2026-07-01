# 注噪与 oracle 标签生成规格(encoder-only 抽取式)

> **定位**:管"训练三元组怎么造"——已有的**干净扩充语料** → `带噪文本 + oracle 对齐 + noise-meta` → **4 头训练标签**。
> 与 [`span_segments_design.md`](./span_segments_design.md)(观测侧:容错解析产段)、
> [`encoder_pretraining_design.md`](./encoder_pretraining_design.md)(任务/头/损失)配套。
> 本文只讲**数据生成**,不重复段流机制。

---

## 〇、总原则:两条线永不交叉

| 线 | 是什么 | 来源 | 性质 |
|---|---|---|---|
| **观测线**(输入 `x̂`) | 容错解析器对**带噪文本**产的段流(`span_segments_design.md`) | 带噪文本 | **可错** |
| **标签线**(oracle `y`) | 从**干净对齐 + 注噪 edit_map** 派生 | 干净语料 + 注噪记录 | **永远正确** |

**二者从不混**:容错解析产"观测"、synth 产"标签"。下面所有标签都走标签线,与容错解析器无关。

---

## 一、`clean_align`:对已有干净语料做**精确 span 解析**(**不改 render**)

> 更正早期方案:**不在 render 写值时吐 provenance**。干净扩充语料**已渲染在盘**,render 保持纯渲染。

**分工提醒**:此处属**标签线**——干净合成文件**必然严格可解**,走**严格内核**(exact span 解析,全 `StructuredSegment`、`conf=1`、grouping 零歧义)拿 oracle。**容错解析器只面向带噪样本(观测线),不在此**;二者永不交叉。得到 oracle 结构真值:

```python
# clean_align:干净文件每个字符区间 → (记录, 字段) + 类型/值
CleanAlign = dict[Span, FieldRef]      # Span=(start,end)  FieldRef=(rec_id, field_id, type_tag)

def build_clean_align(clean_path, fmt, enc) -> CleanAlign:
    align = {}
    for seg in iter_segments(clean_path, fmt, enc):   # 干净文件 → 全 Structured
        for tok in seg["tokens"]:
            if tok["role"] == "value_slot":
                align[tok["span"]] = (seg["tentative_record"],   # 干净下=真实记录
                                      tok["tentative_field"],    # 干净下=真实字段
                                      tok["type_tag"])
    return align
```

- **零额外标注、零 render 改动**:复用同一套 `iter_segments`,只是喂干净文件。
- 干净文件里 `tentative_*` 不再"试探"——因为无噪声,它就是真值。

---

## 二、`inject`:注噪 = 维护 `edit_map` 的文本变换

注噪对**干净文本**做字符级增删改,同时记录**带噪位置 → 干净位置**的映射:

```python
def inject(clean_text: str) -> tuple[str, EditMap, list[NoiseMeta]]:
    # EditMap: 带噪每个字符 → 干净来源字符下标 | INSERTED
    ...

# oracle 对齐(带噪坐标系)= edit_map ∘ clean_align
def compose(edit_map, clean_align) -> dict[int, FieldRef | Literal["INSERTED", "RAW"]]:
    oracle = {}
    for npos, src in edit_map.items():
        if src == "INSERTED":
            oracle[npos] = "INSERTED"          # 注入的假分隔符等 → 特判(damaged/非边界)
        else:
            oracle[npos] = lookup(clean_align, src)   # 回溯干净来源 → (rec, field, type)
    return oracle
```

**关键**:`inject` 自己在做编辑,天然知道 `edit_map`,**无需 render 配合**。`oracle_align_noised = compose(edit_map, clean_align)` 就是带噪坐标系下的字段真值,**全部标签的唯一来源**。

---

## 三、观测表面(值 + 类型**同时保留**)

带噪文本喂**容错解析器**产观测段流,`ObsToken` 结构见 `span_segments_design.md` §二/§三(已更新):

- **值 token 原样保留**(合成值不敏感、不违反 PII 红线),类型在 `type_tag` **并行**标注,**不替换**。
- `tentative_field/tentative_record` 是解析器**可错**的试探归组,作输入特征(可 dropout),**非 gold**。

> 红线约束"落盘持久化",不约束"编码器瞬态消费";真实管线侧落盘另按 CLAUDE.md 脱敏。

---

## 四、算子 → 改干净文本 × `edit_map` × `noise_meta`(形式层 + 结构层)

| 层 | 算子 | 对干净文本的操作 | `edit_map` 效果 | `noise_meta` |
|---|---|---|---|---|
| 形式 | **截断** | 某记录随机字节处切断,尾部删 | 尾部字符消失 | `{op:trunc, layer:form, θ:切比, span:(cut,eof)}` |
| 形式 | **未闭合** | 删一个配对闭合 `}`/`)`/`"` | 该字符消失 | `{op:unclosed, layer:form, tok, pos}` |
| 形式 | **未转义分隔符** | 往某值内插裸分隔符 | 该位 `INSERTED` | `{op:unesc_delim, layer:form, pos, ch}` |
| 结构 | **列漂移** | 增/删一个分隔符 | 增→`INSERTED` / 删→消失 | `{op:col_drift, layer:struct, dir:over/under, pos}` |
| 结构 | **嵌套错位** | 把 key-value 移到错误深度(仍可解析) | 字符搬移,src 指向原位 | `{op:nest_mis, layer:struct, key, from_d, to_d}` |
| 结构 | **记录边界混入** | 值内插换行(裂)/删记录分隔(并) | 插→`INSERTED` / 删→消失 | `{op:rec_mix, layer:struct, type:split/merge, pos}` |
| 结构 | **表头坍塌** | 删表头行 / 用数据行冒充表头 | 表头字符消失 / 复制 | `{op:hdr_collapse, layer:struct, type:drop/promote}` |

> 语义层算子(砸字段名、保 `y`)归**模块 B** 的训练,不在此表(见 `encoder_pretraining_design.md` A/B 划分)。

---

## 五、4 头训练标签,统一从 `oracle_align` 派生(纯函数)

```python
def labels_from_align(noised_tokens, oracle_align, raw_segments, clean_view):
    # 1) boundary:(rec,field) 变化处=真边界;INSERTED 的假分隔符 → 标"非边界"+damaged
    boundary = [is_true_boundary(oracle_align, tok) for tok in noised_tokens]
    damaged  = [oracle_align[tok.pos] == "INSERTED" for tok in noised_tokens]

    # 2) field-binding:每 value token → 其 oracle 字段 id(漂移/错位下仍是真字段)
    binding = [oracle_align[tok.pos].field_id
               for tok in noised_tokens if tok.role == "value_slot"]

    # 3) re-anchor:每个 seg2 RAW 段 → 其 oracle 容器(截断/未闭合产生的坏块)
    reanchor = [oracle_container(oracle_align, seg.span) for seg in raw_segments]

    # 4) consistency:干净视图 ↔ 带噪视图,按 oracle 把"同一字段"配对 → 对比目标
    consist_pairs = pair_same_field(clean_view, noised_tokens, oracle_align)

    return boundary, damaged, binding, reanchor, consist_pairs
```

| 头 | 标签 | 派生规则 |
|---|---|---|
| **boundary** | 逐 token 是否真边界 + `damaged` | oracle_align 中 (rec,field) 跳变处=边界;`INSERTED` 位=非边界+damaged |
| **field-binding** | value token → 真字段 id | 直接查 `oracle_align[pos].field_id`(不受观测漂移影响) |
| **re-anchor** | RAW 段 → 真容器 | 坏块 span 内 oracle 的公共父节点 |
| **consistency** | (干净字段, 带噪字段) 正对 | 按 oracle 字段身份把两视图同字段配对 |

---

## 六、端到端例子:未转义分隔符

```
干净文件(已在盘):  ...,"New York",...          city 值 = "New York"
① build_clean_align: span("New York") → (rec=2, field=city, ⟨str⟩)

② inject 插入逗号:   "New York" → "New,York"
   带噪文本:         Alice,30,New,York
   edit_map:         'New' 各字符→干净源;插入的 ',' → INSERTED;'York' 各字符→干净源
   noise_meta:       {op:unesc_delim, layer:form, pos=…, ch:','}

③ oracle_align(带噪坐标): Alice→(2,name) 30→(2,age) New→(2,city) [,]→INSERTED York→(2,city)

④ 容错解析(观测): 数出 4 列,York tentative_field 可能=溢出(错)——仅作输入,不当标签

⑤ labels_from_align:
   boundary:  Alice|30|New 后=真边界;New 与 York 间那个逗号 → 非边界 + damaged=True
   binding:   New→city, York→city（都绑 city，尽管中间有逗号）
   re-anchor: (无 seg2)
   consist:   干净"New York"字段 ↔ 带噪"New,York"字段 配对拉齐
```

模型被逼学到:**这个逗号是注入的假分隔符,New/York 同属 city**——而观测解析器恰恰会在这里犯错。

---

## 七、代码落地点(不动 render)

1. **`build_clean_align()`**:复用 `iter_segments` 于**干净文件**,抽 `{span → (rec,field,type)}`。新增,无 render 改动。
2. **`src/synth/noise/`**(新模块):`inject()` + 各算子(改文本 + 记 `edit_map` + `noise_meta`)+ `compose()`。
3. **`labels_from_align()`**:纯函数,从 `oracle_align` 派生 4 头标签,可单测(给定小例断言标签)。
4. **训练取数**:带噪文本 →(容错解析,`span_segments`)→ 观测 seg1/seg2 喂 A;标签走 `oracle_align`。**观测线与标签线在代码里也分属两个函数,永不交叉。**

---

## 八、待决

- `θ_max` 结构标签保持界的**逐算子**具体取值(注噪后 oracle 仍须可定义)。
- 跨层复合注噪的 `edit_map` 叠加顺序(语义→结构→形式)与位点不相交采样(见 `encoder_pretraining_design.md` §3 跨层 DoE)。
- `nest_mis`(字符搬移)的 `edit_map` 表示:src 指针需支持"非连续搬移",实现时注意。
