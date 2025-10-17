import os
import re
from typing import List, Dict
from knowledge_loader import KnowledgeLoader

class RAGRetriever:
    def __init__(self):
        self.knowledge_loader = KnowledgeLoader()
    
    def retrieve(self, agent: str, query: str, top_k: int = 3) -> List[str]:
        """檢索相關知識片段"""
        try:
            knowledge = self.knowledge_loader.load_knowledge(agent)
            if not knowledge:
                return []
            
            # 簡單的關鍵詞匹配檢索
            query_words = set(re.findall(r'\w+', query.lower()))
            lines = knowledge.split('\n')
            
            scored_lines = []
            for line in lines:
                if not line.strip() or line.startswith('=') or line.startswith('-'):
                    continue
                
                line_words = set(re.findall(r'\w+', line.lower()))
                score = len(query_words.intersection(line_words))
                if score > 0:
                    scored_lines.append((score, line.strip()))
            
            # 按分數排序並返回前top_k個
            scored_lines.sort(key=lambda x: x[0], reverse=True)
            return [line for _, line in scored_lines[:top_k]]
        
        except Exception as e:
            print(f"RAG檢索錯誤: {e}")
            return []
