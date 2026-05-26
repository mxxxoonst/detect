import os
import json
import csv
import logging
from core.base import BaseParser
from core.utils import stream_read_json, flatten_json
from core.llm_engine import LLMMappingEngine
from pathlib import Path
logger = logging.getLogger(__name__)
_NO_ARG=object()
class JsonParser(BaseParser):
    def __init__(self, file_path, output_dir, config):
        super().__init__(file_path, output_dir, config)
        self.llm_engine = LLMMappingEngine(config)
        self.mapping_rules = {}
        self.detect_path=Path(config['paths'].get('Detect_path',' '))


    def _discovery_phase(self) -> dict:
        """阶段一：发现模式，生成 Schema 统计"""
        logger.info("启动阶段一: Schema 发现")
        key_stats = {}
        total = 0
        
        # 使用工具流式读取
        for obj in stream_read_json(self.file_path):
            total += 1
            flat = flatten_json(obj)
            for k, v in flat.items():
                if k not in key_stats:
                    key_stats[k] = {'count': 1, 'example_value': v}
                else:
                    key_stats[k]['count'] += 1
            
            if total > 5000: # [优化] 采样前5000条即可，不需要全量跑Discovery
                 break
        
        logger.info(f"Schema 发现完成，共识别 {len(key_stats)} 个字段 (采样 {total} 条)")
        
        # 可以在这里保存一份可读的字段分析报告
        self._save_discovery_report(key_stats,total)
        return key_stats
    def _schema_detect(self) -> dict:
        """阶段一：发现模式，生成 Schema字段样本 统计"""
        logger.info("启动阶段一: Schema 发现")
        key_stats = {}
        total = 0
        ## 增加用于检测json文件字段样例逻辑
        raw_samples=[]
        
        # 使用工具流式读取
        for obj in stream_read_json(self.file_path):
            total += 1
            flat = flatten_json(obj)
            sample_list=[]
                
            for k, v in flat.items():
                if k not in key_stats:
                    key_stats[k] = {'count': 1, 'example_value': v}
                else:
                    key_stats[k]['count'] += 1
                sample_list.append(v)
            #采样十条样本记录
            if total <10:
                raw_samples.append(sample_list)
            if total > 50000: # [优化] 采样前50000条即可，不需要全量跑Discovery
                 break
        all_keys=list(key_stats.keys())
        result={
            "file_name":self.file_path.name,
            "source_type":"json",
            "field_name":all_keys,
            "sample_values":raw_samples
        }
        logger.info(f"Schema 发现完成，共识别 {len(key_stats)} 个字段 (采样 {total} 条)")
        
        return result
    def _save_discovery_report(self, stats: dict,total:int):
        """保存字段分析报告"""
        report_path = Path(os.path.join(self.output_dir, f"{os.path.basename(self.file_path)}_report.txt"))
        logger.info(f"\n[+] 发现阶段完成。共处理 {total} 个对象。")
        logger.info(f"[+] 共发现 {len(stats)} 个唯一的扁平化key。")

    # L (加载) -> 写入报告
        try:
        # 按 'count' 降序排序 key_stats
        # key_stats.items() -> [('key_name', {'count': 123, ...}), ...]
            sorted_stats = sorted(
                stats.items(), 
                key=lambda item: item[1]['count'], 
                reverse=True
            )
            
            with open(report_path, 'w', encoding='utf-8') as f:
                f.write("--- Schema 发现报告 ---\n")
                f.write(f"源文件: {self.file_path}\n")
                f.write(f"总计处理对象: {total}\n")
                f.write(f"唯一Key数量: {len(stats)}\n")
                f.write(f"{'='*30}\n\n")
                f.write("Key (频率降序):\n")
                f.write(f"{'Key (扁平化)':<40} {'出现次数':<15} {'示例值 (首次出现)'}\n")
                # key 左对齐并占40个字符宽度
            
                f.write(f"{'-'*40:<40} {'-'*15:<15} {'-'*30}\n")
                
                # 遍历排序后的列表
            for key, stats in sorted_stats:
                count = stats['count']
                example = stats['example_value']
                
                # 使用 repr() 来显示字符串的引号，使 '40500' (str) 和 40500 (int) 有区别
                f.write(f"{key:<40} {count:<15} {repr(example)}\n")
        # --- [修改结束] ---
                
            logger.info(f"[+] 成功写入报告: {report_path}")
        except Exception as e:
            logger.error(f"!! 写入报告失败: {e}")

    def _transform_row(self, flat_obj: dict) -> dict:
        """阶段三：转换引擎 (单行处理)
        遍历maping_rules,在flat_obj中寻找匹配的key，并返回结果"""
        row = {}
        for target_key, strategies in self.mapping_rules.items():
            for strat in strategies:
                mode = strat.get("strategy")
                
                if mode == "direct":
                    src = strat.get("sources")
                    if src in flat_obj:
                        row[target_key] = flat_obj[src]
                        break
                
                elif mode == "composite":
                    srcs = strat.get("sources", [])
                    joiner = strat.get("join_with", " ")
                    if all(k in flat_obj for k in srcs):
                        vals = [str(flat_obj[k]) for k in srcs]
                        row[target_key] = joiner.join(vals)
                        break
        return row

    def process(self):
        #创建基准目录
        if not os.path.exists(self.output_dir):
            os.makedirs(self.output_dir)
        # 1. 创建输出目录
        # project_name=self.file_path.parent.name
        # file_name=self.file_path.stem
        # #为每一个文件创建一个目录
        # output_dir=os.path.join(project_name,self.output_dir)
        # if not os.path.exists(output_dir):
        #     os.makedirs(output_dir)

        # 2. 发现阶段 (获取 Schema)
        schema_stats = self._discovery_phase()
        
        # 3. 映射阶段 (调用 LLM)
        logger.info("启动阶段二: AI 映射生成")
        
        self.mapping_rules = self.llm_engine.generate_mapping(schema_stats)
        # 保存映射规则以备查
        rule_path = Path(os.path.join(self.output_dir, "mapping_rules.json"))
        with open(rule_path, 'w', encoding='utf-8') as f:
            json.dump(self.mapping_rules, f, ensure_ascii=False, indent=2)

        # 4. ETL 执行阶段
        logger.info("启动阶段三: ETL 转换与输出")
        output_csv = Path(os.path.join(self.output_dir, f"{self.file_path.stem}_parsed.csv"))
        
        # 获取 CSV 表头 
        headers =["id_card","user_name","phone","gender","address","url_and_addresses","birthday","age",
                                "country","Province","city","ip","postal_code","job_position","major","school","work_address"] 
        
        try:
            with open(output_csv, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=headers, extrasaction='ignore')
                writer.writeheader()
                
                count = 0
                # 重新全量读取文件进行转换
                for obj in stream_read_json(self.file_path):
                    flat_obj = flatten_json(obj)
                    csv_row = self._transform_row(flat_obj)
                    writer.writerow(csv_row)
                    count += 1
                    
            logger.info(f"处理完成，已生成 CSV: {output_csv} (行数: {count})")
            
        except Exception as e:
            logger.error(f"ETL 阶段失败: {e}")
    def detect(self,source_type=_NO_ARG):
        logger.info("启动Json类型字段schema探查阶段")
        result=self._schema_detect()
        if source_type is not _NO_ARG:
            result["source_type"]=source_type
        
        with open(self.detect_path,'a',encoding="utf-8") as f:
            json_str=json.dumps(result,ensure_ascii=False)
            f.write(json_str+'\n')
        logger.info("字段探查结束")