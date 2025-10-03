from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
import os

app = Flask(__name__)
CORS(app)

# 設定 Gemini API
GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', 'your-api-key-here')
genai.configure(api_key=GEMINI_API_KEY)

@app.route('/')
def home():
    return "AI Backend is running with Gemini!"

@app.route('/generate', methods=['POST'])
def generate():
    try:
        data = request.json
        topic = data.get('topic', '')
        
        model = genai.GenerativeModel('gemini-pro')
        response = model.generate_content(f"為主題「{topic}」生成一個吸引人的文案")
        
        return jsonify({"result": response.text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
