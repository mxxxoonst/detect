import json
import os
import re
import csv
import logging
from typing import Iterator
from core.base import BaseParser
from parsers.csv_parser import CsvParser  # 引入 CSV 解析器用于后续处理

from pathlib import Path
logger = logging.getLogger(__name__)
_NO_ARG=object()
class SqlParser(BaseParser):
    def __init__(self, file_path, output_dir, config):
        super().__init__(file_path, output_dir, config)
        
        # 定义中间产物（原始CSV）的存放路径
        #拆解表存放路径 outputdir/raw_extracted_sql/
        self.raw_output_dir = Path(os.path.join(self.output_dir, "raw_extracted_sql"))
        self.detect_path=Path(config['paths'].get('Detect_path',' '))
        # 缓存与状态
        self.schemas = {}
        self.table_writers = {}
        self.generated_files = []  # 记录生成了哪些文件，以便后续处理

        # === 正则表达式编译 ===
        self.create_table_pattern = re.compile(
            r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`'\"]?(\w+)[`'\"]?", 
            re.IGNORECASE
        )
        self.column_def_pattern = re.compile(
            r"^\s*[`'\"]?(\w+)[`'\"]?",
            re.IGNORECASE
        )
        # 只提取INSERT INTO部分，VALUES后单独处理
        self.insert_header_pattern = re.compile(
            r"INSERT\s+INTO\s+[`'\"]?(\w+)[`'\"]?\s*(?:\(([^)]+)\))?", 
            re.IGNORECASE
        )
        self.sql_keywords = {
            'PRIMARY', 'KEY', 'UNIQUE', 'CONSTRAINT', 'FOREIGN', 
            'INDEX', 'FULLTEXT', 'CHECK', 'PARTITION', 'SPATIAL'
        }

    def _get_writer_info(self, table_name, extracted_columns_str=None):
        """获取 Writer 信息 (包含断点续传/复活逻辑)"""
        # 1. 检查缓存中是否已存在且活跃
        if table_name in self.table_writers:
            info = self.table_writers[table_name]
            if not info['handle'].closed:
                return info
            else:
                logger.info(f"表 {table_name} 句柄已关闭，正在重新打开(追加模式)...")

        # 2. 准备基准路径
        if not os.path.exists(self.raw_output_dir):
            os.makedirs(self.raw_output_dir)
        #为每一个拆解后的table文件创建一个目录
        project_name=self.file_path.parent.name
        output_dir=self.raw_output_dir / project_name
        if not os.path.exists(output_dir):
            os.makedirs(output_dir)    
        csv_path = output_dir / f"{table_name}.csv"
        
        # 3. 决定模式 (w:新建, a:追加)
        mode = 'w'
        if os.path.exists(csv_path):
            mode = 'a'
        
        try:
            f = open(csv_path, mode, encoding='utf-8-sig', newline='')
            writer = csv.writer(f)
            
            headers = []
            # 尝试获取列名 (优先用 INSERT 里的，其次用 Schema 缓存里的)
            if extracted_columns_str:
                headers = [c.strip().strip("`'\"") for c in extracted_columns_str.split(',')]
            elif table_name in self.schemas:
                headers = self.schemas[table_name]
            
            # 写入表头逻辑
            has_header = False
            if mode == 'w' and headers:
                writer.writerow(headers)
                has_header = True
            elif mode == 'a' and os.path.getsize(csv_path) == 0 and headers:
                writer.writerow(headers)
                has_header = True
            
            # 更新缓存
            self.table_writers[table_name] = {
                'handle': f,
                'writer': writer,
                'headers': headers,
                'has_header': has_header
            }
            
            # 记录生成的文件路径
            if csv_path not in self.generated_files:
                self.generated_files.append(csv_path)

            return self.table_writers[table_name]
            
        except IOError as e:
            logger.error(f"无法打开 CSV 文件 {csv_path}: {e}")
            return None

    def _parse_values_part(self, values_str):
        """解析 SQL Value 元组"""
        rows = []
        content = values_str.strip().rstrip(";")
        
        # 正则提取 (...) 
        tuple_matches = re.findall(r"\((.*?)\)(?:,|$)", content)
        
        for match in tuple_matches:
            try:
                # 增大字段限制，防止超长字符串报错
                csv.field_size_limit(2147483647) 
                reader = csv.reader([match], delimiter=',', quotechar="'", skipinitialspace=True)
                for row in reader:
                    rows.append(row)
            except csv.Error as e:
                logger.warning(f"CSV Reader 解析值错误: {e}")
                continue
        return rows

    def _write_rows(self, writer_info, content):
        if not writer_info: 
            return
        try:
            rows = self._parse_values_part(content)
            if rows:
                writer_info['writer'].writerows(rows)
        except Exception as e:
            logger.error(f"写入行失败: {e}")

        """解析 CREATE TABLE 获取列名"""
    def _parse_create_table(self, f_handle: Iterator[str], table_name: str):
        column = []
        while True:
            try:
                line = next(f_handle).strip()
            except StopIteration:
                break
            
            if line.startswith(")") or line.endswith(";"):
                break
            if not line or line.startswith(("--", "/*", "#")):
                continue
                
            match = self.column_def_pattern.match(line)
            if match:
                col_name = match.group(1)
                if col_name.upper() in self.sql_keywords:
                    continue
                column.append(col_name)
        
        if column:
            self.schemas[table_name] = column
            logger.info(f"解析到表结构: {table_name} ({len(column)} columns)")

    def process(self):
        """
        主流程：
        1. 拆解 SQL -> Raw CSVs
        2. 遍历 Raw CSVs -> 调用 CsvParser -> Standard CSVs
        """
        logger.info(f"启动 SQL 拆解器: {self.file_path}")
        
        # === 阶段 1: SQL 拆解 ===
        current_table = None
        current_writer_info = None
        
        try:
            with open(self.file_path, 'r', encoding='utf-8', errors='ignore') as f:
                f_iter = iter(f)
                for line_num, line in enumerate(f_iter, 1):
                    if line_num % 50000 == 0:
                        logger.info(f"已扫描 {line_num} 行 SQL...")

                    line_strip = line.strip()
                    if not line_strip: 
                        continue
                    
                    try:
                        line_upper = line.upper()
                        
                        # --- CREATE TABLE ---
                        if line_upper.startswith("CREATE TABLE"):
                            current_table = None
                            current_writer_info = None
                            match_table = self.create_table_pattern.match(line_strip)
                            if match_table:
                                self._parse_create_table(f_iter, match_table.group(1))
                            continue

                        # --- INSERT INTO ---
                        if line_upper.startswith("INSERT INTO"):
                            match_insert = self.insert_header_pattern.match(line_strip)
                            if match_insert:
                                table_name = match_insert.group(1)
                                cols_str = match_insert.group(2)
                                
                                current_writer_info = self._get_writer_info(table_name, cols_str)
                                current_table = table_name
                                
                                val_idx = line_upper.find("VALUES")
                                if val_idx != -1:
                                    values_part = line_strip[val_idx + 6:].strip()
                                    if values_part:
                                        self._write_rows(current_writer_info, values_part)
                        
                        # --- VALUES (跨行) ---
                        elif current_table and current_writer_info:
                            content = line_strip
                            if content.upper().startswith("VALUES"):
                                content = content[6:].strip()
                            
                            if content and content.startswith("("):
                                self._write_rows(current_writer_info, content)
                        
                        # --- 结束符 ---
                        if line_strip.endswith(';'):
                            current_table = None
                            current_writer_info = None

                    except Exception as loop_e:
                        logger.warning(f"行 {line_num} 解析警告: {loop_e}")

        except Exception as e:
            logger.error(f"SQL 文件读取严重错误: {e}")
        
        finally:
            # 清理文件句柄
            for info in self.table_writers.values():
                try:
                    if not info['handle'].closed:
                        info['handle'].close()
                except Exception as e:
                    logger.error(f"文件句柄清理失败: {e}")
                    pass
            self.table_writers.clear()
            logger.info(f"SQL 拆解完成，生成 {len(self.generated_files)} 个原始 CSV 文件。")

        # === 阶段 2: 自动调用 CSV Parser ===
        if self.generated_files:
            logger.info("=" * 30)
            logger.info("开始对拆解后的 CSV 进行标准化清洗...")
            
            for raw_csv_path in self.generated_files:
                try:
                    # 实例化 CsvParser
                    # 注意：output_dir 依然是 self.output_dir (最终结果目录)，而不是 raw_output_dir
                    worker = CsvParser(raw_csv_path, self.output_dir, self.config)
                    worker.process()
                except Exception as e:
                    logger.error(f"子任务失败 ({os.path.basename(raw_csv_path)}): {e}")
            
            logger.info("所有子 CSV 处理完成。")

    
    def detect(self,source_type=_NO_ARG):
        """
        主流程：
        1. 拆解 SQL -> Raw CSVs
        2. 遍历 Raw CSVs -> 调用 CsvParser -> detect
        """
        logger.info(f"启动 SQL 拆解器: {self.file_path}")
        
        # === 阶段 1: SQL 拆解 ===
        current_table = None
        current_writer_info = None
        
        try:
            with open(self.file_path, 'r', encoding='utf-8', errors='ignore') as f:
                f_iter = iter(f)
                for line_num, line in enumerate(f_iter, 1):
                    if line_num % 50000 == 0:
                        logger.info(f"已扫描 {line_num} 行 SQL...")

                    line_strip = line.strip()
                    if not line_strip: 
                        continue
                    
                    try:
                        line_upper = line.upper()
                        
                        # --- CREATE TABLE ---
                        if line_upper.startswith("CREATE TABLE"):
                            current_table = None
                            current_writer_info = None
                            match_table = self.create_table_pattern.match(line_strip)
                            if match_table:
                                self._parse_create_table(f_iter, match_table.group(1))
                            continue

                        # --- INSERT INTO ---
                        if line_upper.startswith("INSERT INTO"):
                            match_insert = self.insert_header_pattern.match(line_strip)
                            if match_insert:
                                table_name = match_insert.group(1)
                                cols_str = match_insert.group(2)
                                
                                current_writer_info = self._get_writer_info(table_name, cols_str)
                                current_table = table_name
                                
                                val_idx = line_upper.find("VALUES")
                                if val_idx != -1:
                                    values_part = line_strip[val_idx + 6:].strip()
                                    if values_part:
                                        self._write_rows(current_writer_info, values_part)
                        
                        # --- VALUES (跨行) ---
                        elif current_table and current_writer_info:
                            content = line_strip
                            if content.upper().startswith("VALUES"):
                                content = content[6:].strip()
                            
                            if content and content.startswith("("):
                                self._write_rows(current_writer_info, content)
                        
                        # --- 结束符 ---
                        if line_strip.endswith(';'):
                            current_table = None
                            current_writer_info = None

                    except Exception as loop_e:
                        logger.warning(f"行 {line_num} 解析警告: {loop_e}")

        except Exception as e:
            logger.error(f"SQL 文件读取严重错误: {e}")
        
        finally:
            # 清理文件句柄
            for info in self.table_writers.values():
                try:
                    if not info['handle'].closed:
                        info['handle'].close()
                except Exception as e:
                    logger.error(f"文件句柄清理失败: {e}")
                    pass
            self.table_writers.clear()
            logger.info(f"SQL 拆解完成，生成 {len(self.generated_files)} 个原始 CSV 文件。")

        # === 阶段 2: 自动调用 CSV Parser ===
        if self.generated_files:
            logger.info("=" * 30)
            logger.info("开始对拆解后的 CSV 进行字段shema映射...")
            # raw_csv_path ：{output_dir}/{table_name}.csv
            for raw_csv_path in self.generated_files:
                try:
                    # 实例化 CsvParser
                    # 注意：output_dir 依然是 self.output_dir (最终结果目录)，而不是 raw_output_dir
                    worker = CsvParser(raw_csv_path, self.output_dir, self.config)
                    result=worker._save_discovery_report()
                    path_table=raw_csv_path.stem
                    if source_type is not _NO_ARG :
                        result["source_type"]=source_type
                    else:
                        result["source_type"]="sql"
                    result["table_name"]=str(path_table)
                    
                    with open(self.detect_path,'a',encoding="utf-8") as f:
                        json_str=json.dumps(result,ensure_ascii=False)
                        f.write(json_str+'\n')
                    logger.info(f" SQL字段 探查结束: {self.file_path}")
                except Exception as e:
                    logger.error(f"子任务失败 ({os.path.basename(raw_csv_path)}): {e}")
            
            logger.info("所有子 CSV 处理完成。")