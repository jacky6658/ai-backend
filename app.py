from flask import Flask, request, jsonify
from flask_cors import CORS
import os

app = Flask(__name__)
CORS(app)

@app.route('/')
def home():
    return "AI Backend is running!"

@app.route('/generate', methods=['POST'])
def generate():
    return jsonify({"result": "Hello from AI backend!"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
