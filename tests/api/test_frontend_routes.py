import io
import json
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient

from src.app import create_app


class FakeEngine:
    def __init__(self, **kwargs):
        self.running = False
        self.current_second = 0
        self.stopped = 0
        self.started_with = kwargs
        self.submitted = []

    def start(self):
        self.running = True

    def stop(self):
        self.running = False
        self.stopped += 1

    def get_latest_stats(self):
        return {}

    def get_total_carbon_summary(self):
        return {"total_emission_ton": 0.0}

    def get_optimization_result(self):
        return {}

    def get_pricing(self):
        return {"mode": "fixed_tou"}

    def submit_task(self, task):
        self.submitted.append(task)


def make_client():
    engines = []

    def factory(**kwargs):
        engine = FakeEngine(**kwargs)
        engines.append(engine)
        return engine

    app = create_app(engine=None, engine_factory=factory)
    return TestClient(app), engines


def compatible_topology_bytes(name="uploaded-test"):
    topology = json.loads(Path("config/topology_ieee39.json").read_text(encoding="utf-8"))
    topology["name"] = name
    topology["resource_mapping"] = {
        "pv_bus": 10,
        "wind_bus": 20,
        "load_buses": list(range(1, 30)),
    }
    return json.dumps(topology).encode("utf-8")


def _tou_upload():
    return {"pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv")}


def test_apply_exposes_heuristic_dispatch_and_hides_joint_dispatch():
    client, _ = make_client()

    heuristic = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic", "objective_strategy": "min_cost"},
        files=_tou_upload(),
    )
    assert heuristic.status_code == 200
    assert heuristic.json()["configuration"]["dispatch_strategy"] == "lowest_price"
    assert heuristic.json()["configuration"]["dispatch_managed_by"] is None

    global_mode = client.post(
        "/config/apply",
        data={"optimization_model": "global", "objective_strategy": "weighted"},
        files=_tou_upload(),
    )
    assert global_mode.status_code == 200
    assert global_mode.json()["configuration"]["dispatch_strategy"] == "optimizer_managed"
    assert global_mode.json()["configuration"]["dispatch_managed_by"] == "joint_optimizer"


def test_weighted_objective_applies_complete_comprehensive_weights():
    client, engines = make_client()

    response = client.post(
        "/config/apply",
        data={
            "optimization_model": "global",
            "pricing_strategy": "marginal",
            "objective_strategy": "weighted",
        },
    )
    assert response.status_code == 200
    assert client.post("/simulation/start").status_code == 200

    assert engines[-1].started_with["ui_config"]["optimization"]["weights"] == {
        "cost": 1.0,
            "carbon": 1.5,
        "delay": 0.2,
        "peak_shaving": 3.0,
        "renewable_curtailment": 1.0,
        "storage_degradation": 0.5,
    }


def test_apply_rejects_options_hidden_by_selected_model():
    client, _ = make_client()

    delay = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic", "objective_strategy": "min_delay"},
        files=_tou_upload(),
    )
    assert delay.status_code == 422
    assert "min_delay" in delay.json()["detail"]

    managed_dispatch = client.post(
        "/config/apply",
        data={
            "optimization_model": "global",
            "dispatch_strategy": "custom",
            "objective_strategy": "min_cost",
            "custom_dispatch_config": json.dumps({
                "weights": {"price": 1, "load": 0, "carbon": 0, "latency": 0}
            }),
        },
        files=_tou_upload(),
    )
    assert managed_dispatch.status_code == 422
    assert "调度策略" in managed_dispatch.json()["detail"]


def test_apply_config_requires_peak_valley_csv():
    client, _ = make_client()
    response = client.post(
        "/config/apply",
        data={"dispatch_strategy": "lowest_price", "pricing_strategy": "fixed_tou", "optimization_model": "heuristic"},
    )
    assert response.status_code == 422
    assert "CSV" in response.json()["detail"]


def test_apply_config_accepts_user_config_and_csv_then_start():
    client, engines = make_client()
    response = client.post(
        "/config/apply",
        data={"dispatch_strategy": "lowest_price", "pricing_strategy": "fixed_tou", "optimization_model": "heuristic"},
        files={
            "user_config": ("user.yaml", b"simulation:\n  step_seconds: 1\n", "application/yaml"),
            "pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,8,0.4\n8,24,1.2\n", "text/csv"),
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "applied"

    response = client.post("/simulation/start")
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    started_config = engines[-1].started_with
    assert started_config["user_config"]["simulation"] == {"step_seconds": 1}
    assert started_config["ui_config"]["electricity_pricing"]["mode"] == "fixed_tou"
    assert started_config["ui_config"]["electricity_pricing"]["file"] is None
    assert started_config["ui_config"]["electricity_pricing"]["periods"] == [
        {"start_hour": 0.0, "end_hour": 8.0, "price": 0.4},
        {"start_hour": 8.0, "end_hour": 24.0, "price": 1.2},
    ]
    assert started_config["ui_config"]["optimization"]["enabled"] is False


def test_apply_accepts_json_topology_and_passes_it_to_engine():
    client, engines = make_client()
    response = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={
            "pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv"),
            "topology_file": ("custom.json", compatible_topology_bytes(), "application/json"),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["configuration"]["topology_source"] == "user_upload"
    assert payload["configuration"]["topology_summary"]["name"] == "uploaded-test"
    assert payload["configuration"]["topology_summary"]["bus_count"] == 39

    assert client.post("/simulation/start").status_code == 200
    ui_config = engines[-1].started_with["ui_config"]
    assert ui_config["topology"]["name"] == "uploaded-test"
    assert ui_config["grid"]["pv_bus"] == 10
    assert ui_config["grid"]["wind_bus"] == 20


def test_apply_rejects_uploaded_topology_incompatible_with_effective_resources():
    client, _ = make_client()
    topology = json.loads(Path("tests/fixtures/topology_minimal.json").read_text(encoding="utf-8"))
    topology["resource_mapping"] = {"pv_bus": 1, "wind_bus": 2, "load_buses": [2]}

    response = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={
            "pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv"),
            "topology_file": ("minimal.json", json.dumps(topology).encode(), "application/json"),
        },
    )

    assert response.status_code == 422
    assert "data_centers[0].grid_bus_id references unknown bus 3" in response.json()["detail"]


def test_apply_rejects_non_json_topology_upload():
    client, _ = make_client()
    response = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={
            "pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv"),
            "topology_file": ("topology.yaml", b"buses: []", "application/yaml"),
        },
    )

    assert response.status_code == 422
    assert "JSON" in response.json()["detail"]


def test_apply_without_topology_after_upload_returns_to_platform_default():
    client, _ = make_client()
    common = {"pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv")}
    first = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={**common, "topology_file": ("custom.json", compatible_topology_bytes(), "application/json")},
    )
    assert first.status_code == 200

    second = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files=common,
    )

    assert second.status_code == 200
    assert second.json()["configuration"]["topology_source"] == "platform_default"
    assert second.json()["configuration"]["topology_summary"]["name"] == "IEEE39"


def test_apply_returns_effective_uploaded_data_centers_and_sources():
    client, _ = make_client()
    payload = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={
            "user_config": ("user.json", json.dumps({"data_centers": [
                {"id": "DC-X", "name": "自定义中心", "region": "custom", "grid_bus_id": 3,
                 "total_compute": 77, "total_bandwidth": 900,
                 "device_pool": [{"model": "GPU-Custom", "equipment_type": "compute", "quantity": 4, "rated_power_kw": 1.0}]}
            ]}).encode(), "application/json"),
            "pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv"),
        },
    )

    assert payload.status_code == 200
    result = payload.json()
    assert result["effective_data_centers"] == [{
        "id": "DC-X", "name": "自定义中心", "total_compute": 77, "total_bandwidth": 900,
        "compute_device_quantity": 4,
    }]
    assert result["configuration_sources"]["data_centers"] == "user_upload"


def test_apply_returns_platform_data_centers_when_upload_omits_them():
    client, _ = make_client()
    response = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={"pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv")},
    )

    assert response.status_code == 200
    centers = response.json()["effective_data_centers"]
    assert [(center["id"], center["total_compute"]) for center in centers] == [
        ("DC1", 250), ("DC2", 200), ("DC3", 300),
    ]
    assert response.json()["configuration_sources"]["data_centers"] == "platform_default"


def test_apply_preview_uses_engine_default_bandwidth_for_partial_center():
    client, _ = make_client()
    response = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={
            "user_config": ("user.json", json.dumps({"data_centers": [
                {"id": "DC-X", "name": "中心", "region": "custom", "grid_bus_id": 3, "total_compute": 77}
            ]}).encode(), "application/json"),
            "pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv"),
        },
    )

    assert response.status_code == 200
    assert response.json()["effective_data_centers"][0]["total_bandwidth"] == 1000


def test_apply_rejects_legacy_duplicate_config_paths_with_migration_message():
    client, _ = make_client()
    response = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={
            "user_config": ("old.json", b'{"data_sources":{"task_file":"tasks.csv"}}', "application/json"),
            "pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv"),
        },
    )

    assert response.status_code == 422
    assert "data_sources" in response.json()["detail"]
    assert "upload" in response.json()["detail"]


def test_apply_rejects_incomplete_uploaded_device_configuration():
    client, _ = make_client()
    response = client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={
            "user_config": ("user.json", json.dumps({"data_centers": [
                {"id": "DC-X", "name": "中心", "region": "r", "grid_bus_id": 3,
                 "total_compute": 77, "device_pool": [{"equipment_type": "compute", "quantity": 4}]}
            ]}).encode(), "application/json"),
            "pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv"),
        },
    )

    assert response.status_code == 422
    assert "model" in response.json()["detail"]


def test_start_uses_platform_task_file_once_when_no_task_upload(monkeypatch):
    from src.loaders import excel_loader

    calls = []
    monkeypatch.setattr(excel_loader, "load_tasks_from_excel", lambda path: calls.append(path) or [])
    client, engines = make_client()
    client.post(
        "/config/apply",
        data={"optimization_model": "heuristic"},
        files={"pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv")},
    )

    assert client.post("/simulation/start").status_code == 200
    assert client.post("/simulation/start").status_code == 200
    assert len(calls) == 1
    assert calls[0].endswith("task_data_completed.csv")
    assert engines[-1].submitted == []


def test_reapply_after_stop_starts_a_fresh_engine_with_new_configuration():
    client, engines = make_client()
    pricing = {"pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv")}
    first = client.post("/config/apply", data={"optimization_model": "heuristic"}, files=pricing)
    assert first.status_code == 200
    assert client.post("/simulation/start").status_code == 200
    assert client.post("/config/apply", data={"optimization_model": "global"}, files=pricing).status_code == 409
    assert client.post("/simulation/stop").status_code == 200

    second = client.post(
        "/config/apply",
        data={"optimization_model": "global"},
        files={
            **pricing,
            "user_config": ("updated.json", b'{"simulation":{"step_seconds":7}}', "application/json"),
        },
    )
    assert second.status_code == 200
    assert client.post("/simulation/start").status_code == 200

    assert len(engines) == 2
    assert engines[-1].started_with["user_config"]["simulation"]["step_seconds"] == 7


def test_start_requires_applied_config_and_stop_is_idempotent():
    client, engines = make_client()
    response = client.post("/simulation/start")
    assert response.status_code == 409

    client.post(
        "/config/apply",
        data={"dispatch_strategy": "lowest_price", "pricing_strategy": "fixed_tou", "optimization_model": "heuristic"},
        files={"pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv")},
    )
    assert client.post("/simulation/start").status_code == 200
    assert client.post("/simulation/stop").status_code == 200
    assert client.post("/simulation/stop").status_code == 200
    assert engines[-1].stopped == 2


def test_status_has_zero_state_and_export_is_safe_json():
    client, _ = make_client()
    status = client.get("/simulation/status")
    assert status.status_code == 200
    payload = status.json()
    assert payload["status"] == "not_configured"
    assert payload["latest_stats"] == {}

    export = client.get("/export?format=json")
    assert export.status_code == 409


def test_status_keeps_last_non_null_grid_telemetry_until_new_measurement_arrives():
    class TelemetryEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.sample = {
                "wind_generation_mw": 40.0,
                "pv_generation_mw": 0.0,
                "storage_power_mw": 0.0,
                "storage_soc_pct": 50.0,
                "reserve_capacity_mw": 1657.0,
                "grid_voltage_health": {"status": "out_of_range", "min_voltage_pu": 0.925, "max_voltage_pu": 1.064},
            }

        def get_latest_stats(self):
            return self.sample

    engine = TelemetryEngine()
    client = TestClient(create_app(engine))
    first = client.get("/simulation/status").json()["latest_stats"]
    assert first["reserve_capacity_mw"] == 1657.0

    engine.sample = {
        "wind_generation_mw": None,
        "storage_soc_pct": None,
        "reserve_capacity_mw": None,
        "grid_voltage_health": None,
    }
    second = client.get("/simulation/status").json()["latest_stats"]

    assert second["wind_generation_mw"] == 40.0
    assert second["storage_soc_pct"] == 50.0
    assert second["reserve_capacity_mw"] == 1657.0
    assert second["grid_voltage_health"]["status"] == "out_of_range"


def test_task_csv_is_loaded_into_engine_on_start():
    client, engines = make_client()
    client.post(
        "/config/apply",
        data={"dispatch_strategy": "lowest_price", "pricing_strategy": "fixed_tou", "optimization_model": "heuristic"},
        files={
            "pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv"),
            "task_file": ("tasks.csv", b"start_time,status,plan_cpu,\xe7\xae\x97\xe5\x8a\x9b\xe6\xb6\x88\xe8\x80\x97\n0,Running,2,1000\n", "text/csv"),
        },
    )
    assert client.post("/simulation/start").status_code == 200
    assert len(engines[-1].submitted) == 1


def test_export_formats_are_real_csv_and_xlsx():
    client, _ = make_client()
    client.post(
        "/config/apply",
        data={"dispatch_strategy": "lowest_price", "pricing_strategy": "fixed_tou", "optimization_model": "heuristic"},
        files={"pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv")},
    )
    csv_response = client.get("/export?format=csv")
    xlsx_response = client.get("/export?format=xlsx")
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert b"status" in csv_response.content
    assert xlsx_response.status_code == 200
    assert xlsx_response.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert xlsx_response.content[:2] == b"PK"


def test_zip_export_contains_tabular_results_and_runtime_reports():
    client, _ = make_client()
    client.post(
        "/config/apply",
        data={"dispatch_strategy": "lowest_price", "pricing_strategy": "fixed_tou", "optimization_model": "heuristic"},
        files={"pricing_csv": ("tou.csv", b"start_hour,end_hour,price\n0,24,0.5\n", "text/csv")},
    )
    response = client.get("/export?format=zip")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        names = set(archive.namelist())
    assert {"simulation-result.json", "simulation-result.csv", "simulation-result.xlsx"}.issubset(names)


def test_status_and_exports_include_coordination_and_complete_history():
    class HistoryEngine(FakeEngine):
        def __init__(self):
            super().__init__()
            self.current_second = 2
            self.history = [
                {
                    "second": 1, "total_load": 40.0,
                    "coordination_mode": "local", "coordination_iterations": 2,
                    "generator_dispatch": {"gen-a": 30.0},
                    "datacenter_runtime": {"custom-dc": {"used_compute": 8.0}},
                },
                {
                    "second": 2, "total_load": 42.0,
                    "coordination_mode": "local", "coordination_iterations": 3,
                    "generator_dispatch": {"gen-a": 31.0, "gen-b": 11.0},
                    "datacenter_runtime": {"custom-dc": {"used_compute": 9.0}},
                },
            ]

        def get_latest_stats(self):
            return self.history[-1]

        def get_stats_history(self):
            return self.history

        def get_optimization_result(self):
            return {
                "mode": "local", "coordination_iterations": 3,
                "generator_mw": {"gen-a": 31.0, "gen-b": 11.0},
                "objective_components": {"cost": {"raw": 1.0}},
                "strictly_feasible": True,
            }

    app = create_app(HistoryEngine())
    app.state.frontend_session.configuration = {"optimization_model": "local"}
    client = TestClient(app)

    status = client.get("/simulation/status").json()
    assert status["optimization"]["coordination_iterations"] == 3
    assert status["optimization"]["generator_mw"]["gen-b"] == 11.0

    json_export = client.get("/export?format=json").json()
    assert len(json_export["statistics_history"]) == 2
    csv_text = client.get("/export?format=csv").content.decode("utf-8-sig")
    assert len(csv_text.strip().splitlines()) == 3
    assert "generator_dispatch.gen-b" in csv_text
    assert "datacenter_runtime.custom-dc.used_compute" in csv_text
