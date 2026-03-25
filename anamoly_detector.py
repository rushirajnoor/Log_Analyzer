from sklearn.ensemble import IsolationForest
import pandas as pd

# Assume 'features' from previous step
features = pd.read_csv("features.csv")  # or pass directly

model = IsolationForest(contamination=0.1)

model.fit(features)

features['anomaly'] = model.predict(features)

print(features)