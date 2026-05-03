import requests
import json
import time

OLLAMA_URL = "http://localhost:11434/api/generate"


def infer_with_llm(logs_text):

    prompt = f"""
You are an SRE system analyzing logs.

Your job is ONLY to identify the root cause.

Use provided signals and logs carefully.

Return STRICT JSON:
- cause: short and precise root cause (1 sentence)
- service: most likely affected service (frontend, cartservice, redis-cart, paymentservice, unknown)

RULES:
- Use signals like HIGH_ERROR_RATE, REDIS_INVOLVED, FRONTEND_INVOLVED
- If Redis appears in logs → likely redis-cart issue
- If frontend failing → often dependency issue
- Avoid vague answers like "Unknown issue"
- Be specific and deterministic

Logs:
{logs_text}

Output:
{{
  "cause": "...",
  "service": "..."
}}
"""

    for attempt in range(3):  # retry logic

        try:

            response = requests.post(
                OLLAMA_URL,
                json={
                    "model": "qwen2.5:3b",
                    "prompt": prompt,
                    "stream": False
                },
                timeout=30
            )

            text = response.json()["response"].strip()

            # extract JSON safely
            start = text.find("{")
            end = text.rfind("}") + 1

            data = json.loads(text[start:end])

            return {
                "cause": data.get(
                    "cause",
                    "Possible service failure"
                ),
                "service": data.get(
                    "service",
                    "unknown"
                )
            }

        except Exception as e:

            print(
              f"LLM error (attempt {attempt+1}):",
              e
            )

            time.sleep(2)


    # fallback after retries fail
    return {
        "cause":
        "Possible dependency failure (LLM unavailable)",
        "service":
        "unknown"
    }