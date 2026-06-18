"""测试数据生成器: 覆盖所有格式 × 质量等级 × 编码组合。

用法: python test_data/generate.py [--output test_data/samples] [--seed 42]
"""

import argparse
import json
import random
import sys
from pathlib import Path

# ──── 确保 src 在 path 中 ────
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


OUTPUT_DIR = "test_data/samples"
SEED = 42


# ════════════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════════════


def write_text(path: Path, content: str, encoding: str = "utf-8"):
    path.write_text(content, encoding=encoding)


def write_bytes(path: Path, content: bytes):
    path.write_bytes(content)


# ════════════════════════════════════════════════════════════════
# 数据模板
# ════════════════════════════════════════════════════════════════

CN_NAMES = ["张三", "李四", "王五", "赵六", "陈七", "周八", "吴九", "郑十",
            "刘建国", "孙志强", "黄美丽", "钱小红", "马大伟", "朱晓明", "胡海龙"]
CN_PHONES = [f"138{random.randint(10000000, 99999999)}" for _ in range(20)]
CN_EMAILS = [f"user{i}@example.com" for i in range(20)]
CN_ID_CARDS = [f"{random.randint(100000, 999999)}1990{random.randint(1,12):02d}{random.randint(1,28):02d}{random.randint(1000, 9999)}" for _ in range(20)]
CN_ADDRESSES = ["北京市朝阳区某某路100号", "上海市浦东新区某某大厦A座", "广州市天河区某某街道8号",
                "深圳市南山区科技园某某楼", "杭州市西湖区某某路200号", "成都市武侯区某某巷50号"]
CN_PROVINCES = ["北京", "上海", "广东", "浙江", "江苏", "四川", "湖北", "湖南"]
CN_GENDERS = ["男", "女"]


def make_user_record(i: int) -> dict:
    return {
        "id": i + 1,
        "name": random.choice(CN_NAMES),
        "gender": random.choice(CN_GENDERS),
        "age": random.randint(18, 65),
        "phone": random.choice(CN_PHONES),
        "email": random.choice(CN_EMAILS),
        "id_card": random.choice(CN_ID_CARDS),
        "address": random.choice(CN_ADDRESSES),
        "province": random.choice(CN_PROVINCES),
        "score": round(random.uniform(60, 100), 2),
        "created_at": f"2024-{random.randint(1,12):02d}-{random.randint(1,28):02d}T{random.randint(0,23):02d}:{random.randint(0,59):02d}:00Z",
    }


# ════════════════════════════════════════════════════════════════
# 各格式生成器
# ════════════════════════════════════════════════════════════════


def gen_json_tier1(out: Path):
    """干净 JSON 文件."""
    records = [make_user_record(i) for i in range(20)]

    write_text(out / "clean_users.json", json.dumps(records, indent=2, ensure_ascii=False))

    jsonl = "\n".join(json.dumps(r, ensure_ascii=False) for r in records)
    write_text(out / "clean_users.jsonl", jsonl)

    write_text(out / "clean_single_user.json", json.dumps(records[0], indent=2, ensure_ascii=False))

    nested = {
        "org": "测试公司",
        "departments": [
            {"name": "技术部", "manager": records[0], "members": records[1:5]},
            {"name": "产品部", "manager": records[5], "members": records[6:10]},
        ]
    }
    write_text(out / "clean_nested.json", json.dumps(nested, indent=2, ensure_ascii=False))


def gen_json_tier2(out: Path):
    """噪声 JSON: 尾逗号、单引号、注释、不完整."""
    records = [make_user_record(i) for i in range(5)]

    lines = ["["]
    for i, r in enumerate(records):
        lines.append("  {")
        lines.append(f"    // 用户 {i+1}")
        lines.append(f"    'id': {r['id']},")
        lines.append(f"    'name': '{r['name']}',")
        lines.append(f"    'phone': '{r['phone']}',")
        lines.append(f"    'email': '{r['email']}',")
        lines.append("  },")
    lines.append("]")
    write_text(out / "noisy_trailing_comma.json", "\n".join(lines))

    incomplete = json.dumps(records, indent=2, ensure_ascii=False)
    write_text(out / "noisy_incomplete.json", incomplete[:-20])

    content = json.dumps(records, indent=2, ensure_ascii=False)
    write_bytes(out / "noisy_bom.json", b"\xef\xbb\xbf" + content.encode("utf-8"))


def gen_json_gbk(out: Path):
    """GBK 编码 JSON."""
    records = [make_user_record(i) for i in range(10)]
    content = json.dumps(records, indent=2, ensure_ascii=False)
    write_bytes(out / "gbk_users.json", content.encode("gbk"))


def gen_csv_tier1(out: Path):
    """干净 CSV / TSV."""
    records = [make_user_record(i) for i in range(30)]
    headers = list(records[0].keys())

    lines = [",".join(headers)]
    for r in records:
        lines.append(",".join(str(r[h]) for h in headers))
    write_text(out / "clean_users.csv", "\n".join(lines))

    lines = ["\t".join(headers)]
    for r in records:
        lines.append("\t".join(str(r[h]) for h in headers))
    write_text(out / "clean_users.tsv", "\n".join(lines))

    lines = ["|".join(headers)]
    for r in records:
        lines.append("|".join(str(r[h]) for h in headers))
    write_text(out / "clean_users_pipe.csv", "\n".join(lines))


def gen_csv_gbk(out: Path):
    """GBK 编码 CSV."""
    records = [make_user_record(i) for i in range(15)]
    headers = list(records[0].keys())
    lines = [",".join(headers)]
    for r in records:
        lines.append(",".join(str(r[h]) for h in headers))
    write_bytes(out / "gbk_users.csv", "\n".join(lines).encode("gbk"))


def gen_csv_tier2(out: Path):
    """噪声 CSV: 列数不一致."""
    headers = "id,name,gender,age,phone,email,id_card,address,province,score,created_at"
    lines = [headers]
    for i in range(20):
        if i in (5, 12):
            lines.append(f"{i+1},张三,男,28,13800001111")
        elif i == 8:
            lines.append(f"{i+1},李四,女,35,13900002222,lisi@ex.com,1234567890,北京朝阳,北京,95.5,2024-01-01T00:00:00Z,extra_col")
        else:
            r = make_user_record(i)
            lines.append(",".join(str(r[h]) for h in headers.split(",")))
    write_text(out / "noisy_column_drift.csv", "\n".join(lines))


def gen_csv_utf16(out: Path):
    """UTF-16 (带 BOM) CSV: 验证 BOM 文本不被误判二进制 (CTARS_BF 场景)。"""
    records = [make_user_record(i) for i in range(15)]
    headers = list(records[0].keys())
    lines = [",".join(headers)]
    for r in records:
        lines.append(",".join(str(r[h]) for h in headers))
    write_bytes(out / "utf16_users.csv", "\n".join(lines).encode("utf-16"))


def gen_csv_headerless_noisy(out: Path):
    """无表头 CSV, 约 1/4 行因 value 内嵌逗号列数漂移: 验证众数列稳定性 (Yatra_BF 场景)。"""
    rows = []
    for i in range(20):
        r = make_user_record(i)
        if i % 4 == 0:
            rows.append(f"{r['id']},{r['name']},{r['phone']},a,b,c")   # 多 2 列
        else:
            rows.append(f"{r['id']},{r['name']},{r['phone']},{r['address']}")
    write_text(out / "headerless_noisy.csv", "\n".join(rows))


def gen_sql_tier1(out: Path):
    """干净 SQL 文件: CREATE TABLE + INSERT."""
    sql = """-- 用户表
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name VARCHAR(50) NOT NULL,
    gender CHAR(1),
    age INTEGER,
    phone VARCHAR(20),
    email VARCHAR(100),
    id_card VARCHAR(18),
    address VARCHAR(200),
    province VARCHAR(20),
    score DECIMAL(5,2),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_users_phone ON users(phone);
CREATE INDEX idx_users_email ON users(email);

"""
    records = [make_user_record(i) for i in range(10)]
    for r in records:
        sql += (
            f"INSERT INTO users (id, name, gender, age, phone, email, id_card, address, province, score, created_at) "
            f"VALUES ({r['id']}, '{r['name']}', '{r['gender']}', {r['age']}, '{r['phone']}', "
            f"'{r['email']}', '{r['id_card']}', '{r['address']}', '{r['province']}', {r['score']}, '{r['created_at']}');\n"
        )
    write_text(out / "clean_schema.sql", sql)


def gen_sql_tier2(out: Path):
    """噪声 SQL: 截断/引号不闭合."""
    sql = """CREATE TABLE products (
    id INTEGER PRIMARY KEY,
    name VARCHAR(100),
    price DECIMAL(10,2)
);

INSERT INTO products (id, name, price) VALUES (1, 'Widget', 9.99);
INSERT INTO products (id, name, price) VALUES (2, 'Gadget', 19.99);
INSERT INTO products (id, name, price) VALUES (3, 'Broken quote, 29.99);
INSERT INTO products (id, name, price) VALUES (4, 'Doohickey', 39.99);
"""
    write_text(out / "noisy_truncated.sql", sql)


def gen_xlsx_tier1(out: Path):
    """干净 xlsx: 多 sheet (users / orders), 首行表头。需要 openpyxl。"""
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "users"
    records = [make_user_record(i) for i in range(20)]
    headers = list(records[0].keys())
    ws.append(headers)
    for r in records:
        ws.append([r[h] for h in headers])

    ws2 = wb.create_sheet("orders")
    order_headers = ["id", "user_id", "product", "amount", "order_date"]
    ws2.append(order_headers)
    products = ["笔记本电脑", "手机", "键盘", "鼠标", "显示器"]
    for i in range(15):
        ws2.append([
            i + 1, random.randint(1, 20), random.choice(products),
            round(random.uniform(9.9, 9999.0), 2),
            f"2024-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}",
        ])

    wb.save(str(out / "clean_users.xlsx"))


def gen_log_files(out: Path):
    """生成日志文件."""
    log_lines = []
    levels = ["INFO", "WARN", "ERROR", "DEBUG"]
    for i in range(50):
        ts = f"2024-{random.randint(1,12):02d}-{random.randint(1,28):02d} {random.randint(0,23):02d}:{random.randint(0,59):02d}:{random.randint(0,59):02d},123"
        level = random.choice(levels)
        msgs = [
            f"Request processed from 192.168.1.{random.randint(1,255)}",
            f"用户 {random.choice(CN_NAMES)} 登录成功",
            "Database connection timeout after 30s",
            f"File upload completed: report_{random.randint(1,99)}.pdf",
            f"权限检查失败: user_id={random.randint(1000,9999)}",
            f"Cache miss for key: session:{random.randint(10000,99999)}",
        ]
        log_lines.append(f"[{ts}] [{level}] {random.choice(msgs)}")
    write_text(out / "app_server.log", "\n".join(log_lines))


def gen_free_text(out: Path):
    """生成自由文本文件 (含 PII 在句中)."""
    texts = [
        "申请人张三，身份证号440106199003151234，联系电话13800001111，现居住于北京市朝阳区某某路100号。该申请人于2024年1月提交了贷款申请，申请金额为50万元。",
        "员工李四的入职信息如下：姓名李四，性别女，出生日期1992年8月15日，邮箱lisi@example.com。紧急联系人王五，电话13900002222。",
        "Dear customer, your account has been created. Please verify your email address: zhangsan@example.com. Your phone number 13812345678 has been registered.",
        "系统通知：用户赵六(身份证320106198505060011)的资料审核已通过，请尽快邮寄相关材料至上海市浦东新区某某大厦A座。",
        "这是一个普通的文本段落，不包含任何结构化信息。它用于测试自由文本的检测能力。系统应该能识别出这类文件属于 free_text 类型而非结构化数据。",
    ]
    full_text = "\n\n".join(texts * 3)
    write_text(out / "free_text_zh.txt", full_text)


def gen_empty_files(out: Path):
    """空文件."""
    write_text(out / "empty.txt", "")
    write_text(out / "empty.csv", "")
    write_text(out / "empty.json", "")


def gen_wrong_extension(out: Path):
    """扩展名与实际内容不符的文件."""
    records = [make_user_record(i) for i in range(5)]
    write_text(out / "actually_json.txt", json.dumps(records, indent=2, ensure_ascii=False))

    headers = "id,name,phone,email"
    lines = [headers] + [f"{i+1},{make_user_record(i)['name']},{make_user_record(i)['phone']},{make_user_record(i)['email']}" for i in range(5)]
    write_text(out / "actually_csv.txt", "\n".join(lines))

    sql = "CREATE TABLE test (id INT);\nINSERT INTO test VALUES (1);\nINSERT INTO test VALUES (2);\n"
    write_text(out / "actually_sql.txt", sql)

    log_lines = []
    for i in range(20):
        log_lines.append(f"2024-01-{random.randint(1,28):02d}T12:00:00Z INFO Processing item {i}")
    write_text(out / "actually_log.txt", "\n".join(log_lines))


def gen_binary_files(out: Path):
    """其他二进制文件."""
    write_bytes(out / "random.bin", random.Random(42).randbytes(4096))


# ════════════════════════════════════════════════════════════════
# 主入口
# ════════════════════════════════════════════════════════════════

GENERATORS = [
    ("json_tier1", gen_json_tier1),
    ("json_tier2", gen_json_tier2),
    ("json_gbk", gen_json_gbk),
    ("csv_tier1", gen_csv_tier1),
    ("csv_gbk", gen_csv_gbk),
    ("csv_tier2", gen_csv_tier2),
    ("csv_utf16", gen_csv_utf16),
    ("csv_headerless_noisy", gen_csv_headerless_noisy),
    ("sql_tier1", gen_sql_tier1),
    ("sql_tier2", gen_sql_tier2),
    ("xlsx_tier1", gen_xlsx_tier1),
    ("log_files", gen_log_files),
    ("free_text", gen_free_text),
    ("empty_files", gen_empty_files),
    ("wrong_extension", gen_wrong_extension),
    ("binary_files", gen_binary_files),
]


def main():
    parser = argparse.ArgumentParser(description="测试数据生成器")
    parser.add_argument("--output", default=OUTPUT_DIR, help=f"输出目录 (默认: {OUTPUT_DIR})")
    parser.add_argument("--seed", type=int, default=SEED, help=f"随机种子 (默认: {SEED})")
    args = parser.parse_args()

    random.seed(args.seed)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    print(f"生成测试数据到: {out}")
    for name, gen_func in GENERATORS:
        try:
            gen_func(out)
            print(f"  [OK] {name}")
        except Exception as e:
            print(f"  [FAIL] {name}: {e}")

    count = len(list(out.iterdir()))
    print(f"\n总计生成 {count} 个测试文件")


if __name__ == "__main__":
    main()
