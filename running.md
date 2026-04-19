# Log Analyzer Startup Guide

> Full stack with Kubernetes, Logs, Metrics, RCA, Remediation, Verification, and Dashboard

## 📋 Startup Order

### 1. Docker

```bash
sudo systemctl start docker
```

### 2. Minikube

```bash
minikube start --driver=docker
```

### 3. Metrics Server

Check if metrics are available:

```bash
kubectl top pods
```

If it fails, enable metrics-server addon:

```bash
minikube addons enable metrics-server
```

Verify again:

```bash
kubectl top pods
```

### 4. Verify Online Boutique

```bash
kubectl get pods
```

### 5. Frontend Traffic (Optional)

Open a new terminal and run:

```bash
kubectl port-forward deployment/frontend 8080:8080
```

Then visit `http://localhost:8080` to generate traffic.

### 6. PostgreSQL

```bash
sudo systemctl start postgresql
```

### 7. Backend API

Open a new terminal and run:

```bash
uvicorn backend:app --reload
```

Leave this running in the terminal.

### 8. Log Collector

Open a new terminal and run:

```bash
python log_collector.py
```

### 9. Auto Remediation

Open a new terminal and run:

```bash
python auto_remediation.py
```

This includes:

- Metrics check
- Health check
- RCA
- Remediation
- Verification

### 10. Dashboard

Open a new terminal and run:

```bash
streamlit run dashboard.py
```

Then visit `http://localhost:8501`

---

## 🔥 Quick Reference

**Full startup sequence:**

1. Docker
2. Minikube
3. Metrics Server
4. `kubectl get pods`
5. Port-forward (optional)
6. PostgreSQL
7. Backend
8. Log Collector
9. Auto Remediation
10. Dashboard

**Final Stack:**

- Kubernetes
- Logs
- Metrics
- RCA
- Remediation
- Verification
- Dashboard

---

## 🧪 Testing

### Service Failure Test

Scale down cartservice:

```bash
kubectl scale deployment cartservice --replicas=0
```

### Metrics Test

Generate load and monitor metrics:

```bash
kubectl top pods
```

Watch the output from `auto_remediation.py` for metrics and remediation actions.

---

## 🛑 Shutdown Order

Kill long-running processes:

```bash
pkill -f log_collector
pkill -f auto_remediation
```

Stop other services by pressing `Ctrl+C` in their respective terminals:

- Streamlit dashboard
- Backend API
- Port-forward (if running)

Finally, stop Minikube:

```bash
minikube stop
```


TRUNCATE logs RESTART IDENTITY;
TRUNCATE remediation_history RESTART IDENTITY;

