import os
from typing import Optional

class KnowledgeLoader:
    def __init__(self, data_dir: str = "data"):
        self.data_dir = data_dir
    
    def load_knowledge(self, agent: str) -> Optional[str]:
        """根據agent載入對應的知識庫"""
        agent_to_file = {
            "positioning": "kb_positioning.txt",
            "topic_selection": "kb_topic_selection.txt", 
            "script_generation": "kb_script_generation.txt"
        }
        
        if agent not in agent_to_file:
            return None
        
        file_path = os.path.join(self.data_dir, agent_to_file[agent])
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            print(f"知識庫檔案不存在: {file_path}")
            return None
        except Exception as e:
            print(f"載入知識庫錯誤: {e}")
            return None
