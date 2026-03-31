from fastapi import FastAPI
import redis
import time
import logging

app = FastAPI()

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

def get_redis():
    return redis.Redis(host="redis", port=6379, socket_connect_timeout=1)

@app.get("/")
def root():
    try:
        r = get_redis()
        r.ping()
        logging.info("Connected to Redis successfully")
        return {"status": "ok"}
    except Exception as e:
        logging.error(f"Redis connection failed: {str(e)}")
        return {"status": "error"}


# Continuous logging loop (important)
while True:
    try:
        r = get_redis()
        r.ping()
        logging.info("Heartbeat: Redis OK")
    except Exception as e:
        logging.error(f"Heartbeat: Redis failure - {str(e)}")

    time.sleep(2)