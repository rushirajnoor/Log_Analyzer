import requests
import json

OLLAMA_URL = "http://localhost:11434/api/generate"


def infer_with_llm(logs_text):
    prompt = f"""
You are an SRE agent.

Analyze logs and return a structured PLAN.

Return STRICT JSON with:
- cause: short root cause
- service: affected service (redis / fastapi / postgres / unknown)
- plan: list of steps

Step types allowed:
- "check_container <name>"
- "restart_container <name>"
- "noop"

RULES:
- If service is down → check_container first
- Only restart if necessary
- If no issue → plan = ["noop"]

Logs:
{logs_text}

Output:
{{
  "cause": "...",
  "service": "...",
  "plan": ["step1", "step2"]
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
        data = json.loads(text[start:end])

        return {
            "cause": data.get("cause", "Unknown"),
            "service": data.get("service", "unknown"),
            "plan": data.get("plan", [])
        }

    except Exception as e:
        print("LLM error:", e)
        return {
            "cause": "Unknown",
            "service": "unknown",
            "plan": ["noop"]
        }