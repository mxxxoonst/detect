"""一次性验证脚本: 在 test_data/DataPart (Q1) 和 csv_tests (Q2) 上对比
现行精确签名 vs 提议的鲁棒指纹。仅本地小样本分析, 不依赖远程数据。"""
import json, re, csv, sys, glob, os, hashlib
from collections import Counter, defaultdict
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.extract.skeleton import structure_signature

ROOT = os.path.dirname(os.path.abspath(__file__))

# ───────────────────────── Q1: JSON 记录级 ─────────────────────────
def strip_trailing_commas(txt):
    """字符串感知地去掉 `,` 后紧跟 ]/} 的尾逗号; 不误伤字符串内逗号。"""
    out = []
    in_str = False; esc = False
    n = len(txt)
    for k, ch in enumerate(txt):
        if in_str:
            out.append(ch)
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
            continue
        if ch == '"':
            in_str = True; out.append(ch); continue
        if ch == ",":
            j = k + 1
            while j < n and txt[j] in " \t\r\n": j += 1
            if j < n and txt[j] in "]}":
                continue   # 丢弃这个尾逗号
        out.append(ch)
    return "".join(out)


def load_records(fn):
    """鲁棒加载: 优先 ijson 流式数组(与流水线一致); 失败再 raw_decode 逐顶层值
    (容忍 JSONL / 拼接多个顶层值)。不对原文做正则改写, 避免误伤字符串内逗号。"""
    recs = []
    try:
        import ijson
        with open(fn, "rb") as f:
            for item in ijson.items(f, "item"):
                recs.append(item)
        if recs:
            return recs
    except Exception:
        recs = []
    # 非数组 / ijson 失败: 逐元素容错恢复(模拟流水线 ijson 部分恢复)
    txt = strip_trailing_commas(open(fn, encoding="utf-8").read())
    dec = json.JSONDecoder()
    n = len(txt)
    ws = re.compile(r"[\s,\[\]]*")        # 跳过空白/逗号/数组括号
    i = ws.match(txt, 0).end()
    while i < n:
        try:
            obj, j = dec.raw_decode(txt, i)
        except json.JSONDecodeError:
            break                          # 到达损坏尾部, 保留已恢复部分
        if isinstance(obj, list): recs.extend(obj)
        else: recs.append(obj)
        i = ws.match(txt, j).end()
    return recs

def norm_type(v):
    if v is None: return None
    if isinstance(v, bool): return "bool"
    if isinstance(v, (int, float)): return "num"
    if isinstance(v, str): return "str"
    if isinstance(v, dict): return "obj"
    if isinstance(v, list): return "arr"
    return "other"

def leaf_types(v, prefix, out):
    if isinstance(v, dict):
        if not v: return
        for k in sorted(v): leaf_types(v[k], prefix + "." + k, out)
    elif isinstance(v, list):
        if not v: return
        for e in v: leaf_types(e, prefix + "[]", out)
    else:
        t = norm_type(v)
        if t is not None: out.setdefault(prefix, set()).add(t)

def rec_paths(r):
    o = {}; leaf_types(r, "", o); return o

def compatible(proto, pr):
    for p, ts in pr.items():
        if p in proto and proto[p].isdisjoint(ts):
            return False
    return True

def merge_cluster(records):
    protos = []          # list[dict path->typeset]
    sizes = []
    for r in records:
        pr = rec_paths(r)
        for i, proto in enumerate(protos):
            if compatible(proto, pr):
                for p, ts in pr.items(): proto.setdefault(p, set()).update(ts)
                sizes[i] += 1
                break
        else:
            protos.append({p: set(t) for p, t in pr.items()}); sizes.append(1)
    return protos, sizes

print("=" * 70)
print("Q1  JSON 分片: 现行精确签名  vs  兼容性合并(union schema)")
print("=" * 70)
for fn in sorted(glob.glob(os.path.join(ROOT, "DataPart", "*.json"))):
    recs = load_records(fn)
    cur = len(Counter(structure_signature(r) for r in recs))
    protos, sizes = merge_cluster(recs)
    print(f"  {os.path.basename(fn):14s} records={len(recs):6d}  "
          f"现行签名分片={cur:5d}  ->  合并分片={len(protos):3d}  "
          f"(各簇记录数 {sorted(sizes, reverse=True)[:6]}{'...' if len(sizes) > 6 else ''})")

# ───────────────────────── Q2: CSV 文件级 ─────────────────────────
def sniff_sep(path):
    cand = [",", ";", "|", "\t"]; best, bestsd = ",", 1e9
    from statistics import pstdev
    for s in cand:
        cc = []
        for i, line in enumerate(open(path, encoding="utf-8", errors="replace")):
            if i >= 20: break
            if line.strip(): cc.append(line.count(s) + 1)
        if len(cc) >= 2 and cc[0] >= 2:
            sd = pstdev(cc)
            if sd < bestsd: bestsd, best = sd, s
    return best

def is_pure_num(s):
    return bool(re.fullmatch(r"[-+]?\d+(\.\d+)?", s.strip()))

def cell_class(s):
    s = s.strip()
    if s == "": return "e"          # empty
    if is_pure_num(s): return "n"   # numeric
    if "@" in s and "." in s: return "@"   # email-ish
    if re.fullmatch(r"[\d\s()+\-]{6,}", s): return "p"  # phone-ish
    return "s"                      # string

def has_header(rows, sep):
    if len(rows) < 2: return False
    r0 = rows[0].split(sep); r1 = rows[1].split(sep)
    if len(r0) != len(r1): return False
    # 表头: 第0行无纯数字, 但数据行至少一列同位置是纯数字
    r0_anynum = any(is_pure_num(c) for c in r0)
    r1_anynum = any(is_pure_num(c) for c in r1)
    return (not r0_anynum) and r1_anynum

def read_rows(path, sep, limit=60):
    """quote 感知地读前 limit 行 (用 csv.reader, 尊重引号内分隔符)。"""
    rows = []
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        for i, row in enumerate(csv.reader(f, delimiter=sep)):
            if row and any(c.strip() for c in row): rows.append(row)
            if len(rows) >= limit: break
    return rows

def csv_fingerprint(path):
    sep = sniff_sep(path)
    rows = read_rows(path, sep)
    if not rows: return ("empty",), sep, False
    flat = [sep.join(r) for r in rows]
    hdr = has_header(flat, sep)
    if hdr:
        header = [c.strip().strip('"').lower() for c in rows[0]]
        return ("H", tuple(header)), sep, True
    # headerless: 列数 + 每列"非空类型集"(空格当通配, 不参与签名) —— 与 Q1 同一归一化
    ncols = Counter(len(r) for r in rows).most_common(1)[0][0]
    colsets = []
    for j in range(ncols):
        s = set()
        for r in rows:
            if len(r) == ncols and j < len(r):
                c = cell_class(r[j])
                if c != "e": s.add(c)          # 空 → 不携带 schema 信息
        colsets.append(s)
    return ("V", ncols, tuple(frozenset(s) for s in colsets)), sep, False

def v_compatible(a, b):
    """两个 headerless 指纹是否兼容: 同列数, 每列非空类型集不冲突(允许一方为空集)。"""
    if a[0] != "V" or b[0] != "V" or a[1] != b[1]: return False
    for sa, sb in zip(a[2], b[2]):
        if sa and sb and sa.isdisjoint(sb): return False
    return True

print()
print("=" * 70)
print("Q2  CSV schema 去重: 60 个文件 -> 按指纹聚类")
print("=" * 70)
prints = []
for path in sorted(glob.glob(os.path.join(ROOT, "..", "csv_tests", "*.csv"))):
    fp, sep, hdr = csv_fingerprint(path)
    prints.append((os.path.basename(path), fp))

# 先按精确指纹分桶, 再把兼容的 headerless 桶合并(空格通配) —— 与 Q1 同一思想
exact = defaultdict(list)
for name, fp in prints: exact[fp].append(name)

merged = []   # list[ (representative_fp, [files]) ]
for fp, files in exact.items():
    placed = False
    if fp[0] == "V":
        for m in merged:
            if v_compatible(m[0], fp):
                # 取列数类型集的并作为新代表
                newcols = tuple(frozenset(a | b) for a, b in zip(m[0][2], fp[2]))
                m[0] = ("V", fp[1], newcols); m[1].extend(files); placed = True; break
    if not placed:
        merged.append([fp, list(files)])

clusters = {tuple(["#%d" % i]): files for i, (fp, files) in enumerate(merged)}
fp_of = {tuple(["#%d" % i]): merged[i][0] for i in range(len(merged))}

print(f"  文件总数 = {sum(len(v) for v in clusters.values())}   "
      f"精确指纹桶 = {len(exact)}   ->   兼容合并后 distinct schema = {len(merged)}\n")
for key, files in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
    fp = fp_of[key]
    tag = fp[0]
    if tag == "H":
        desc = "HEADER " + ",".join(fp[1][:6]) + ("..." if len(fp[1]) > 6 else "")
    elif tag == "V":
        valsig = "".join("".join(sorted(s)) if s else "*" for s in fp[2])
        desc = f"NOHEADER cols={fp[1]} valsig={valsig[:40]}{'...' if len(valsig)>40 else ''}"
    else:
        desc = str(fp)
    rep = files[0] + (f"  (+{len(files)-1} 重复)" if len(files) > 1 else "")
    print(f"  x{len(files):2d}  代表={rep}")
    print(f"        {desc}")
    if len(files) > 1:
        print(f"        全部: {files}")
