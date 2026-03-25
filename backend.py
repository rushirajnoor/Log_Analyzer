from fastapi import FastAPI
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.orm import declarative_base, sessionmaker

# --- DB CONFIG ---
DATABASE_URL = "postgresql://loguser:password@localhost:5432/logdb"

engine = create_engine(DATABASE_URL, echo=True)  # echo=True to debug SQL
SessionLocal = sessionmaker(bind=engine)

Base = declarative_base()


# --- TABLE MODEL ---
class LogDB(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(Float)
    level = Column(String)
    service = Column(String)
    message = Column(String)


# --- CREATE TABLE (IMPORTANT) ---
Base.metadata.create_all(bind=engine)


# --- FASTAPI ---
app = FastAPI()


# --- SCHEMA ---
class Log(BaseModel):
    timestamp: float
    level: str
    service: str
    message: str


# --- INSERT LOG ---
@app.post("/logs")
async def receive_log(log: Log):
    db = SessionLocal()

    db_log = LogDB(
        timestamp=log.timestamp,
        level=log.level,
        service=log.service,
        message=log.message
    )

    db.add(db_log)
    db.commit()
    db.close()

    return {"status": "ok"}


# --- GET LOGS ---
@app.get("/logs")
def get_logs():
    db = SessionLocal()
    results = db.query(LogDB).all()
    db.close()

    return [
        {
            "id": r.id,
            "timestamp": r.timestamp,
            "level": r.level,
            "service": r.service,
            "message": r.message
        }
        for r in results
    ]