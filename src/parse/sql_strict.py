"""SQL 超集分句器 `scan_sql`: 方言无关的流式状态机。

把 SQL 文本切成语句边界, 同时报告每条是否「平衡且完整」。**流式逐块扫到 EOF**,
不读全文件、不止 64KB 头 (GB 量级 SQL dump 为硬性要求)。

这是 parse 阶段严格/容错解析的**共享内核**:
- 严格模式 (recovery=OFF): `scan_sql` 全程零截断、所有语句平衡且可归类 ⟹ strict_ok。
- 容错模式 (recovery=ON):  在 `scan_sql` 切出的语句边界上跑 regex 抽 schema, 抽出计 P, 抽不出计 L。

二者基于同一遍状态机扫描, 故 `strict_ok(x) ⟺ tolerant(x) 零截断` 恒成立
(退化曲线起点 I(x_clean)=1 的命门, 见 docs/parser_strict_tolerant_design.md §0.3)。

PII 红线: 本模块只在内存里短暂持有当前语句文本以判平衡/供上层抽结构事实,
不持久化任何 SQL 语句原文; 上层抽出表名/列名后即丢弃。
"""

from typing import Iterator, NamedTuple, Optional

from src.utils.logger import get_logger

log = get_logger(__name__)


# ── 状态常量 ──────────────────────────────────────────────────────────────
NORMAL = "NORMAL"
SQUOTE = "SQUOTE"          # '...'  单引号字符串
DQUOTE = "DQUOTE"          # "..."  双引号 (标识符或字符串, 方言相关)
BTICK = "BTICK"            # `...`  反引号标识符 (MySQL)
LINE_CMT = "LINE_CMT"      # -- ... 行注释直到 \n
BLOCK_CMT = "BLOCK_CMT"    # /* ... */ 块注释
DOLLAR = "DOLLAR"          # $tag$ ... $tag$ dollar-quote (postgres)

# 缓冲上限: 防御异常长 (或因未闭合而吞掉整篇的) 语句。超限即强制切断并记 truncated。
MAX_STMT_BUF = 1 << 20     # 1 MiB

# 流式读块大小。
_CHUNK = 1 << 16           # 64 KiB / 次, 但会一直读到 EOF


class SqlStatement(NamedTuple):
    """一条被切出的 SQL 语句 (内存态, 不落盘)。

    text:       语句文本 (含尾部分隔符前的内容; 供上层 regex 抽 schema)。
    terminated: 是否以 NORMAL/depth==0 处的 ';' 正常收尾。
    balanced:   引号/注释/dollar-quote/括号在语句结束时是否全部闭合 (depth==0 且回到 NORMAL)。
    truncated:  该语句是否为截断尾句 (EOF 时仍在非 NORMAL 状态 / depth>0 / 无分隔符正常收尾,
                或因 MAX_STMT_BUF 超限被强切)。
    """
    text: str
    terminated: bool
    balanced: bool
    truncated: bool


class ScanResult(NamedTuple):
    """`scan_sql_file` 的逐文件汇总 (不含任何 SQL 原文)。

    C/P/L:    语句级计数。严格内核里 C = 平衡且可归类的语句; P = 容错抽取救回的语句;
              L = 截断/不可归类语句。本模块只产 scan 层事实 (n_total/n_balanced/n_truncated);
              C/P/L 的最终归账由上层 sql_parser 按抽取结果填 (scan 提供 strict 侧 C 估计)。
    n_total:      语句总数 N。
    n_balanced:   平衡且正常收尾的语句数 (严格侧 C 候选)。
    n_truncated:  截断/未闭合语句数 (落 L)。
    strict_ok:    严格判定: 全程零截断、所有语句平衡且正常收尾 ⟹ True (clean ⟺ I_strict==1)。
    n_form:       形式层破坏标记 (truncated / unbalanced / None)。供噪声分布聚类, 结构化不落原文。
    detail:       结构化细节 (各状态计数等), 供 n_detail 落 grades.jsonl。
    """
    n_total: int
    n_balanced: int
    n_truncated: int
    strict_ok: bool
    n_form: Optional[str]
    detail: dict


def scan_sql_statements(stream: Iterator[str]) -> Iterator[SqlStatement]:
    """流式状态机: 消费字符流, 逐条 yield 切出的 SqlStatement。

    Args:
        stream: 产出文本块 (str) 的可迭代对象 (逐块, 内部按字符推进)。

    Yields:
        SqlStatement: 每遇到 NORMAL/depth==0 的 ';' 切一条; EOF 时若缓冲非空再 yield 尾句
        (并据状态判 truncated)。
    """
    state = NORMAL
    depth = 0                     # 括号嵌套深度 (仅 NORMAL 下计)
    dollar_tag = ""               # 当前 dollar-quote 的 tag, 形如 "$tag$" / "$$"
    buf: list[str] = []           # 当前语句字符缓冲 (内存态)
    buf_len = 0

    # 把字符流摊平成单字符迭代器, 但保留 O(1) 前瞻能力 (需要看下一个字符判 '--' '/*' '*/' '$tag$')。
    chars = _iter_chars(stream)
    pending: Optional[str] = None  # 前瞻回退槽

    def _next() -> Optional[str]:
        nonlocal pending
        if pending is not None:
            c, pending = pending, None
            return c
        return next(chars, None)

    def _peek() -> Optional[str]:
        nonlocal pending
        if pending is None:
            pending = next(chars, None)
        return pending

    def _flush(terminated: bool) -> SqlStatement:
        nonlocal buf, buf_len
        text = "".join(buf)
        buf = []
        buf_len = 0
        balanced = (state == NORMAL and depth == 0)
        # 未以 NORMAL/depth==0 处 ';' 正常收尾的语句一律判截断 (含「平衡但缺尾分号」)。
        # 设计 §3: 「末句无分隔符正常结束 => 该(尾)语句判为截断 => 计 L」(保守 fail-closed)。
        truncated = not terminated
        return SqlStatement(text=text, terminated=terminated,
                            balanced=balanced, truncated=truncated)

    def _push(c: str) -> bool:
        """把字符压入缓冲; 返回是否触发 MAX_STMT_BUF 超限。"""
        nonlocal buf_len
        buf.append(c)
        buf_len += 1
        return buf_len >= MAX_STMT_BUF

    while True:
        c = _next()
        if c is None:
            break

        # MAX_STMT_BUF 防御: 超长语句 (常因未闭合吞掉后文) 强切, 判截断。
        if _push(c):
            log.debug("scan_sql: 语句缓冲超过 MAX_STMT_BUF=%d, 强切并判截断", MAX_STMT_BUF)
            # 强切: 当前状态未归零即 unbalanced/truncated。
            text = "".join(buf)
            buf.clear()
            buf_len = 0
            balanced = (state == NORMAL and depth == 0)
            yield SqlStatement(text=text, terminated=False,
                               balanced=balanced, truncated=True)
            # 缓冲清空后继续扫 (状态保持, 让后续字符尽量归到下一句)。
            continue

        if state == NORMAL:
            if c == "'":
                state = SQUOTE
            elif c == '"':
                state = DQUOTE
            elif c == "`":
                state = BTICK
            elif c == "-":
                if _peek() == "-":
                    _push(_next())          # 吃掉第二个 '-'
                    state = LINE_CMT
            elif c == "/":
                if _peek() == "*":
                    _push(_next())          # 吃掉 '*'
                    state = BLOCK_CMT
            elif c == "$":
                tag = _read_dollar_tag(_peek, _next, _push)
                if tag is not None:
                    dollar_tag = tag
                    state = DOLLAR
                # tag is None: 不是合法 dollar-quote 开头 (如 $1 占位符), 留在 NORMAL
            elif c == "(":
                depth += 1
            elif c == ")":
                if depth > 0:
                    depth -= 1
            elif c == ";":
                if depth == 0:
                    yield _flush(terminated=True)
            # 其余字符无状态影响

        elif state == SQUOTE:
            if c == "\\":
                # 反斜杠转义 (方言相关, 如 MySQL): 吞掉下一个字符, 不退出串。
                nxt = _next()
                if nxt is not None:
                    _push(nxt)
            elif c == "'":
                if _peek() == "'":
                    _push(_next())          # '' 转义: 留在串内
                else:
                    state = NORMAL

        elif state == DQUOTE:
            if c == "\\":
                nxt = _next()
                if nxt is not None:
                    _push(nxt)
            elif c == '"':
                if _peek() == '"':
                    _push(_next())          # "" 转义
                else:
                    state = NORMAL

        elif state == BTICK:
            if c == "`":
                if _peek() == "`":
                    _push(_next())          # `` 转义 (MySQL 标识符内反引号)
                else:
                    state = NORMAL

        elif state == LINE_CMT:
            if c == "\n":
                state = NORMAL

        elif state == BLOCK_CMT:
            if c == "*":
                if _peek() == "/":
                    _push(_next())          # 吃掉 '/'
                    state = NORMAL

        elif state == DOLLAR:
            if c == "$":
                # 试着匹配收尾 tag。
                closing = _try_match_dollar_close(dollar_tag, _peek, _next, _push)
                if closing:
                    state = NORMAL
                    dollar_tag = ""

    # EOF: 若缓冲仍有非空白内容, 作为尾句 yield。
    if "".join(buf).strip():
        # 未以 ';' 正常收尾 → terminated=False; balanced 视状态而定。
        yield _flush(terminated=False)


def _iter_chars(stream: Iterator[str]) -> Iterator[str]:
    """把文本块流摊平成单字符流 (常数内存; 逐块读到 EOF)。"""
    for chunk in stream:
        for ch in chunk:
            yield ch


def _read_dollar_tag(peek, nxt, push) -> Optional[str]:
    """已消费一个 '$', 试读 dollar-quote 开标签 `$tag$` (tag 为 [A-Za-z_]\\w* 或空)。

    成功: 把构成 tag 的字符 (含收尾 '$') 推进缓冲, 返回完整标签串 (如 "$$"/"$tag$")。
    失败 (非合法 dollar-quote, 如 `$1 ` / `$tag<EOF>`): 返回 None, 留在 NORMAL。
        已读的标识符字符已 push 进语句缓冲作普通文本 (内容保留), 仅不开 dollar-quote;
        作为开标签的 '$' 后续字符若非 `[A-Za-z0-9_]` 则不消费 (留给主循环)。
    """
    tag_chars: list[str] = []
    # 先看是否立即是收尾 '$' → "$$"
    p = peek()
    if p == "$":
        push(nxt())                          # 消费收尾 '$'
        return "$$"
    # 读 tag 标识符部分
    while True:
        p = peek()
        if p is None:
            # EOF 前未见收尾 '$' → 非法 dollar-quote。已消费的 tag_chars 已 push 进缓冲
            # 作普通文本 (内容保留), 不开 dollar-quote, 留在 NORMAL。
            return None
        if p == "$":
            push(nxt())                      # 消费收尾 '$'
            return "$" + "".join(tag_chars) + "$"
        if p.isalnum() or p == "_":
            tag_chars.append(nxt())          # 消费 tag 字符
            push(tag_chars[-1])
        else:
            # 非法字符 (如 '$1' 的 '1' 其实合法, 但 '$ ' 的空格非法) → 非 dollar-quote。
            return None


def _try_match_dollar_close(tag: str, peek, nxt, push) -> bool:
    """已消费 DOLLAR 状态下的一个 '$', 试匹配收尾 tag (tag 形如 "$$" / "$name$")。

    tag 的内部 = tag[1:-1] (去掉首尾 '$')。我们刚消费了首 '$', 需匹配 内部 + 收尾 '$'。
    成功: 消费掉匹配字符并 push, 返回 True。失败: 不消费, 返回 False (该 '$' 留作普通字符)。
    """
    inner = tag[1:-1]                         # "" for "$$"
    if inner == "":
        # "$$": 刚消费的 '$' 后需紧跟一个 '$'
        if peek() == "$":
            push(nxt())
            return True
        return False
    # "$name$": 逐字符比对 inner, 再要一个收尾 '$'
    consumed: list[str] = []
    for want in inner:
        if peek() == want:
            consumed.append(nxt())
            push(consumed[-1])
        else:
            # 不匹配。已消费的字符无法干净回退到流里, 但它们本就属于 dollar 体,
            # push 进缓冲是正确的 (内容保留), 仅未闭合 → 留在 DOLLAR 状态。
            return False
    if peek() == "$":
        push(nxt())
        return True
    return False


def _file_chunks(path: str, encoding: str) -> Iterator[str]:
    """把 SQL 文件按 _CHUNK 流式 yield 文本块 (常数内存, 读到 EOF)。"""
    with open(path, "r", encoding=encoding, errors="replace") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            yield chunk


def iter_sql_file_statements(path: str, encoding: str) -> Iterator[SqlStatement]:
    """**惰性** 逐条 yield 一个 SQL 文件切出的语句 (常数内存, 不物化全部)。

    GB 级 SQL dump 为硬性要求: 上层应惰性消费本迭代器、只累加 C/P/L + 有界样本,
    **绝不 `list()` 物化全部语句** (否则数百万语句的文本会撑爆内存)。
    """
    yield from scan_sql_statements(_file_chunks(path, encoding))


def scan_sql_file(path: str, encoding: str) -> tuple[list[SqlStatement], ScanResult]:
    """流式扫一个 SQL 文件, 返回 (语句列表, 汇总)。

    ⚠ 物化全部语句到 list, **仅供小文件/测试** (GB 文件请用 `iter_sql_file_statements`
    惰性消费, 见 parse_sql_text)。

    严格判定: strict_ok ⟺ 至少 1 条语句 ∧ 零截断 ∧ 所有语句平衡且正常收尾。
    """
    statements = list(iter_sql_file_statements(path, encoding))
    return statements, summarize(statements)


def summarize(statements: list[SqlStatement]) -> ScanResult:
    """从语句列表汇总 ScanResult (纯计数, 不持有原文)。"""
    n_total = len(statements)
    n_truncated = sum(1 for s in statements if s.truncated)
    n_unbalanced = sum(1 for s in statements if not s.balanced)
    # 平衡且正常收尾 (严格 C 候选)。
    n_balanced = sum(1 for s in statements if s.balanced and s.terminated)

    strict_ok = (n_total > 0 and n_truncated == 0 and n_balanced == n_total)

    if n_truncated > 0:
        n_form = "truncated"
    elif n_unbalanced > 0:
        n_form = "unbalanced"
    else:
        n_form = None

    detail = {
        "n_total": n_total,
        "n_balanced": n_balanced,
        "n_truncated": n_truncated,
        "n_unbalanced": n_unbalanced,
    }
    return ScanResult(n_total=n_total, n_balanced=n_balanced,
                      n_truncated=n_truncated, strict_ok=strict_ok,
                      n_form=n_form, detail=detail)
