import pandas as pd
from sqlalchemy import create_engine

# --- DB CONNECTION (FIXED: using SQLAlchemy, no warnings) ---
engine = create_engine("postgresql://loguser:password@localhost:5432/logdb")


# --- STEP 1: Get latest timestamp (no hardcoding) ---
def get_latest_timestamp():
    query = "SELECT MAX(timestamp) as ts FROM logs;"
    df = pd.read_sql(query, engine)
    return df['ts'][0]


# --- STEP 2: Fetch logs around anomaly (FIXED window: ±5 min) ---
def get_logs_around(timestamp):
    query = f"""
    SELECT *
    FROM logs
    WHERE to_timestamp(timestamp) 
    BETWEEN to_timestamp({timestamp}) - interval '5 minutes'
    AND to_timestamp({timestamp}) + interval '5 minutes';
    """

    df = pd.read_sql(query, engine)
    return df


# --- STEP 3: Analyze patterns ---
def analyze_root_cause(df):
    grouped = df.groupby(['level', 'message']).size().reset_index(name='count')
    grouped = grouped.sort_values(by='count', ascending=False)
    return grouped


# --- STEP 4: Reasoning layer ---
def infer_cause(grouped):
    if grouped.empty:
        return "No clear root cause found"

    # Priority weights
    weights = {
        "ERROR": 3,
        "WARNING": 2,
        "INFO": 1
    }

    grouped['score'] = grouped.apply(
        lambda row: row['count'] * weights.get(row['level'], 1),
        axis=1
    )

    grouped = grouped.sort_values(by='score', ascending=False)

    top = grouped.iloc[0]
    message = top['message'].lower()

    if "database" in message:
        return "Database connectivity issue"
    elif "memory" in message:
        return "High memory usage"
    elif "login" in message:
        return "Authentication-related issue"
    else:
        return "Unknown issue"



# --- STEP 5: Full RCA pipeline ---
def run_rca(timestamp):
    df = get_logs_around(timestamp)

    print("Fetched rows:", len(df))  # debug

    if df.empty:
        return "No logs found in this window"

    grouped = analyze_root_cause(df)
    cause = infer_cause(grouped)

    return {
        "top_patterns": grouped.head(3).to_dict(orient="records"),
        "inferred_cause": cause
    }


# --- MAIN EXECUTION ---
if __name__ == "__main__":
    ts = get_latest_timestamp()

    if ts is None:
        print("No logs in database")
    else:
        result = run_rca(ts)
        print(result)