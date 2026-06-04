import json
import logging
from pathlib import Path
import pandas as pd
import csv
import io
from core.base import BaseParser
from core.llm_engine import LLMMappingEngine

logger = logging.getLogger(__name__)
_NO_ARG=object()
class CsvParser(BaseParser):
    def __init__(self, file_path, output_dir, config):
        super().__init__(file_path, output_dir, config)
        self.llm_engine = LLMMappingEngine(config)
        # 支持从配置中读取分隔符，默认为 None (自动推断)
        self.sep = config.get('settings', {}).get('csv_sep', None)
        
        self.detect_path=Path(config['paths'].get('Detect_path',' '))
    def _read_csv_safe(self,only_header=False,nrows=None)->pd.DataFrame:
        """
        统一的安全读取方法，自动处理 NUL 字节问题
        :param only_header: 是否只读取表头（为了性能）
        :param nrows: 读取行数限制
        :return: DataFrame
        """
        read_args={
            "sep":self.sep,
            "engine":"python",
            "encoding":"utf-8-sig",
            "encoding_errors":"ignore",
            "on_bad_lines":"skip"

        }
        if only_header:
            read_args["nrows"]=0
        elif nrows:
            read_args["nrows"]=nrows
        try:
            return pd.read_csv(self.file_path,**read_args)
        except csv.Error as e:
            if "NUL" in str(e) or "NULL" in str(e):
                logger.warning(f"检测到 NUL 字节，启用内存清洗模式: {self.file_path.name}")
                return self._read_cleaned_data(read_args)
            raise e
        except Exception as e:
            logger.error(f"读取 CSV 失败: {e}")
            raise e
    def _read_cleaned_data(self, read_args)->pd.DataFrame :
        """
        读取文件到内存，清洗 NUL 字节，然后转为 DataFrame
        注意：对于极大文件(>2GB)可能需要改写为分块处理，目前方案适用于中小型文件
        """
        try:
            with open(self.file_path, 'r', encoding='utf-8', errors='replace') as f:
                # 一次性读取并替换 (内存消耗较大，但最简单)
                content = f.read().replace('\0', '')
            
            return pd.read_csv(io.StringIO(content), **read_args)
        except Exception as e:
            logger.error(f"清洗并读取失败: {e}")
            raise e
    def _get_headers(self) -> list:
        """阶段一：发现表头"""
        try:
            # 只读前几行获取表头，节省内存
            df=self._read_csv_safe(only_header=True)
            headers = df.columns.to_list()
            logger.info(f"CSV 表头识别: {headers}")
            return headers
        except Exception as e:
            logger.error(f"无法读取 CSV 表头: {e}")
            return []
    def _transform_and_save(self, mapping_dict: dict):
        """阶段三：ETL 转换与保存 (保留你的 Pandas 逻辑)"""
        if not mapping_dict:
            logger.warning("映射字典为空，跳过转换")
            return

        try:
            
            # 1. 读取源文件
            logger.info("正在加载 CSV 数据...")
            df_input = self._read_csv_safe(only_header=False)

            # 2. 准备输出 DataFrame
            standard_columns = list(mapping_dict.keys())
            max_src_col = df_input.shape[1] - 1
            df_output = pd.DataFrame(index=df_input.index, columns=standard_columns)

            # 3. 应用映射规则
            invalid_values = {'', '0', '0.0', '0.00', 'nan', 'null', 'none'}
            
            for col_name, rule in mapping_dict.items():
                # --- 情况 A: 单列直接映射 ---
                if isinstance(rule, int):
                    if rule < 0: 
                        continue # 跳过 -1
                    if rule > max_src_col:
                        logger.warning(f"字段 [{col_name}] 索引越界 ({rule})，跳过")
                        continue
                    
                    df_output[col_name] = df_input.iloc[:, rule]

                # --- 情况 B: 多列组合映射 ---
                elif isinstance(rule, list):
                    valid_idx = [i for i in rule if 0 <= i <= max_src_col]
                    if not valid_idx:
                     continue
                    
                    join_char =self.llm_engine.standard_fields.get("join_with"," ")  # 这里也可以从 standard_fields.json 读取 join_with
                    df_subset = df_input.iloc[:, valid_idx]
                    
                    df_output[col_name] = df_subset.apply(
                        lambda row: join_char.join(
                            [
                                str(x) for x in row 
                                if pd.notna(x) 
                                and str(x).strip().lower() not in invalid_values
                            ]
                        ),
                        axis=1
                    )

            # 4. 填充空值并保存
            df_output = df_output.fillna(" ")
            
            # 确保目录存在
            self.output_dir.mkdir(parents=True, exist_ok=True)
            
            #为每一个文件创建一个目录
            #project_name=self.file_path.parent.name
            #file_name=self.file_path.stem
            #output_dir=os.path.join(project_name,self.output_dir)
            #if not os.path.exists(output_dir):
            #    os.makedirs(output_dir)
            file_name=self.file_path.stem
            output_file = self.output_dir / f"{file_name}_parsed.csv"
            
            df_output.to_csv(output_file, index=False, encoding="utf-8-sig")
            logger.info(f"CSV 处理完成，已保存至: {output_file}")

        except Exception as e:
            logger.error(f"Pandas 转换失败: {e}")
            raise e
    def _save_discovery_report(self):
        """保存字段分析报告"""
        logger.info("\n[+] CSV发现阶段报告开始写入。")
 
        ##读取csv前几行获取字段schema和sample_value
        header_Sample=self._read_csv_safe(False,10)
        header_clean=header_Sample.where(header_Sample.notna(),None)
        result={
            "file_name":self.file_path.name,
            "source_type":"csv",
            "field_name":header_clean.columns.to_list(),
            "sample_values":header_clean.values.tolist(),
        }
        return result
    def process(self):
        logger.info(f"启动 CSV 解析器: {self.file_path}")
        
        # 1. 获取表头
        headers = self._get_headers()
        if not headers: 
            return

        # 2. 获取映射 (调用新写的 csv 方法)
        logger.info("正在进行 AI 语义映射...")
        mapping_dict = self.llm_engine.generate_csv_header_mapping(headers)
        
        # 打印映射结果日志
        logger.info(f"映射策略生成: {mapping_dict}")

        # 3. 执行转换
        self._transform_and_save(mapping_dict)
    def detect(self,source_type=_NO_ARG):
        logger.info(f"启动 CSV 解析器只做Schema字段语义探查: {self.file_path}")
        
        result=self._save_discovery_report()
        if source_type is not _NO_ARG:
            result["source_type"]=source_type
        
        with open(self.detect_path,'a',encoding="utf-8") as f:
            json_str=json.dumps(result,ensure_ascii=False)
            f.write(json_str+'\n')
        logger.info(f" CSV 探查结束: {self.file_path}")