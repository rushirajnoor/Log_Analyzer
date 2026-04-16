import requests
import json

OLLAMA_URL = "http://localhost:11434/api/generate"


def infer_with_llm(logs_text):
    prompt = f"""
You are an autonomous SRE agent.

Analyze the logs and ALWAYS return:
- cause: short root cause
- steps: list of commands

STRICT RULES:
- If Redis issue → steps MUST include: docker restart redis
- If system is healthy → steps = []
- Output STRICT JSON ONLY

Logs:
{logs_text}

Output:
{{
  "cause": "...",
  "steps": ["cmd1"]
}}
"""

    try:
        response = requests.post(
            OLLAMA_URL,
            json={
                "model": "qwen2.5:3b",
                "prompt": prompt,
                "stream": False
            },
            timeout=20
        )

        text = response.json()["response"].strip()

        start = text.find("{")
        end = text.rfind("}") + 1

        json_text = text[start:end]
        data = json.loads(json_text)

        cause = data.get("cause", "Unknown")
        steps = data.get("steps", [])

        # 🔥 FIXED FALLBACK (based on cause)
        if not steps:
            cause_lower = cause.lower()

            if "redis" in cause_lower:
                steps = ["docker restart redis"]

        return {
            "cause": cause,
            "steps": steps
        }

    except Exception as e:
        print("LLM error:", e)
        return {
            "cause": "Unknown issue",
            "steps": []
        }