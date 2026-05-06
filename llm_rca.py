import requests
import json
import time

OLLAMA_URL = "http://localhost:11434/api/generate"


def infer_with_llm(logs_text):

    prompt = f"""
You are an SRE system analyzing microservice logs.

Your task is to identify:
1. Root cause
2. Most likely affected service

You MUST choose a service from this list:
frontend, cartservice, redis-cart, checkoutservice,
paymentservice, productcatalogservice, recommendationservice,
shippingservice, currencyservice, emailservice, adservice, unknown

---

STRICT RULES:

- Use signals like HIGH_ERROR_RATE, REDIS_INVOLVED, FRONTEND_INVOLVED
- If REDIS_INVOLVED → choose redis-cart
- If frontend errors + backend hints → choose backend service (cartservice or redis-cart)
- If only frontend errors and no backend evidence → choose frontend
- If logs mention specific service → choose that service
- Avoid "unknown" unless absolutely no signal exists
- Be deterministic and confident

---

Logs:
{logs_text}

---

Return STRICT JSON ONLY:

{{
  "cause": "...",
  "service": "..."
}}
"""

    for attempt in range(3):

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

            # safe JSON extraction
            start = text.find("{")
            end = text.rfind("}") + 1

            data = json.loads(text[start:end])

            service = data.get("service", "unknown")

            # 🔴 HARD VALIDATION (important)
            allowed_services = {
                "frontend", "cartservice", "redis-cart",
                "checkoutservice", "paymentservice",
                "productcatalogservice", "recommendationservice",
                "shippingservice", "currencyservice",
                "emailservice", "adservice", "unknown"
            }

            if service not in allowed_services:
                service = "unknown"

            return {
                "cause": data.get(
                    "cause",
                    "Possible service failure"
                ),
                "service": service
            }

        except Exception as e:

            print(f"LLM error (attempt {attempt+1}):", e)
            time.sleep(2)

    return {
        "cause": "Possible dependency failure (LLM unavailable)",
        "service": "unknown"
    }