from fastapi import FastAPI

app = FastAPI()

logs = []

@app.post("/logs")
async def receive_log(log: dict):
    logs.append(log)
    print("Received:", log)
    return {"status": "ok"}

@app.get("/logs")
def get_logs():
    return logs