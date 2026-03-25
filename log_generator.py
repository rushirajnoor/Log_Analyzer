import time
import random

levels = ["INFO", "WARNING", "ERROR"]

while True:
    level = random.choice(levels)

    if level == "INFO":
        msg = "User login successful"
    elif level == "WARNING":
        msg = "High memory usage detected"
    else:
        msg = "Database connection failed"

    log = f"{level}: {msg}"

    with open("app.log", "a") as f:
        f.write(log + "\n")

    print(log)
    time.sleep(1)