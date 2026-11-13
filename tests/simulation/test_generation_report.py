from pathlib import Path
import shutil

from src.simulation.engine import (
    SimulationEngine,
    task_power_snapshot,
    calculate_net_load_mw,
    format_total_cost_log,
    display_load_with_network_loss,
    aggregate_storage_power_samples,
    calculate_training_average_latency,
    format_training_latency_log,
)
from src.domain.task import Task


def test_task_power_snapshot_separates_planned_and_actual_windows():
    task = Task(
        id="delayed",
        user_id="u",
        task_type="batch",
        required_compute=10,
        duration_hours=1,
        submitted_at=0,
    )
    task.actual_start_second = 1800
    task.actual_end_second = 5400
    task.actual_power_mw = 2.5

    before_start = task_power_snapshot([task], {}, 900)
    after_start = task_power_snapshot([task], {}, 2700)

    assert before_start["planned_task_power_mw"] == 2.5
    assert before_start["actual_task_power_mw"] == 0
    assert after_start["planned_task_power_mw"] == 2.5
    assert after_start["actual_task_power_mw"] == 2.5


def test_net_load_uses_initial_load_plan_and_actual_compute():
    assert calculate_net_load_mw(100.0, 70.0, 8.0) == 38.0


def test_total_cost_console_line_is_currency_formatted():
    assert format_total_cost_log(1234.5) == "  总运行成本: 1,234.50 元"


def test_training_average_latency_uses_submission_to_completion_for_training_only():
    training = Task(
        id="training", user_id="u", task_type="AI_TRAINING", required_compute=1,
        duration_hours=1, submitted_at=10, actual_end_second=70,
    )
    web = Task(
        id="web", user_id="u", task_type="WEB_SERVICE", required_compute=1,
        duration_hours=1, submitted_at=0, actual_end_second=100,
    )

    assert calculate_training_average_latency([training, web]) == 60.0
    assert format_training_latency_log(60.0) == "  Average training task latency: 60.00 s"


def test_display_load_includes_network_loss_for_power_balance():
    assert display_load_with_network_loss(100.0, 2.5) == 102.5
    assert display_load_with_network_loss(100.0, None) == 100.0


def test_storage_power_samples_are_aggregated_into_time_bins():
    samples = aggregate_storage_power_samples(
        [{"second": 0, "storage_power_mw": 10},
         {"second": 60, "storage_power_mw": 20},
         {"second": 300, "storage_power_mw": -5}],
        bucket_seconds=300,
    )
    assert samples == [(150.0, 15.0), (450.0, -5.0)]


def test_generation_totals_are_rendered_as_lines_with_reference_palette():
    tmp_path = Path("tests/.tmp_generation_lines")
    shutil.rmtree(tmp_path, ignore_errors=True)
    tmp_path.mkdir(parents=True)
    engine = SimulationEngine.__new__(SimulationEngine)
    engine.report_dir = tmp_path
    engine.stats_records = {
        0: {
            "second": 0,
            "base_generation_mw": 1,
            "conventional_generation_mw": 1,
            "wind_generation_mw": 1,
            "pv_generation_mw": 1,
            "storage_discharge_mw": 0,
            "grid_import_mw": 0,
            "total_load": 4,
            "total_generation": 4,
            "task_summary": {},
        }
    }
    engine._write_svg_report(tmp_path / "simulation_dashboard.svg")
    svg = (tmp_path / "generation_24h.svg").read_text(encoding="utf-8")
    assert svg.count('stroke-width="2.8"') == 2
    assert '#b7d9b1' in svg
    assert '#f5c99d' in svg
    shutil.rmtree(tmp_path, ignore_errors=True)


def test_generation_report_writes_stacked_areas_and_top_left_legend():
    tmp_path = Path("tests/.tmp_generation_report")
    shutil.rmtree(tmp_path, ignore_errors=True)
    tmp_path.mkdir(parents=True)
    engine = SimulationEngine.__new__(SimulationEngine)
    engine.report_dir = tmp_path
    engine.stats_records = {
        0: {
            "second": 0,
            "base_generation_mw": 40,
            "conventional_generation_mw": 20,
            "wind_generation_mw": 10,
            "pv_generation_mw": 5,
            "wind_available_mw": 12,
            "pv_available_mw": 6,
            "storage_discharge_mw": -3,
            "grid_import_mw": 2,
            "total_load": 55,
            "total_generation": 58,
            "planned_task_power_mw": 1,
            "actual_task_power_mw": 0,
            "task_summary": {
                "submitted": 0, "scheduling": 0, "completed": 0,
                "running": 0, "avg_planned_duration_seconds": 0,
                "avg_actual_execution_seconds": 0,
            },
            "compute_power_mw": 0,
        },
        3600: {
            "second": 3600,
            "base_generation_mw": 42,
            "conventional_generation_mw": 21,
            "wind_generation_mw": 12,
            "pv_generation_mw": 8,
            "wind_available_mw": 15,
            "pv_available_mw": 10,
            "storage_discharge_mw": 0,
            "grid_import_mw": 1,
            "total_load": 60,
            "total_generation": 62,
            "planned_task_power_mw": 2,
            "actual_task_power_mw": 1.5,
            "task_summary": {
                "submitted": 1, "scheduling": 1, "completed": 0,
                "running": 0, "avg_planned_duration_seconds": 10,
                "avg_actual_execution_seconds": 0,
            },
            "compute_power_mw": 1,
        },
    }

    engine._write_svg_report(tmp_path / "simulation_dashboard.svg")
    svg = (tmp_path / "generation_24h.svg").read_text(encoding="utf-8")

    assert svg.count("fill-opacity") >= 6
    assert "Generation" in svg
    assert "Total Load" in svg
    assert "Total Generation" in svg
    assert ">Total Load (MW)<" not in svg
    assert ">Total Generation (MW)<" not in svg
    assert 'fill="white"' in svg
    assert 'x="95" y="38" width="840" height="48"' not in svg
    assert 'x="110"' in svg
    assert (tmp_path / "supply_storage_24h.svg").exists()
    assert (tmp_path / "task_power_24h.svg").exists()
    supply_svg = (tmp_path / "supply_storage_24h.svg").read_text(encoding="utf-8")
    task_svg = (tmp_path / "task_power_24h.svg").read_text(encoding="utf-8")
    assert ">Load (MW)<" not in supply_svg
    assert "Net Load" in supply_svg
    assert "Wind Input" in supply_svg
    assert "PV Input" in supply_svg
    assert supply_svg.count("<polygon") >= 2
    assert "Storage Power (MW)" in supply_svg
    assert "Grid Import" not in supply_svg
    assert "#806c88" in supply_svg
    assert "24小时" not in supply_svg
    assert "24小时" not in svg
    assert "Planned Task Power" in task_svg
    assert "Actual Task Power" in task_svg
    assert ">Planned Task Power (MW)<" not in task_svg
    assert ">Actual Task Power (MW)<" not in task_svg
    shutil.rmtree(tmp_path, ignore_errors=True)
