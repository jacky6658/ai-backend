@app.post("/generate")
def generate():
    try:
        data = request.get_json(force=True) or {}
        topic = (data.get("topic") or "").strip()
        action = (data.get("action") or "copywriting").strip()
        user_level = (data.get("user_level") or "beginner").strip()

        if not topic:
            return jsonify({"error": "請提供 topic"}), 400
        if len(topic) > 300:
            return jsonify({"error": "topic 過長，請精簡至 300 字內"}), 400

        user_id = session.get("user_id") or ("u_" + uuid.uuid4().hex[:10])
        session["user_id"] = user_id

        # 依等級稍微調整輸出長度，避免過長導致延遲
        max_tok = 800 if user_level == "advanced" else (700 if user_level == "intermediate" else 600)

        system_hint = (
            "你是短影音文案/腳本教練，輸出精煉、結構清晰。"
            "避免過多贅詞，給出可直接使用的段落。"
        )
        user_prompt = (
            f"主題：{topic}\n"
            f"任務：{('腳本設計' if action=='scriptwriting' else '文案撰寫')}\n"
            f"對象等級：{user_level}\n"
            "請用繁體中文輸出。"
        )

        model = genai.GenerativeModel(
            os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            generation_config={"max_output_tokens": max_tok, "candidate_count": 1, "temperature": 0.8}
        )

        def run_once():
            chat = model.start_chat(history=[])
            return chat.send_message(f"{system_hint}\n\n{user_prompt}")

        try:
            resp = run_once()
        except Exception:
            # 遇到偶發錯誤/逾時，再嘗試一次
            resp = run_once()

        result = getattr(resp, "text", None) or ""
        if not result:
            return jsonify({"error": "模型沒有回覆內容"}), 502

        save_conversation(user_id, topic, action, user_level, user_prompt, result)

        return jsonify({
            "message": {"content": result},
            "user_id": user_id,
            "user_level": user_level,
            "timestamp": datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({"error": f"server_error: {str(e)}"}), 500
