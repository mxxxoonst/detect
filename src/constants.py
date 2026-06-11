SNIFF_HEAD_BYTES = 65536      # 嗅探读 64KB 头部
SNIFF_LINES = 20              # txt 投票读前 20 行
SQLITE_MAGIC = b"SQLite format 3\x00"   # 16 字节 SQLite 魔数
ACCEPT_THRESHOLD = 0.5        # 投票分数阈值, 低于视为 free_text
LOW_CONF_THRESHOLD = 0.7      # 低置信抽检阈值
SAMPLE_PER_FILE = 1000        # 阶段2每文件抽样 record 数
MAX_BINARY_RATIO = 0.3        # 不可打印字节占比超过此值视为二进制

# 中英 PII 关键词
PII_KEY_PATTERN = (
    r'\b(name|phone|email|mail|id_card|idcard|ssn|social.security|'
    r'address|addr|passport|birth|birthday|gender|sex|'
    r'mobile|tel|telephone|contact|'
    r'身份证|姓名|电话|邮箱|地址|密码|手机|'
    r'生日|性别|年龄|籍贯|民族|住址|'
    r'card_no|cardno|bank|account|credit|'
    r'ip_addr|mac|imei|uuid|token|secret|password|passwd|pwd)\b'
)

# 自由文本字段: 值平均长度阈值
FREE_TEXT_AVG_LEN_THRESHOLD = 100

# SQL 关键词 regex
SQL_KEYWORD_PATTERN = (
    r'\b(create\s+table|insert\s+into|drop\s+table|'
    r'alter\s+table|create\s+index|select\s+.+\s+from|'
    r'update\s+.+\s+set|delete\s+from)\b'
)

# 强 SQL 结构标记 (DDL/DML): 命中即近乎确定为 SQL，权重高于 CSV，
# 解决 "INSERT/CREATE 行以 ; 结尾被误当分号分隔 CSV" 的误判 (见 actually_sql.txt)
SQL_STRONG_PATTERN = (
    r'\b(create\s+table|insert\s+into|drop\s+table|alter\s+table|create\s+index)\b'
)

# 日志行模式
LOG_PATTERN = (
    r'^\s*[\[\(]?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}'
    r'|\b(INFO|WARN|ERROR|DEBUG|TRACE|FATAL)\b'
    r'|^\d{2}:\d{2}:\d{2}'
)

# 强日志信号: 行首时间戳 + 日志级别 双命中 → 近乎确定为日志，权重高于 CSV/JSON，
# 解决 "时间戳里的逗号被当 CSV 列 / 行首 [ 被当 JSON" 的误判 (见 app_server.log)
LOG_TS_PREFIX_PATTERN = (
    r'^\s*[\[\(]?\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}'
    r'|^\s*[\[\(]?\d{2}:\d{2}:\d{2}'
)
LOG_LEVEL_PATTERN = r'\b(INFO|WARN|WARNING|ERROR|DEBUG|TRACE|FATAL)\b'

# 支持的格式
STRUCTURED_FORMATS = {'json', 'jsonl', 'csv', 'tsv', 'sql', 'sqlite'}
UNSTRUCTURED_FORMATS = {'log', 'free_text'}
BINARY_FORMATS = {'db_nonsqlite', 'binary_unknown'}
