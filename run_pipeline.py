"""
Run the Neural Network Log Analyzer data pipeline.

Usage:
    python3 run_pipeline.py              # Quick test (first 2000 lines)
    python3 run_pipeline.py --full       # Full dataset (~140k lines)
    python3 run_pipeline.py --lines 5000 # Custom line limit
"""

import argparse
import logging
import sys
import os

# Ensure project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)-7s | %(message)s",
)

from nn_pipeline.data.dataset import build_pipeline


def main():
    parser = argparse.ArgumentParser(description="Run the NN Log Analyzer data pipeline")
    parser.add_argument("--full", action="store_true", help="Process all lines (slow)")
    parser.add_argument("--lines", type=int, default=2000, help="Max lines to parse (default: 2000)")
    args = parser.parse_args()

    max_lines = None if args.full else args.lines

    print()
    print("╔══════════════════════════════════════════════════════════╗")
    print("║     Neural Network Log Analyzer — Data Pipeline         ║")
    print("╚══════════════════════════════════════════════════════════╝")
    print()

    result = build_pipeline(max_lines=max_lines)

    print()
    print(result.summary())

    # Show a few sample windows
    print()
    print("═══ Sample Windows ═══")
    for i, (window, label) in enumerate(zip(result.windows[:5], result.labels[:5])):
        print(
            f"  Window {window.window_id:>3} | "
            f"{window.size:>3} entries | "
            f"{window.duration:>6.1f}s | "
            f"severity={label.severity_name:<8} | "
            f"fault={label.fault_name:<20} | "
            f"conf={label.confidence:.2f}"
        )
        if label.evidence:
            print(f"             └─ {label.evidence[0]}")

    # Show template samples
    templates = result.template_extractor.get_all_templates()
    print()
    print(f"═══ Sample Templates ({len(templates)} total) ═══")
    for tid, tstr in list(templates.items())[:10]:
        print(f"  [{tid:>3}] {tstr[:90]}")

    print()
    print("✅ Pipeline complete!")
    print(f"   {len(result.train_indices)} train / {len(result.val_indices)} val / {len(result.test_indices)} test windows")
    print()


if __name__ == "__main__":
    main()