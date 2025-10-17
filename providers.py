from typing import Generator, Dict, Any
import json
import time

class LLMProvider:
    def __init__(self, provider_type: str = "local"):
        self.provider_type = provider_type
    
    def stream_response(self, messages: list, **kwargs) -> Generator[Dict[str, Any], None, None]:
        """串流回應"""
        if self.provider_type == "local":
            yield from self._local_stream(messages, **kwargs)
        elif self.provider_type == "openai":
            yield from self._openai_stream(messages, **kwargs)
        elif self.provider_type == "gemini":
            yield from self._gemini_stream(messages, **kwargs)
        else:
            raise ValueError(f"不支援的provider: {self.provider_type}")
    
    def _local_stream(self, messages: list, **kwargs) -> Generator[Dict[str, Any], None, None]:
        """本地假模型串流"""
        # 模擬完整回應
        full_response = self._generate_local_response(messages, **kwargs)
        
        # 逐token輸出 - 智能加速
        tokens = list(full_response)
        for i, token in enumerate(tokens):
            yield {
                "type": "content",
                "token": token
            }
            
            # 智能延遲：開頭慢一點，中間快，結尾稍慢
            if i < 10:  # 前10個字符稍慢，讓用戶看到開始
                time.sleep(0.01)
            elif i < len(tokens) - 10:  # 中間部分最快
                time.sleep(0.003)
            else:  # 結尾稍慢，讓用戶看到完成
                time.sleep(0.008)
        
        yield {"type": "done"}
    
    def _generate_local_response(self, messages: list, **kwargs) -> str:
        """生成本地回應"""
        # 從kwargs獲取參數
        agent = kwargs.get('agent', 'script_generation')
        topic = kwargs.get('topic', '')
        template = kwargs.get('template', 'A')
        platform = kwargs.get('platform', 'Reels')
        duration = kwargs.get('duration', '30')
        
        # 根據模板生成腳本
        if agent == "script_generation":
            return self._generate_script(topic, template, platform, duration)
        else:
            return f"這是{agent}的回應，主題：{topic}"
    
    def _generate_script(self, topic: str, template: str, platform: str, duration: str) -> str:
        """生成腳本"""
        templates = {
            "A": "標準行銷三段式（Hook → Value → CTA）",
            "B": "問題 → 解決 → 證明（Problem → Solution → Proof）",
            "C": "Before → After → 秘密揭露",
            "D": "教學知識型（迷思 → 原理 → 要點 → 行動）",
            "E": "故事敘事型（起 → 承 → 轉 → 合）"
        }
        
        platforms = {
            "Reels": "自然、生活化、強情緒；30s內最穩",
            "TikTok": "節奏更快、梗感強；字卡與反差戲劇化",
            "小紅書": "審美/文案同理心；畫面乾淨、字幕精修",
            "YouTube Shorts": "高品質內容，適合教學和深度內容"
        }
        
        script = f"""# {topic} - {templates[template]} 腳本

**平台：** {platforms[platform]}
**時長：** {duration}秒

## 腳本結構

### Hook (0-5秒)
「你知道為什麼{topic}總是沒效果嗎？」
- 直擊痛點，吸睛開場
- 使用問句或反差手法

### Value (5-{int(duration)*0.8}秒)
核心價值內容：
1. 機制原理說明
2. 具體步驟方法  
3. 真實見證效果

### CTA ({int(duration)*0.8}-{duration}秒)
「記得關注收藏，獲取更多{topic}技巧」
- 明確行動指引
- 關注、留言或購買連結

## 拍攝要點
- 鏡頭：CU/MCU/MS/WS交替
- 節奏：2-3秒換畫面
- 字幕：關鍵詞加粗放大
- 聲音：乾淨收音，重點加強

## 分鏡腳本
```json
{{
  "segments": [
    {{"type": "hook", "start_sec": 0, "end_sec": 5, "camera": "CU", "dialog": "你知道為什麼{topic}總是沒效果嗎？", "visual": "大字卡+表情特寫"}},
    {{"type": "value", "start_sec": 5, "end_sec": {int(duration)*0.8}, "camera": "MS", "dialog": "核心內容講解", "visual": "產品展示+字幕"}},
    {{"type": "cta", "start_sec": {int(duration)*0.8}, "end_sec": {duration}, "camera": "WS", "dialog": "記得關注收藏", "visual": "品牌logo"}}
  ]
}}
```"""
        
        return script
    
    def _openai_stream(self, messages: list, **kwargs) -> Generator[Dict[str, Any], None, None]:
        """OpenAI串流（預留）"""
        # TODO: 實現OpenAI API串流
        yield {"type": "error", "message": "OpenAI provider 尚未實現"}
    
    def _gemini_stream(self, messages: list, **kwargs) -> Generator[Dict[str, Any], None, None]:
        """Gemini串流（預留）"""
        # TODO: 實現Gemini API串流
        yield {"type": "error", "message": "Gemini provider 尚未實現"}
