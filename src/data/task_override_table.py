"""Create a durable task table with missing fields filled once.

The source workbook is never modified.  The generated CSV/XLSX keeps every
source column and appends canonical platform fields, so subsequent runs read
stable values instead of regenerating them from a random seed.
"""

from pathlib import Path
import random

import pandas as pd

from src.data.synthetic_tasks import SyntheticTaskDataFactory
from src.domain.task import Task


CANONICAL_COLUMNS = (
    "task_id", "data_size_gb", "required_cpu_cores", "required_gpu_count",
    "required_gpu_type", "user_specified_data_center", "submitted_at",
    "duration_seconds", "max_wait_seconds",
)


def generate_realistic_task_profile(row_count: int, *, seed: int = 42) -> list[dict[str, float]]:
    """Create repeatable normal, peak, and recovery arrival periods."""
    rng = random.Random(f"{seed}:workload-profile")
    profile = []
    submitted_at = 0.0
    for index in range(max(0, row_count)):
        if index:
            phase_second = submitted_at % 1200.0
            if 720.0 <= phase_second < 1020.0:
                interval = rng.uniform(2.0, 3.0)   # Implementation note.
            elif phase_second >= 1020.0:
                interval = rng.uniform(7.0, 9.0)   # Implementation note.
            else:
                interval = rng.uniform(4.0, 6.0)   # Implementation note.
            submitted_at += interval
        profile.append({"submitted_at": round(submitted_at, 3)})
    return profile


def _apply_realistic_workload_profile(
    table: pd.DataFrame,
    *,
    ordered_indices: list,
    plan_gpu_col: str | None,
    compute_col: str | None,
    seed: int,
) -> None:
    """Balance arrivals and exclusive GPU use while retaining peak periods."""
    arrivals = generate_realistic_task_profile(len(ordered_indices), seed=seed)
    for position, row_index in enumerate(ordered_indices):
        rng = random.Random(f"{seed}:workload-row:{row_index}")
        original_duration = pd.to_numeric(table.at[row_index, "duration_seconds"], errors="coerce")
        original_duration = float(original_duration) if pd.notna(original_duration) else 180.0
        duration_seconds = round(min(240.0, max(60.0, original_duration * rng.uniform(0.4, 0.6))), 3)

        plan_gpu = pd.to_numeric(table.at[row_index, plan_gpu_col], errors="coerce") if plan_gpu_col else 0.0
        plan_gpu = float(plan_gpu) if pd.notna(plan_gpu) else 0.0
        compute_value = pd.to_numeric(table.at[row_index, compute_col], errors="coerce") if compute_col else 0.0
        compute_value = float(compute_value) if pd.notna(compute_value) else 0.0
        required_compute = plan_gpu * 0.2 if plan_gpu > 0 else max(compute_value / 1000.0, 1.0)
        if plan_gpu <= 0:
            gpu_probability = 0.0
        elif required_compute >= 80:
            gpu_probability = 1.0
        elif required_compute >= 40:
            gpu_probability = 0.70
        elif required_compute >= 20:
            gpu_probability = 0.22
        elif required_compute >= 10:
            gpu_probability = 0.12
        else:
            gpu_probability = 0.03

        table.at[row_index, "submitted_at"] = arrivals[position]["submitted_at"]
        table.at[row_index, "duration_seconds"] = duration_seconds
        table.at[row_index, "required_gpu_count"] = int(rng.random() < gpu_probability)
        table.at[row_index, "max_wait_seconds"] = round(
            min(360.0, max(90.0, duration_seconds * 0.75 + rng.uniform(30.0, 120.0))),
            3,
        )


def create_task_override_table(
    source_path: str | Path,
    output_path: str | Path,
    *,
    seed: int = 42,
    region: str = "region_a",
) -> Path:
    """Complete missing task data and write a reusable override table."""
    source_path = Path(source_path)
    output_path = Path(output_path)
    if source_path.suffix.lower() in {".csv", ".tsv"}:
        table = pd.read_csv(source_path, dtype=str)
    else:
        table = pd.read_excel(source_path, dtype=str)

    completed = {}
    factory = SyntheticTaskDataFactory(seed=seed)
    columns = list(table.columns)
    # Support the letter-labelled export currently used by the source file,
    # while also accepting canonical column names in a prepared table.
    def find(*names):
        lowered = {str(c).strip().lower(): c for c in columns}
        for name in names:
            if name.lower() in lowered:
                return lowered[name.lower()]
        return None

    letter_export = len(columns) > 5 and [str(c).strip() for c in columns[:6]] == ["A", "B", "C", "D", "E", "F"]
    start_col = find("start_time", "starttime", "开始时间", "submitted_at", "提交时间") or (columns[4] if letter_export else None)
    end_col = find("end_time", "endtime", "结束时间") or (columns[5] if letter_export else None)
    status_col = find("status", "状态") or (columns[3] if len(columns) > 3 else None)
    gpu_col = find("plan_gpu", "gpu", "gpu数") or (columns[8] if len(columns) > 8 else None)
    gpu_type_col = find("gpu_type", "gpu类型") or (columns[9] if len(columns) > 9 else None)
    compute_col = find("compute", "算力消耗", "算力") or (columns[10] if len(columns) > 10 else None)
    preferred_col = find("user_specified_data_center", "data_center", "data_center_id", "指定数据中心", "用户指定数据中心")
    data_size_col = find("data_size_gb", "数据量", "数据量gb")
    cpu_col = find("required_cpu_cores", "cpu核数", "required_cpu")
    gpu_count_col = find("required_gpu_count", "gpu_count", "GPU数量")
    required_gpu_type_col = find("required_gpu_type", "required_gpu_type")
    max_wait_col = find("max_wait_hours", "max_wait", "可延后时间", "最大等待小时")

    def optional_float(value):
        try:
            return float(value) if pd.notna(value) and str(value).strip() else None
        except (TypeError, ValueError):
            return None
    valid_starts = []
    for value in table[start_col] if start_col else []:
        try:
            numeric = float(value)
            if pd.notna(numeric):
                valid_starts.append(numeric)
        except (TypeError, ValueError):
            continue
    first_valid = min(valid_starts) if valid_starts else None
    for row_index, row in table.iterrows():
        status = str(row.get(status_col, "")).strip() if status_col else ""
        if first_valid is None:
            continue
        start = optional_float(row.get(start_col))
        if start is None:
            start = optional_float(row.get("submitted_at"))
        if start is None:
            continue
        try:
            end = float(row.get(end_col)) if end_col and pd.notna(row.get(end_col)) else None
        except (TypeError, ValueError):
            end = None
        duration_seconds = (end - start) if end is not None else 0
        if duration_seconds <= 0:
            duration_random = __import__("random").Random(f"{seed}:duration:{row_index}")
            duration_seconds = duration_random.randint(60, 3600)
        duration_seconds = min(24 * 3600, max(30, duration_seconds))
        duration_hours = duration_seconds / 3600.0
        try:
            plan_gpu = float(row.get(gpu_col) or 0)
        except (TypeError, ValueError):
            plan_gpu = 0.0
        try:
            compute_value = float(row.get(compute_col)) if compute_col else 0.0
        except (TypeError, ValueError):
            compute_value = 0.0
        required_compute = (plan_gpu / 100.0) * 20.0 if plan_gpu > 0 else max(compute_value / 1000.0, 1.0)
        specified = str(row.get(preferred_col)).strip() if preferred_col and pd.notna(row.get(preferred_col)) else None
        task = Task(
            id=f"Task_{row_index}", user_id=f"user_{row_index}", task_type="ai_training" if plan_gpu > 0 else "general",
            required_compute=required_compute, duration_hours=duration_hours, region=region,
            submitted_at=(start - first_valid),
            max_wait_hours=optional_float(row.get(max_wait_col)) or 1.0,
            data_size_gb=optional_float(row.get(data_size_col)) if data_size_col else None,
            required_cpu_cores=(int(optional_float(row.get(cpu_col))) if cpu_col and optional_float(row.get(cpu_col)) is not None else None),
            required_gpu_count=(int(optional_float(row.get(gpu_count_col))) if gpu_count_col and optional_float(row.get(gpu_count_col)) is not None else None),
            required_gpu_type=(
                str(row.get(required_gpu_type_col)).strip()
                if required_gpu_type_col and pd.notna(row.get(required_gpu_type_col)) and str(row.get(required_gpu_type_col)).strip()
                else (str(row.get(gpu_type_col)).strip() if gpu_type_col and pd.notna(row.get(gpu_type_col)) else None)
            ),
            user_specified_data_center=specified or None,
        )
        completed[task.id] = factory.complete(task)

    # Keep original rows and values. Canonical columns are the durable,
    # normalized values consumed by the platform.
    table = table.astype(object)
    for column in CANONICAL_COLUMNS:
        if column not in table.columns:
            table[column] = ""
        table[column] = table[column].astype("object")
    for row_index in table.index:
        task = completed.get(f"Task_{row_index}")
        if task is None:
            # Keep even failed/waiting source rows complete in the durable
            # table; the runtime may still filter them by status.
            rng = __import__("random").Random(f"{seed}:row:{row_index}")
            start_value = optional_float(table.at[row_index, start_col]) if start_col else None
            if start_value is None:
                start_value = optional_float(table.at[row_index, "submitted_at"]) or float(row_index)
            table.at[row_index, "task_id"] = f"Task_{row_index}"
            table.at[row_index, "data_size_gb"] = round(rng.uniform(10, 200), 2)
            table.at[row_index, "required_cpu_cores"] = rng.choice((2, 4, 8, 16))
            gpu_count = optional_float(table.at[row_index, gpu_count_col]) if gpu_count_col else None
            table.at[row_index, "required_gpu_count"] = int(gpu_count or 0)
            table.at[row_index, "required_gpu_type"] = str(table.at[row_index, required_gpu_type_col]).strip() if required_gpu_type_col and pd.notna(table.at[row_index, required_gpu_type_col]) else ""
            table.at[row_index, "user_specified_data_center"] = ""
            table.at[row_index, "submitted_at"] = max(0.0, start_value - (first_valid or start_value))
            table.at[row_index, "max_wait_seconds"] = 180.0
            table.at[row_index, "duration_seconds"] = rng.randint(60, 3600)
            continue
        table.at[row_index, "task_id"] = task.id
        table.at[row_index, "data_size_gb"] = task.data_size_gb
        table.at[row_index, "required_cpu_cores"] = task.required_cpu_cores
        table.at[row_index, "required_gpu_count"] = task.required_gpu_count
        table.at[row_index, "required_gpu_type"] = task.required_gpu_type or ""
        table.at[row_index, "user_specified_data_center"] = task.user_specified_data_center or ""
        table.at[row_index, "submitted_at"] = task.submitted_at
        table.at[row_index, "max_wait_seconds"] = task.max_wait_seconds or task.max_wait_hours * 3600.0
        table.at[row_index, "duration_seconds"] = round(task.duration_hours * 3600.0, 3)

    def workload_order(index):
        start_value = optional_float(table.at[index, start_col]) if start_col else None
        return (start_value is None, start_value if start_value is not None else float(index))

    ordered_indices = sorted(list(table.index), key=workload_order)
    _apply_realistic_workload_profile(
        table,
        ordered_indices=ordered_indices,
        plan_gpu_col=gpu_col,
        compute_col=compute_col,
        seed=seed,
    )

    # The source end/finish timestamp is not a scheduling input.  Retain the
    # derived duration, but remove the misleading release-time column.
    if end_col and end_col in table.columns:
        table = table.drop(columns=[end_col])
    if "duration_hours" in table.columns:
        table = table.drop(columns=["duration_hours"])
    if "max_wait_hours" in table.columns:
        table = table.drop(columns=["max_wait_hours"])

    # Fill remaining source-side gaps while preserving the intentionally
    # optional user-selected data-centre column.
    for column in list(table.columns):
        if column == "user_specified_data_center":
            continue
        normalized = str(column).strip().lower()
        if normalized in {"j", "gpu_type", "gpu类型", "required_gpu_type"}:
            table[column] = table[column].fillna("NONE").replace("", "NONE")
        elif normalized in {"e", "start_time", "starttime", "开始时间"}:
            table[column] = table[column].fillna(first_valid if first_valid is not None else 0)
        elif normalized in {"g", "h", "i", "plan_cpu", "plan_mem", "plan_gpu"}:
            table[column] = table[column].fillna(0)
        else:
            table[column] = table[column].fillna("")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.suffix.lower() == ".xlsx":
        table.to_excel(output_path, index=False)
    else:
        table.to_csv(output_path, index=False, encoding="utf-8")
    return output_path
