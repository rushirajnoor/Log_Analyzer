import argparse
import subprocess
import sys
import select

LOG_FILE = "raw_logs.txt"
APP_PATH = "docker_app/app.py"
PYTHON_COMMAND = sys.executable

parser = argparse.ArgumentParser(description="Capture raw logs for the website demo.")
parser.add_argument(
    "--pod",
    nargs="+",
    help="If provided, capture logs from one or more Kubernetes pods instead of running the local app.",
)
parser.add_argument(
    "--namespace",
    default="default",
    help="Kubernetes namespace to use when reading pod logs. Defaults to default.",
)
args = parser.parse_args()


def list_pods(namespace):
    out = subprocess.check_output(
        ["kubectl", "get", "pods", "-n", namespace, "-o", "jsonpath={.items[*].metadata.name}"],
        text=True,
    )
    return out.split()


def resolve_pod_names(selectors, namespace):
    pods = list_pods(namespace)
    resolved = []

    for selector in selectors:
        exact = [p for p in pods if p == selector]
        if exact:
            resolved.extend(exact)
            continue

        starts = [p for p in pods if p.startswith(selector)]
        if starts:
            resolved.extend(starts)
            continue

        contains = [p for p in pods if selector in p]
        if contains:
            resolved.extend(contains)
            continue

        print(f"Warning: no pod matched selector '{selector}' in namespace '{namespace}'", file=sys.stderr)

    return list(dict.fromkeys(resolved))


print("Press Ctrl+C to stop and keep the file for review.")

with open(LOG_FILE, "w", encoding="utf-8") as out_file:
    if args.pod:
        pod_names = resolve_pod_names(args.pod, args.namespace)
        if not pod_names:
            raise SystemExit(
                "No matching pods found. Run `kubectl get pods -n <namespace>` and use exact or prefix pod names."
            )

        print(
            f"Following raw pod logs from {pod_names} in namespace '{args.namespace}' and writing to {LOG_FILE}..."
        )

        processes = []
        for pod in pod_names:
            process = subprocess.Popen(
                ["kubectl", "logs", "-f", "-n", args.namespace, pod],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            processes.append((pod, process))

        pipes = [proc.stdout for _, proc in processes if proc.stdout is not None]

        try:
            while pipes:
                ready, _, _ = select.select(pipes, [], [], 1)
                for pipe in ready:
                    line = pipe.readline()
                    if not line:
                        pipes = [p for p in pipes if p is not pipe]
                        continue

                    pod_name = next((pod for pod, proc in processes if proc.stdout is pipe), "pod")
                    raw_line = line.rstrip("\n")
                    formatted = f"[{pod_name}] {raw_line}"
                    print(formatted)
                    out_file.write(formatted + "\n")
                    out_file.flush()
        except KeyboardInterrupt:
            print("\nStopping log capture...")
        finally:
            for _, proc in processes:
                proc.terminate()
                proc.wait()

    else:
        print(f"Starting local website app and writing raw logs to {LOG_FILE}...")
        process = subprocess.Popen(
            [PYTHON_COMMAND, APP_PATH],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

        try:
            for line in process.stdout:
                raw_line = line.rstrip("\n")
                print(raw_line)
                out_file.write(raw_line + "\n")
                out_file.flush()
        except KeyboardInterrupt:
            print("\nStopping log capture...")
        finally:
            process.terminate()
            process.wait()

    print(f"Raw logs saved to {LOG_FILE}")
