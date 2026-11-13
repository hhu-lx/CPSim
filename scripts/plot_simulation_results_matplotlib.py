"""Redraw the simulation result charts with Matplotlib."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd
from matplotlib.ticker import MultipleLocator, StrMethodFormatter


FONT_SIZE = 50
LEGEND_KW = {
    "fontsize": FONT_SIZE,
    "handlelength": 1.2,
    "handletextpad": 0.5,
    "labelspacing": 0.35,
    "borderpad": 0.3,
}


def configure_fonts() -> None:
    plt.rcParams.update(
        {
            "font.family": "Times New Roman",
            "font.size": FONT_SIZE,
            "axes.titlesize": FONT_SIZE,
            "axes.labelsize": FONT_SIZE,
            "xtick.labelsize": FONT_SIZE,
            "ytick.labelsize": FONT_SIZE,
            "legend.fontsize": FONT_SIZE,
            "legend.handlelength": 1.2,
            "axes.edgecolor": "black",
            "axes.labelcolor": "black",
            "xtick.color": "black",
            "ytick.color": "black",
            "text.color": "black",
        }
    )


def integer_ticks(axis, step: int) -> None:
    axis.yaxis.set_major_locator(MultipleLocator(step))
    axis.yaxis.set_major_formatter(StrMethodFormatter("{x:.0f}"))


def time_axis(axis, hours: pd.Series) -> None:
    """Use elapsed hours on every chart, matching the project's SVG reports."""
    axis.set_xlim(0, 24)
    axis.xaxis.set_major_locator(MultipleLocator(6))
    axis.xaxis.set_major_formatter(StrMethodFormatter("{x:.0f}"))


def generation_stack_data(data: pd.DataFrame) -> dict[str, pd.Series]:
    """Group generation with renewables on top and signed residual sources below."""
    zero = pd.Series(0.0, index=data.index)
    storage = data.get("storage_power_mw", zero).fillna(0)
    thermal = data.get("base_generation_mw", zero).fillna(0) + data.get("conventional_generation_mw", zero).fillna(0)
    others = data.get("grid_import_mw", zero).fillna(0) + storage
    return {
        "Others": others,
        "Thermal Power Generation": thermal.clip(lower=0),
        "Wind": data["wind_generation_mw"].fillna(0).clip(lower=0),
        "PV": data["pv_generation_mw"].fillna(0).clip(lower=0),
    }


def save_generation_chart(data: pd.DataFrame, output: Path) -> None:
    hours = data["second"] / 3600.0
    grouped = generation_stack_data(data)
    labels = ["Others", "Thermal Power Generation", "Wind", "PV"]
    values = [grouped[label] for label in labels]
    # Palette aligned with the supplied architecture figure: light blue,
    # green, warm yellow, and peach with a dark blue load curve.
    colors = ["#f3c7a1", "#8fc0e3", "#8fc27a", "#ffd66b"]

    figure, axis = plt.subplots(figsize=(24, 16))
    figure.subplots_adjust(left=0.12, right=0.88, bottom=0.15, top=0.72)
    axis.stackplot(hours, values, labels=labels, colors=colors, alpha=0.82)
    axis.plot(hours, data["total_load"].fillna(0), color="#8b5a2b", linewidth=3.5, label="Total Load")
    axis.set_xlabel("Time (h)")
    axis.set_ylabel("Power (MW)")
    integer_ticks(axis, 2000)
    time_axis(axis, hours)
    axis.grid(axis="y", color="#d5dbe3", linewidth=1.0)
    axis.legend(**LEGEND_KW, ncol=2, loc="lower left", bbox_to_anchor=(0, 1.03), frameon=False)
    figure.savefig(output, dpi=180, facecolor="white")
    plt.close(figure)


def save_storage_chart(data: pd.DataFrame, output: Path) -> None:
    hours = data["second"] / 3600.0
    figure, load_axis = plt.subplots(figsize=(24, 16))
    figure.subplots_adjust(left=0.12, right=0.88, bottom=0.15, top=0.72)
    storage_axis = load_axis.twinx()

    wind = data["wind_available_mw"].fillna(0).clip(lower=0)
    pv = data["pv_available_mw"].fillna(0).clip(lower=0)
    load_axis.stackplot(
        hours,
        wind,
        pv,
        labels=["Wind Input", "PV Input"],
        colors=["#79a95b", "#f4c84f"],
        alpha=0.60,
        linewidth=0.8,
    )
    load_axis.plot(hours, data["net_load_mw"], color="#8b5a2b", linewidth=4.0, label="Residual Load", zorder=4)

    storage_power = data["storage_power_mw"].fillna(0)
    discharge = storage_power.clip(lower=0)
    charge = storage_power.clip(upper=0)
    bar_width = max(0.005, 24.0 / max(len(data), 1) * 0.82)
    storage_axis.bar(
        hours,
        discharge,
        width=bar_width,
        color="#c96b3b",
        alpha=0.38,
        edgecolor="none",
        label="Storage Discharge",
        align="center",
        zorder=1,
    )
    storage_axis.bar(
        hours,
        charge,
        width=bar_width,
        color="#6f9fcf",
        alpha=0.78,
        edgecolor="none",
        label="Storage Charge",
        align="center",
        zorder=1,
    )

    load_axis.set_xlabel("Time (h)")
    load_axis.set_ylabel("Residual Load (MW)")
    storage_axis.set_ylabel("Storage Power (MW)")
    max_load = float(pd.concat([wind + pv, data["net_load_mw"].abs()]).max())
    load_axis.set_ylim(-max_load, max_load)
    storage_axis.set_ylim(-50, 50)
    integer_ticks(load_axis, 1000)
    integer_ticks(storage_axis, 25)
    time_axis(load_axis, hours)
    storage_axis.set_xlim(load_axis.get_xlim())
    load_axis.grid(axis="y", color="#d5dbe3", linewidth=1.0)

    handles_left, labels_left = load_axis.get_legend_handles_labels()
    handles_right, labels_right = storage_axis.get_legend_handles_labels()
    left_by_label = dict(zip(labels_left, handles_left))
    right_by_label = dict(zip(labels_right, handles_right))
    legend_labels = ["Residual Load", "Wind Input", "PV Input", "Storage Discharge", "Storage Charge"]
    legend_handles = [left_by_label[label] if label in left_by_label else right_by_label[label] for label in legend_labels]
    load_axis.legend(
        legend_handles,
        legend_labels,
        **LEGEND_KW,
        ncol=3,
        loc="lower left",
        bbox_to_anchor=(0, 1.03),
        frameon=False,
    )
    figure.savefig(output, dpi=180, facecolor="white")
    plt.close(figure)


def save_task_power_chart(data: pd.DataFrame, output: Path) -> None:
    hours = data["second"] / 3600.0
    figure, axis = plt.subplots(figsize=(24, 16))
    figure.subplots_adjust(left=0.12, right=0.88, bottom=0.15, top=0.72)
    axis.plot(hours, data["planned_task_power_mw"], linewidth=3.0, color="#e8a15a", label="Planned Task Power")
    axis.plot(hours, data["actual_task_power_mw"], linewidth=3.0, color="#79a95b", label="Actual Task Power")
    axis.set_xlabel("Time (h)")
    axis.set_ylabel("Power (MW)")
    integer_ticks(axis, 50)
    time_axis(axis, hours)
    axis.grid(axis="y", color="#d5dbe3", linewidth=1.0)
    axis.legend(**LEGEND_KW, ncol=2, loc="lower left", bbox_to_anchor=(0, 1.03), frameon=False)
    figure.savefig(output, dpi=180, facecolor="white")
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description="Redraw simulation charts with Matplotlib")
    parser.add_argument("input_csv", type=Path, help="simulation-result.csv from the exported ZIP")
    parser.add_argument("--output-dir", type=Path, default=Path("matplotlib_redraw"))
    args = parser.parse_args()

    configure_fonts()
    data = pd.read_csv(args.input_csv)
    required = {
        "second", "total_load", "total_generation", "base_generation_mw",
        "conventional_generation_mw", "wind_generation_mw", "pv_generation_mw",
        "grid_import_mw", "storage_discharge_mw", "wind_available_mw",
        "pv_available_mw", "net_load_mw", "storage_power_mw",
        "planned_task_power_mw", "actual_task_power_mw",
    }
    missing = sorted(required.difference(data.columns))
    if missing:
        raise ValueError(f"Missing required columns: {', '.join(missing)}")

    data = data.sort_values("second").reset_index(drop=True)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    save_generation_chart(data, args.output_dir / "generation_24h_matplotlib.png")
    save_storage_chart(data, args.output_dir / "storage_behavior_matplotlib.png")
    save_task_power_chart(data, args.output_dir / "task_power_matplotlib.png")
    print(f"Saved Matplotlib charts to {args.output_dir.resolve()}")


if __name__ == "__main__":
    main()
