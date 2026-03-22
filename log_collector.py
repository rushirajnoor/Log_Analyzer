import requests

def follow(file):
    file.seek(0, 2)
    while True:
        line = file.readline()
        if not line:
            continue
        yield line

with open("app.log", "r") as logfile:
    for line in follow(logfile):
        log_data = {"message": line.strip()}
        requests.post("http://127.0.0.1:8000/logs", json=log_data)