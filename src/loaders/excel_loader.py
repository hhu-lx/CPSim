# src/loaders/excel_loader.py
import pandas as pd
from pathlib import Path
from typing import List, Optional, Dict
from src.domain.task import Task
from src.domain.enums import TaskType
from src.simulation.network_sim import get_data_size_by_task_type

class ExcelTaskLoader:

    GPU_TYPE_MAP = {
        'V100': TaskType.AI_TRAINING,
        'P100': TaskType.AI_TRAINING,
        'T4': TaskType.AI_TRAINING,
        'MISC': TaskType.GENERAL,
        '': TaskType.GENERAL,
    }
    VALID_STATUSES = {'Terminated', 'Running'}

    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self.raw_data = None
        self.tasks = []
        self._load_data()

    def _load_data(self):
        if not self.file_path.exists():
            raise FileNotFoundError(f"File not found: {self.file_path}")
        if self.file_path.suffix.lower() in {".csv", ".tsv"}:
            self.raw_data = pd.read_csv(self.file_path, dtype=str)
        else:
            self.raw_data = pd.read_excel(self.file_path, dtype=str)
        print(f"Loaded data: {len(self.raw_data)} rows")
        print(f"   Columns: {list(self.raw_data.columns)}")

    def _auto_detect_columns(self, columns) -> Dict[str, str]:
        col_list = list(columns)
        col_map = {}

        # Implementation note.
        # Implementation note.
        first_10 = [str(c).strip().lstrip("\ufeff") for c in col_list[:10]]
        if all(c in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J'] for c in first_10[:4]) and first_10[4] == 'E':
            print(" Letter-based columns detected; mapping by position...")
            pos_map = {
                'job_name': 0, 'task_name': 1, 'inst_num': 2, 'status': 3,
                'start_time': 4, 'end_time': 5, 'plan_cpu': 6, 'plan_mem': 7,
                'plan_gpu': 8, 'gpu_type': 9, 'compute': 10
            }
            for key, pos in pos_map.items():
                if pos < len(col_list) and not (key == 'end_time' and str(col_list[pos]).strip() != 'F'):
                    col_map[key] = col_list[pos]
            # The completed table intentionally omits the old F/end-time
            # column, so positional indexes after E shift left.  Recover the
            # remaining source fields by their letter labels when present.
            letter_fields = {'plan_cpu': 'G', 'plan_mem': 'H', 'plan_gpu': 'I', 'gpu_type': 'J'}
            for key, label in letter_fields.items():
                if label in col_list:
                    col_map[key] = label
            for col in col_list:
                if str(col).strip() in {'compute消耗', 'compute'}:
                    col_map['compute'] = col
            # Letter-labelled exports may still append canonical columns such
            # as the optional user-selected data centre.
            appended_aliases = {
                'task_id': {'task_id', '任务id', '任务编号'},
                'data_size_gb': {'data_size_gb', '数据量gb', '数据量_gb', '数据量'},
                'required_cpu_cores': {'required_cpu_cores', 'required_cpu', 'cpu核数'},
                'required_gpu_count': {'required_gpu_count', 'gpu_count', 'gpu数量'},
                'required_gpu_type': {'required_gpu_type', 'gpu_type_required'},
                'user_specified_data_center': {
                    'user_specified_data_center', 'data_center', 'data_center_id',
                    '指定数据中心', '用户指定数据中心', '数据中心',
                },
                'submitted_at': {'submitted_at', '提交时间', '提交时间秒'},
                'duration_seconds': {'duration_seconds', '持续时长秒', '任务时长秒', '持续时间秒'},
                'max_wait_seconds': {'max_wait_seconds', 'max_wait_sec', '最长wait秒', '最大wait秒', '允许延迟秒'},
                'max_wait_hours': {'max_wait_hours', 'max_wait', '可延后时间', '最大wait小时', '允许延迟小时'},
            }
            for col in col_list:
                normalized = str(col).strip().lstrip("\ufeff").lower()
                for key, aliases in appended_aliases.items():
                    if normalized in aliases:
                        col_map[key] = col
            return col_map


        col_lower = {str(col).strip().lower(): col for col in columns}
        mapping = {
            'start_time': ['start_time', 'starttime', '开始时间', '启动时间'],
            'end_time': ['end_time', 'endtime', '结束时间', '完成时间'],
            'status': ['status', '状态'],
            'plan_gpu': ['plan_gpu', 'gpu', 'gpu数'],
            'gpu_type': ['gpu_type', 'gpu类型'],
            'plan_cpu': ['plan_cpu', 'cpu', 'cpu核数'],
            'plan_mem': ['plan_mem', 'mem', '内存'],
            'job_name': ['job_name', '作业名称', '职位名称'],
            'task_name': ['task_name', '任务名称'],
            'inst_num': ['inst_num', '实例数'],
            'task_id': ['task_id', '任务ID', '任务编号'],
            'data_size_gb': ['data_size_gb', '数据量gb', '数据量_gb', '数据量'],
            'required_cpu_cores': ['required_cpu_cores', 'required_cpu', 'cpu核数'],
            'required_gpu_count': ['required_gpu_count', 'gpu_count', 'GPU数量'],
            'required_gpu_type': ['required_gpu_type', 'gpu_type_required'],
            'user_specified_data_center': [
                'user_specified_data_center', '指定数据中心', '用户指定数据中心',
                '数据中心', 'data_center', 'data_center_id',
            ],
            'max_wait_seconds': ['max_wait_seconds', 'max_wait_sec', '最长wait秒', '最大wait秒', '允许延迟秒'],
            'max_wait_hours': ['max_wait_hours', 'max_wait', '可延后时间', '最大wait小时', '允许延迟小时'],
            'submitted_at': ['submitted_at', '提交时间', '提交时间秒'],
            'duration_seconds': ['duration_seconds', '持续时长秒', '任务时长秒', '持续时间秒'],
        }
        for key, aliases in mapping.items():
            for alias in aliases:
                if alias.lower() in col_lower:
                    col_map[key] = col_lower[alias.lower()]
                    break
            if key not in col_map:
                for col in col_list:
                    col_str = str(col).lower().strip()
                    for alias in aliases:
                        if alias.lower() in col_str or col_str in alias.lower():
                            col_map[key] = col
                            break
                    if key in col_map:
                        break
        return col_map

    def _find_compute_column(self) -> Optional[str]:
        # Implementation note.
        for col in self.raw_data.columns:
            col_str = str(col).strip()
            if 'compute' in col_str or '消耗' in col_str:
                return col
        if len(self.raw_data.columns) >= 11:
            return self.raw_data.columns[10]
        return None

    def _to_float(self, value) -> Optional[float]:
        # Implementation note.
        if pd.isna(value) or value == '' or value is None:
            return None
        try:
            return float(value)
        except (ValueError, TypeError):
            return None

    def load_tasks(self, region: str = "region_a", time_compression: float = 1.0) -> List[Task]:
        """Load tasks using the timestamps stored in the input file.

        Task timestamps are normalized to seconds in
        ``config/task_data_completed.csv``.  ``time_compression`` remains in
        the signature for callers using older code, but is intentionally not
        applied so simulation time always matches the task data.
        """
        if time_compression <= 0:
            raise ValueError("time_compression must be positive")
        self.tasks = []
        df = self.raw_data.copy()
        col_map = self._auto_detect_columns(df.columns)
        print(f" Column mapping: {col_map}")  # Debug output

        # Implementation note.
        start_col = col_map.get('start_time')
        end_col = col_map.get('end_time')
        status_col = col_map.get('status')
        plan_gpu_col = col_map.get('plan_gpu')
        gpu_type_col = col_map.get('gpu_type')
        plan_cpu_col = col_map.get('plan_cpu')
        compute_col = self._find_compute_column()
        preferred_dc_col = col_map.get('user_specified_data_center')
        task_id_col = col_map.get('task_id')
        data_size_col = col_map.get('data_size_gb')
        cpu_cores_col = col_map.get('required_cpu_cores')
        gpu_count_col = col_map.get('required_gpu_count')
        required_gpu_type_col = col_map.get('required_gpu_type')
        max_wait_seconds_col = col_map.get('max_wait_seconds')
        max_wait_hours_col = col_map.get('max_wait_hours')
        submitted_at_col = col_map.get('submitted_at')
        duration_seconds_col = col_map.get('duration_seconds')

        # Implementation note.
        if start_col is None:
            raise ValueError("start_time column not found; check the column mapping")

        # Implementation note.
        df[start_col] = pd.to_numeric(df[start_col], errors='coerce')
        if end_col and end_col in df.columns:
            df[end_col] = pd.to_numeric(df[end_col], errors='coerce')
        # Implementation note.
        for col in [plan_gpu_col, plan_cpu_col]:
            if col and col in df.columns:
                df[col] = pd.to_numeric(df[col], errors='coerce')

        # Implementation note.
        df_sorted = df.dropna(subset=[start_col]).sort_values(by=start_col)

        if df_sorted.empty:
            raise ValueError("No valid start_time data")

        # Implementation note.
        first_valid_time = df_sorted.iloc[0][start_col]
        print(f"First task timestamp: {first_valid_time}")

        task_count = 0
        for idx, row in df_sorted.iterrows():
            # Implementation note.
            status = str(row.get(status_col, '')).strip() if status_col else ''
            if status not in self.VALID_STATUSES:
                continue

            start_ts = row.get(start_col)
            if pd.isna(start_ts) or start_ts == '':
                continue

            # Implementation note.
            submitted_hours = (start_ts - first_valid_time) / 3600

            # Implementation note.
            if duration_seconds_col and pd.notna(row.get(duration_seconds_col)):
                duration_hours = (self._to_float(row.get(duration_seconds_col)) or 3600.0) / 3600.0
            elif end_col and end_col in row.index:
                end_ts = row.get(end_col)
                if pd.notna(end_ts) and end_ts != '':
                    duration_seconds = (end_ts - start_ts)
                    duration_hours = duration_seconds / 3600
                else:
                    duration_hours = 1.0
            else:
                duration_hours = 1.0

            if duration_hours <= 0:
                duration_hours = 0.5
            if duration_hours > 24:
                duration_hours = 24.0

            # Implementation note.
            plan_gpu = row.get(plan_gpu_col, 0) if plan_gpu_col else 0
            if pd.isna(plan_gpu):
                plan_gpu = 0
            gpu_type = str(
                row.get(required_gpu_type_col, row.get(gpu_type_col, ''))
                if required_gpu_type_col else row.get(gpu_type_col, '')
            ).strip()

            # Implementation note.
            if plan_gpu and plan_gpu > 0:
                required_compute = (plan_gpu / 100.0) * 20.0
            elif compute_col and compute_col in row.index:
                compute_val = self._to_float(row.get(compute_col))
                if compute_val is not None and compute_val > 0:
                    required_compute = compute_val / 1000.0
                else:
                    required_compute = 1.0
            else:
                required_compute = 1.0

            # Implementation note.
            if plan_gpu and plan_gpu > 0 and gpu_type in self.GPU_TYPE_MAP:
                task_type = self.GPU_TYPE_MAP[gpu_type]
            elif plan_gpu and plan_gpu > 0:
                task_type = TaskType.AI_TRAINING
            else:
                task_type = TaskType.GENERAL

            # Implementation note.
            data_size = self._to_float(row.get(data_size_col)) if data_size_col else None
            if data_size is None:
                data_size = get_data_size_by_task_type(
                    task_type.value if hasattr(task_type, 'value') else str(task_type),
                    plan_gpu,
                )
            cpu_cores = self._to_float(row.get(cpu_cores_col)) if cpu_cores_col else None
            gpu_count = self._to_float(row.get(gpu_count_col)) if gpu_count_col else None
            task_id = str(row.get(task_id_col)).strip() if task_id_col and pd.notna(row.get(task_id_col)) else f"Task_{idx}"
            submitted_value = self._to_float(row.get(submitted_at_col)) if submitted_at_col else None
            task = Task(
                id=task_id,
                user_id=f"user_{abs(hash(str(start_ts))) % 100}",
                task_type=task_type.value if hasattr(task_type, 'value') else str(task_type),
                required_compute=required_compute,
                duration_hours=duration_hours,
                region=region,
                status="submitted",
                # Task timestamps are seconds from the first submitted task;
                # release is decided by the scheduler, not stored in the table.
                submitted_at=(submitted_value if submitted_value is not None else submitted_hours * 3600.0),
                max_wait_seconds=(
                    self._to_float(row.get(max_wait_seconds_col))
                    if max_wait_seconds_col and self._to_float(row.get(max_wait_seconds_col)) is not None
                    else ((self._to_float(row.get(max_wait_hours_col)) or 1.0) * 3600.0 if max_wait_hours_col else 3600.0)
                ),
                data_size_gb=data_size,
                required_cpu_cores=int(cpu_cores) if cpu_cores is not None else None,
                required_gpu_count=int(gpu_count) if gpu_count is not None else None,
                user_specified_data_center=(
                    str(row.get(preferred_dc_col)).strip()
                    if preferred_dc_col and pd.notna(row.get(preferred_dc_col))
                    and str(row.get(preferred_dc_col)).strip()
                    else None
                ),
            )
            # Implementation note.
            task.gpu_type = gpu_type
            task.plan_gpu = plan_gpu
            self.tasks.append(task)
            task_count += 1

        print(f" Successfully loaded {task_count} tasks")
        if self.tasks:
            print(f"   Submission time range: {self.tasks[0].submitted_at:.0f}s ~ {self.tasks[-1].submitted_at:.0f}s")
        return self.tasks


def load_tasks_from_excel(file_path: str = None, filename: str = "data_updated.xlsx", region: str = "region_a", time_compression: float = 1.0):
    if file_path is None:
        project_root = Path(__file__).parent.parent.parent
        config_dir = project_root / "config"
        file_path = config_dir / filename
        if not file_path.exists():
            xlsx_files = list(config_dir.glob("*.xlsx"))
            if xlsx_files:
                file_path = xlsx_files[0]
            else:
                raise FileNotFoundError(f"in {config_dir}: no Excel file found")
    loader = ExcelTaskLoader(str(file_path))
    return loader.load_tasks(region, time_compression=time_compression)
