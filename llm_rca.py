import requests

OLLAMA_URL = "http://localhost:11434/api/generate"


def infer_with_llm(logs_text):
    prompt = f"""
You are an expert SRE system.

Analyze the logs and identify the root cause in ONE short sentence.

Logs:
{logs_text}

Root cause:
"""

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": "qwen2.5:3b",
                "prompt": prompt,
                "stream": False
            },
            timeout=10
        )

        return response.json()["response"].strip()

    except Exception as e:
        print("LLM error:", e)
        return "Unknown issue"