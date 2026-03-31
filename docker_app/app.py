import time
import random
import logging

# Logging format
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)

services = ["auth", "db", "api"]

while True:
    level = random.choice(["INFO", "WARNING", "ERROR"])
    service = random.choice(services)

    if level == "INFO":
        logging.info(f"[{service}] User login successful")
    elif level == "WARNING":
        logging.warning(f"[{service}] High memory usage detected")
    elif level == "ERROR":
        logging.error(f"[{service}] Connection failed")

    time.sleep(1)