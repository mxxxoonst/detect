import json
from pathlib import Path
from parsers.json_parser import JsonParser
from parsers.sql_parser import SqlParser
from parsers.csv_parser import CsvParser  
from core.format_detector import FormatDetector
from core.base import BaseParser
import logging
logger = logging.getLogger(__name__)

class TxtParser(BaseParser):
    """
    TxtParser 不做具体解析，而是作为 Gateway (网关)
    根据 Detector 的结果，实例化并调用具体的 Parser
    """
    def __init__(self, file_path, output_dir, config):
        super().__init__(file_path, output_dir, config)
        self.detector = FormatDetector()
        self.detect_path=Path(config['paths'].get('Detect_path',' '))

    def process(self):
        """
        统一入口：分析 TXT -> 路由 -> 调用具体 Parser -> 返回结果
        """
       # 1. 探测格式
        fmt_type, meta = self.detector.detect(self.file_path)
        logger.info(f"[TxtParser] 检测到文件类型: {fmt_type}, 元数据: {meta}")

        # 2. 动态路由与修正
        if fmt_type == "json":
            # 实例化 JsonParser 并委托任务
            parser = JsonParser(self.file_path, self.output_dir, self.config)
            return parser.process()
        elif fmt_type == "csv":
        
            parser = CsvParser(self.file_path, self.output_dir, self.config)
            
            # 获取探测到的分隔符
            detected_sep = meta.get("delimiter")
            
            # 修正 CsvParser 的分隔符
            if detected_sep:
                parser.sep = detected_sep 
                
            return parser.process()

        elif fmt_type == "sql":
            parser = SqlParser(self.file_path, self.output_dir, self.config)
            return parser.process()
            
        else:
            logger.error(f"无法处理的格式: {fmt_type}")
    
    def detect(self):
        """
        负责schema分析
        """
       # 1. 探测格式
        fmt_type, meta = self.detector.detect(self.file_path)
        logger.info(f"[TxtParser] 检测到文件类型: {fmt_type}, 元数据: {meta}")

        # 2. 动态路由与修正
        if fmt_type == "json":
            # 实例化 JsonParser 并委托任务
            parser = JsonParser(self.file_path, self.output_dir, self.config)
            return parser.detect(source_type="txt")

        elif fmt_type == "csv":
        
            parser = CsvParser(self.file_path, self.output_dir, self.config)
            
            # 获取探测到的分隔符
            detected_sep = meta.get("delimiter")
            
            # 修正 CsvParser 的分隔符
            if detected_sep:
                parser.sep = detected_sep 
                
            return parser.detect(source_type="txt")

        elif fmt_type == "sql":
            parser = SqlParser(self.file_path, self.output_dir, self.config)
            return parser.detect(source_type="txt")
            
        else:
            logger.error(f"无法处理的格式: {fmt_type}")