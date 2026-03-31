import pandas as pd
from sqlalchemy import create_engine

# --- DB CONNECTION ---
engine = create_engine("postgresql://loguser:password@localhost:5432/logdb")


# --- STEP 1: Get latest timestamp ---
def get_latest_timestamp():
    query = "SELECT MAX(timestamp) as ts FROM logs;"
    df = pd.read_sql(query, engine)
    return df['ts'][0]


# --- STEP 2: Fetch logs around anomaly ---
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
def analyze_patterns(df):
    grouped = df.groupby(['level', 'service', 'message']).size().reset_index(name='count')
    return grouped


# --- STEP 4: Weighted RCA (IMPORTANT) ---
def infer_cause(grouped):
    if grouped.empty:
        return "No clear root cause found", grouped

    # Severity weights
    weights = {
        "ERROR": 3,
        "WARNING": 2,
        "INFO": 1
    }

    # Apply score
    grouped['score'] = grouped.apply(
        lambda row: row['count'] * weights.get(row['level'], 1),
        axis=1
    )

    grouped = grouped.sort_values(by='score', ascending=False)

    top = grouped.iloc[0]
    message = top['message'].lower()

    # Rule-based interpretation
    if "database" in message:
        cause = "Database connectivity issue"
    elif "memory" in message:
        cause = "High memory usage"
    elif "login" in message:
        cause = "Authentication-related activity"
    else:
        cause = "Unknown issue"

    return cause, grouped


# --- STEP 5: Full RCA pipeline ---
def run_rca(timestamp):
    df = get_logs_around(timestamp)

    print("Fetched rows:", len(df))

    if df.empty:
        return {
            "top_patterns": [],
            "inferred_cause": "No logs found"
        }

    grouped = analyze_patterns(df)
    cause, scored = infer_cause(grouped)

    return {
        "top_patterns": scored.head(5).to_dict(orient="records"),
        "inferred_cause": cause
    }


# --- MAIN (TEST) ---
if __name__ == "__main__":
    ts = get_latest_timestamp()

    if ts is None:
        print("No logs in database")
    else:
        result = run_rca(ts)
        print(result)