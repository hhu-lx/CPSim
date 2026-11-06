# src/simulation/orchestrator.py
import argparse
import time
from pathlib import Path
from src.simulation.engine import SimulationEngine
from src.loaders.config_loader import load_platform_config, resolve_path
from src.loaders.excel_loader import load_tasks_from_excel


def main():
    parser = argparse.ArgumentParser(
        description="Computing-Power Synergy simulation platform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # Implementation note.
    parser.add_argument("--config", type=str, default=None,
                       help="Configuration file path")
    parser.add_argument("--excel", type=str, default=None,
                       help="Task data Excel file")
    parser.add_argument("--load", type=str, default=None,
                       help="Load CSV file")
    parser.add_argument("--pv", type=str, default=None,
                       help="PV CSV file")
    parser.add_argument("--wind", type=str, default=None,
                       help="Wind CSV file")
    parser.add_argument("--user-config", type=str, default=None,
                       help="Frontend user JSON configuration")
    parser.add_argument("--stats-interval", type=int, default=None,
                       help="Statistics interval in seconds")
    parser.add_argument("--speed", type=float, default=None,
                       help="Output delay in seconds")
    args = parser.parse_args()

    print("=" * 60)
    print("      Computing-Power Synergy Platform - Configuration Driven    ")
    print("=" * 60)

    # Implementation note.
    try:
        config = load_platform_config(args.config)
    except FileNotFoundError:
        print("Configuration file not found; using defaults")
        config = {}

    # Implementation note.
    tasks = []
    excel_file = args.excel or config.get('tasks', {}).get('excel_file')
    excel_file = resolve_path(excel_file) if excel_file else None
    default_region = config.get('tasks', {}).get('default_region', 'region_a')
    if excel_file:
        try:
            tasks = load_tasks_from_excel(excel_file, region=default_region)
        except FileNotFoundError as e:
            print(f"{e}")
    else:
        try:
            tasks = load_tasks_from_excel(region=default_region)
        except FileNotFoundError:
            print("Task data file not found")

    print(f"Loaded tasks: {len(tasks)}")

    # Implementation note.
    print("\nInitializing simulation engine...")
    engine = SimulationEngine(
        config_file=args.config,
        time_scale=args.speed,
        load_file=args.load,
        pv_file=args.pv,
        wind_file=args.wind,
        user_config_file=args.user_config,
        stats_interval_seconds=args.stats_interval
    )

    # Implementation note.
    for task in tasks:
        engine.submit_task(task)
    print(f"Submitted {len(tasks)} tasks")
    engine.start()

    print("\nSimulation running...")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        print("\nSimulation stopped")


if __name__ == "__main__":
    main()
