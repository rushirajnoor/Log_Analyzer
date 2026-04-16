import requests
import json

OLLAMA_URL = "http://localhost:11434/api/generate"


def infer_with_llm(logs_text):
    prompt = f"""
You are an autonomous SRE agent.

Analyze the logs and return STRICT JSON with:
- cause: short root cause
- service: one of [redis, fastapi, postgres, unknown]
- steps: list of shell commands to fix the issue

STRICT RULES:
- If Redis issue → steps MUST include: docker restart redis
- If FastAPI issue → steps MUST include: docker restart fastapi-app
- If Postgres issue → steps MUST include: docker restart postgres
- If system is healthy → steps = []
- NEVER return empty steps if there is an error
- Output ONLY valid JSON (no explanation)

Logs:
{logs_text}

Output format:
{{
  "cause": "...",
  "service": "...",
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

        # --- Extract JSON safely ---
        start = text.find("{")
        end = text.rfind("}") + 1

        if start == -1 or end == -1:
            raise ValueError("No JSON found in LLM response")

        json_text = text[start:end]
        data = json.loads(json_text)

        # --- Extract fields ---
        cause = data.get("cause", "Unknown issue")
        service = data.get("service", "unknown")
        steps = data.get("steps", [])

        # 🔥 HARD FALLBACK (critical for reliability)
        cause_lower = cause.lower()
        service_lower = service.lower()

        if not steps:
            if "redis" in cause_lower or "redis" in service_lower:
                steps = ["docker restart redis"]

            elif "fastapi" in cause_lower or "fastapi" in service_lower:
                steps = ["docker restart fastapi-app"]

            elif "postgres" in cause_lower or "database" in cause_lower:
                steps = ["docker restart postgres"]

        return {
            "cause": cause,
            "service": service,
            "steps": steps
        }

    except Exception as e:
        print("LLM error:", e)

        return {
            "cause": "Unknown issue",
            "service": "unknown",
            "steps": []
        }