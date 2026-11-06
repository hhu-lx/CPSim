"""HTTP routes used by the browser dashboard."""

from __future__ import annotations

import csv
import email
import io
import json
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs
from xml.sax.saxutils import escape

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from src.loaders.config_loader import build_data_centers, build_storages, load_platform_config, resolve_path
from src.loaders.topology_loader import (
    build_pandapower_network,
    load_topology,
    topology_grid_overrides,
    topology_summary,
    validate_grid_bus_references,
    validate_topology,
)
from src.loaders.user_config_loader import merge_frontend_config, validate_frontend_user_config, validate_user_resources
from src.strategies.electricity.fixed_tou import FixedTimeOfUsePricing
from src.strategies.allocation.custom_weighted import WeightedCustomStrategy
from src.optimization.mode_capabilities import (
    COMPREHENSIVE_OBJECTIVE_WEIGHTS,
    normalise_frontend_selection,
)
from src.strategies.electricity.custom_parameterized import GridResponsivePricing

router = APIRouter()


def _parse_config(raw: bytes, filename: str) -> dict[str, Any]:
    try:
        value = yaml.safe_load(raw.decode("utf-8-sig")) if filename.lower().endswith((".yaml", ".yml")) else json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Unable to parse user configuration file: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("User configuration data must be an object")
    return value


def _parse_topology(raw: bytes, filename: str) -> dict[str, Any]:
    if not filename or not filename.lower().endswith(".json"):
        raise ValueError("Power topology must be a JSON file")
    if len(raw) > 5 * 1024 * 1024:
        raise ValueError("Power topology file must not exceed 5 MB")
    try:
        topology = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to parse power topology JSON: {exc}") from exc
    validate_topology(topology)
    # Exercise pandapower construction here so schema values that are
    # structurally present but unusable fail before the simulation starts.
    try:
        build_pandapower_network(topology)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Unable to build power topology: {exc}") from exc
    return topology


def _parse_pricing(raw: bytes) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(text))
        required = {"start_hour", "end_hour", "price"}
        if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
            raise ValueError("Time-of-use pricing CSV must contain start_hour,end_hour,price fields")
        periods = []
        for row in reader:
            if not row or not any(str(v or "").strip() for v in row.values()):
                continue
            periods.append({"start_hour": float(row["start_hour"]), "end_hour": float(row["end_hour"]), "price": float(row["price"])})
        return FixedTimeOfUsePricing(periods).as_dict()
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        if isinstance(exc, ValueError) and str(exc).startswith("Time-of-use pricing"):
            raise
        raise ValueError(f"Invalid time-of-use pricing CSV: {exc}") from exc


def _validated_custom_dispatch(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Custom dispatch strategy must be a JSON/YAML object")
    unknown = set(config) - {"name", "weights", "allow_cross_region"}
    if unknown:
        raise ValueError(f"unknown custom dispatch field: {sorted(unknown)[0]}")
    strategy = WeightedCustomStrategy(
        weights=config.get("weights"),
        allow_cross_region=config.get("allow_cross_region", True),
    )
    return {
        "name": str(config.get("name") or "custom_weighted"),
        "weights": strategy.weights,
        "allow_cross_region": strategy.allow_cross_region,
    }


def _validated_custom_pricing(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Custom pricing strategy must be a JSON/YAML object")
    allowed = {
        "name", "base_price", "local_load_coefficient", "compute_load_coefficient",
        "voltage_deviation_coefficient", "minimum_price", "maximum_price",
    }
    unknown = set(config) - allowed
    if unknown:
        raise ValueError(f"unknown custom pricing field: {sorted(unknown)[0]}")
    strategy = GridResponsivePricing(**{key: value for key, value in config.items() if key != "name"})
    return {
        "name": str(config.get("name") or "grid_responsive_price"),
        "base_price": strategy.base_price,
        "local_load_coefficient": strategy.local_load_coefficient,
        "compute_load_coefficient": strategy.compute_load_coefficient,
        "voltage_deviation_coefficient": strategy.voltage_deviation_coefficient,
        "minimum_price": strategy.minimum_price,
        "maximum_price": strategy.maximum_price,
    }


def _validated_custom_objective(config: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Custom optimization objective must be a JSON/YAML object")
    unknown = set(config) - {"name", "weights"}
    if unknown:
        raise ValueError(f"unknown custom objective field: {sorted(unknown)[0]}")
    weights = config.get("weights")
    if not isinstance(weights, dict):
        raise ValueError("custom objective weights must be an object")
    allowed = {
        "cost", "carbon", "delay", "peak_shaving",
        "renewable_curtailment", "storage_degradation",
    }
    unknown_weights = set(weights) - allowed
    if unknown_weights:
        raise ValueError(f"unknown weight: {sorted(unknown_weights)[0]}")
    effective = {}
    for key in allowed:
        value = weights.get(key, 0.0)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) < 0:
            raise ValueError(f"objective weight {key} must be a non-negative number")
        effective[key] = float(value)
    if not any(value > 0 for value in effective.values()):
        raise ValueError("at least one objective weight must be greater than zero")
    return {
        "name": str(config.get("name") or "custom_multi_objective"),
        "weights": effective,
    }


def _xlsx_bytes(rows: list[list[Any]], config_rows: list[list[Any]]) -> bytes:
    """Create a small standards-compliant XLSX using only the stdlib."""
    def sheet_xml(values: list[list[Any]]) -> str:
        body = []
        for row_number, row in enumerate(values, 1):
            cells = []
            for column_number, value in enumerate(row, 1):
                column = ""
                number = column_number
                while number:
                    number, remainder = divmod(number - 1, 26)
                    column = chr(65 + remainder) + column
                text = escape(str(value if value is not None else ""))
                cells.append(f'<c r="{column}{row_number}" t="inlineStr"><is><t>{text}</t></is></c>')
            body.append(f'<row r="{row_number}">{"".join(cells)}</row>')
        return '<?xml version="1.0" encoding="UTF-8" standalone="yes"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + "".join(body) + "</sheetData></worksheet>"

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/><Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        archive.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        archive.writestr("xl/workbook.xml", '<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="simulation" sheetId="1" r:id="rId1"/><sheet name="configuration" sheetId="2" r:id="rId2"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml(rows))
        archive.writestr("xl/worksheets/sheet2.xml", sheet_xml(config_rows))
    return buffer.getvalue()


async def _request_parts(request: Request) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    """Parse the small dashboard form without requiring python-multipart."""
    content_type = request.headers.get("content-type", "")
    body = await request.body()
    fields: dict[str, str] = {}
    files: dict[str, dict[str, Any]] = {}
    if content_type.startswith("multipart/form-data"):
        message = email.message_from_bytes(
            b"Content-Type: " + content_type.encode() + b"\r\nMIME-Version: 1.0\r\n\r\n" + body
        )
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename is not None:
                files[name] = {"filename": filename, "content": payload}
            else:
                fields[name] = payload.decode("utf-8", errors="replace")
    else:
        values = parse_qs(body.decode("utf-8"), keep_blank_values=True)
        fields = {key: value[-1] for key, value in values.items()}
    return fields, files


@dataclass
class FrontendSession:
    engine: Any = None
    engine_factory: Callable[..., Any] | None = None
    configuration: dict[str, Any] | None = None
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    tasks_loaded_for: Any = field(default=None, repr=False)
    last_telemetry: dict[str, Any] = field(default_factory=dict, repr=False)

    def apply(self, *, dispatch_strategy: str | None, pricing_strategy: str, optimization_model: str, topology: str, user_config: dict[str, Any] | None, pricing: dict[str, Any], files: dict[str, dict[str, Any]], custom_dispatch: dict[str, Any] | None = None, custom_pricing: dict[str, Any] | None = None, objective_strategy: str | None = None, custom_objective: dict[str, Any] | None = None, uploaded_topology: dict[str, Any] | None = None) -> dict[str, Any]:
        objective_strategy = objective_strategy or "min_cost"
        selection = normalise_frontend_selection(
            optimization_model, dispatch_strategy, objective_strategy
        )
        dispatch_strategy = selection.dispatch_strategy
        optimization = {"enabled": optimization_model != "heuristic", "mode": {"local": "hierarchical", "global": "joint"}.get(optimization_model, "none")}
        effective_dispatch = _validated_custom_dispatch(custom_dispatch) if dispatch_strategy == "custom" else None
        effective_custom_pricing = _validated_custom_pricing(custom_pricing) if pricing_strategy == "custom" else None
        effective_custom_objective = _validated_custom_objective(custom_objective) if objective_strategy == "custom" else None
        if (
            optimization_model == "heuristic"
            and effective_custom_objective
            and effective_custom_objective["weights"].get("delay", 0.0) > 0.0
        ):
            raise ValueError("Baseline mode does not coordinate computing and power; custom objective delay weight must be 0")
        if objective_strategy:
            optimization["objective"] = "weighted" if objective_strategy == "custom" else objective_strategy
        if objective_strategy == "weighted":
            optimization["weights"] = dict(COMPREHENSIVE_OBJECTIVE_WEIGHTS)
        if effective_custom_objective:
            optimization["weights"] = effective_custom_objective["weights"]
        effective_pricing = (
            {"mode": "custom", "parameters": effective_custom_pricing, "file": None}
            if pricing_strategy == "custom"
            else {
                "mode": "marginal" if pricing_strategy == "marginal" else "fixed_tou",
                "periods": pricing.get("periods", []),
                "file": None,
            }
        )
        ui_config = {
            "optimization": optimization,
            "scheduling": {
                "strategy": "load_balance" if dispatch_strategy == "optimizer_managed" else "custom_weighted" if dispatch_strategy == "custom" else dispatch_strategy,
                **({"settings": {"weights": effective_dispatch["weights"], "allow_cross_region": effective_dispatch["allow_cross_region"]}} if effective_dispatch else {}),
            },
            "electricity_pricing": effective_pricing,
        }
        if uploaded_topology is not None:
            ui_config["topology"] = uploaded_topology
            mapping = topology_grid_overrides(uploaded_topology)
            if mapping:
                ui_config["grid"] = mapping
        parsed_user_config = user_config or {}
        validate_frontend_user_config(parsed_user_config)
        base_config = load_platform_config()
        effective_config, configuration_sources = merge_frontend_config(base_config, parsed_user_config, ui_config)
        effective_topology = uploaded_topology
        topology_source = "user_upload" if uploaded_topology is not None else "platform_default"
        if effective_topology is None:
            topology_path = base_config.get("grid", {}).get("topology_file") or "config/topology_ieee39.json"
            effective_topology = load_topology(resolve_path(topology_path))
        validate_grid_bus_references(effective_topology, effective_config.get("grid", {}))
        validate_user_resources(effective_config, effective_topology)
        effective_dc_models = build_data_centers(effective_config)
        build_storages(effective_config)
        generator_parameter_sources = {
            str(generator.get("id") or generator.get("name") or f"gen_{index}"):
                dict(generator.get("parameter_sources", {}))
            for index, generator in enumerate(effective_topology.get("generators", []))
        }
        effective_centers = [
            {
                "id": center.id,
                "name": center.name,
                "total_compute": center.total_compute,
                "total_bandwidth": center.total_bandwidth,
                "compute_device_quantity": sum(
                    device.quantity for device in center.device_pool
                    if center._is_compute_device(device)
                ),
            }
            for center in effective_dc_models
        ]
        self.configuration = {
            "dispatch_strategy": dispatch_strategy,
            "dispatch_managed_by": selection.dispatch_managed_by,
            "pricing_strategy": pricing_strategy,
            "optimization_model": optimization_model,
            "topology": effective_topology.get("name", topology),
            "topology_source": topology_source,
            "topology_summary": topology_summary(effective_topology),
            "generator_parameter_sources": generator_parameter_sources,
            "optimization": optimization,
            "pricing": effective_pricing,
            "mapping": {"name": "linear", "readonly": True},
            "user_config": parsed_user_config,
            "effective_data_centers": effective_centers,
            "configuration_sources": configuration_sources,
            "custom_dispatch_strategy": effective_dispatch,
            "custom_pricing_strategy": effective_custom_pricing,
            "objective_strategy": objective_strategy,
            "custom_objective": effective_custom_objective,
        }
        self.engine_config = parsed_user_config
        self.ui_config = ui_config
        self.files = files
        self.engine = None
        self.tasks_loaded_for = None
        self.last_telemetry = {}
        return {
            "status": "applied",
            "configuration": self.configuration,
            "effective_data_centers": effective_centers,
            "configuration_sources": configuration_sources,
            "files": {key: value["filename"] for key, value in files.items()},
        }

    def start(self) -> None:
        if self.configuration is None:
            raise RuntimeError("Apply the configuration first")
        if self.engine is not None and getattr(self.engine, "running", False):
            return
        if self.engine_factory is not None:
            self.engine = self.engine_factory(user_config=self.engine_config, ui_config=self.ui_config)
        if self.engine is None:
            from src.simulation.engine import SimulationEngine
            self.engine = SimulationEngine(user_config=self.engine_config, ui_config=self.ui_config)
        if self.tasks_loaded_for is not self.engine and hasattr(self.engine, "submit_task"):
            from src.loaders.excel_loader import load_tasks_from_excel
            task_upload = self.files.get("task_file")
            if task_upload:
                suffix = "." + task_upload["filename"].rsplit(".", 1)[-1].lower() if "." in task_upload["filename"] else ".csv"
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as stream:
                    content = task_upload["content"]
                    stream.write(content.encode("utf-8") if isinstance(content, str) else content)
                    task_path = stream.name
                try:
                    tasks = load_tasks_from_excel(task_path)
                    if hasattr(self.engine, "submit_tasks"):
                        self.engine.submit_tasks(tasks)
                    else:
                        for task in tasks:
                            self.engine.submit_task(task)
                finally:
                    try:
                        import os
                        os.unlink(task_path)
                    except OSError:
                        pass
            else:
                task_path = resolve_path(load_platform_config().get("tasks", {}).get("excel_file"))
                if task_path:
                    tasks = load_tasks_from_excel(task_path)
                    if hasattr(self.engine, "submit_tasks"):
                        self.engine.submit_tasks(tasks)
                    else:
                        for task in tasks:
                            self.engine.submit_task(task)
            self.tasks_loaded_for = self.engine
        if hasattr(self.engine, "start") and not getattr(self.engine, "running", False):
            self.engine.start()

    def stop(self) -> None:
        if self.engine is not None and hasattr(self.engine, "stop"):
            self.engine.stop()

    def status(self) -> dict[str, Any]:
        engine = self.engine
        latest = engine.get_latest_stats() if engine is not None and hasattr(engine, "get_latest_stats") else {}
        if latest:
            # A failed or not-yet-due calculation may temporarily return
            # null.  Keep the last real measurement until a new one arrives.
            self.last_telemetry.update({key: value for key, value in latest.items() if value is not None})
            latest = dict(self.last_telemetry)
        carbon = engine.get_total_carbon_summary() if engine is not None and hasattr(engine, "get_total_carbon_summary") else {}
        optimization = engine.get_optimization_result() if engine is not None and hasattr(engine, "get_optimization_result") else {}
        if self.configuration is None:
            state = "not_configured"
        elif engine is None:
            state = "configured"
        elif getattr(engine, "running", False):
            state = "running"
        else:
            state = "stopped"
        return {"status": state, "simulation_second": getattr(engine, "current_second", 0) if engine else 0, "latest_stats": latest or {}, "carbon": carbon or {}, "optimization": optimization or {}, "configuration": self.configuration}


def _session(request: Request) -> FrontendSession:
    return request.app.state.frontend_session


@router.post("/config/apply")
async def apply_config(request: Request):
    session = _session(request)
    if session.engine is not None and getattr(session.engine, "running", False):
        raise HTTPException(status_code=409, detail="Stop the current simulation before applying a new configuration")
    fields, uploads = await _request_parts(request)
    dispatch_strategy = fields.get("dispatch_strategy")
    pricing_strategy = fields.get("pricing_strategy", "fixed_tou")
    optimization_model = fields.get("optimization_model", "heuristic")
    objective_strategy = fields.get("objective_strategy", "min_cost")
    topology = fields.get("topology", "IEEE 39-bus system")
    pricing_csv = uploads.get("pricing_csv")
    user_config = uploads.get("user_config")
    task_file = uploads.get("task_file")
    custom_dispatch_file = uploads.get("custom_dispatch_file")
    custom_pricing_file = uploads.get("custom_pricing_file")
    custom_objective_file = uploads.get("custom_objective_file")
    topology_file = uploads.get("topology_file")
    if pricing_strategy == "fixed_tou" and pricing_csv is None:
        raise HTTPException(status_code=422, detail="Import a time-of-use pricing CSV file")
    if pricing_csv is not None and (not pricing_csv["filename"] or not pricing_csv["filename"].lower().endswith(".csv")):
        raise HTTPException(status_code=422, detail="Time-of-use pricing must use a CSV file")
    try:
        pricing = _parse_pricing(pricing_csv["content"]) if pricing_csv is not None else {}
        parsed_config = None
        files = {"pricing_csv": {"filename": pricing_csv["filename"], "content": pricing}} if pricing_csv is not None else {}
        custom_dispatch = json.loads(fields["custom_dispatch_config"]) if fields.get("custom_dispatch_config") else None
        custom_pricing = json.loads(fields["custom_pricing_config"]) if fields.get("custom_pricing_config") else None
        custom_objective = json.loads(fields["custom_objective_config"]) if fields.get("custom_objective_config") else None
        uploaded_topology = _parse_topology(topology_file["content"], topology_file["filename"]) if topology_file is not None else None
        if topology_file is not None:
            files["topology_file"] = {"filename": topology_file["filename"], "content": uploaded_topology}
        if custom_dispatch_file is not None:
            custom_dispatch = _parse_config(custom_dispatch_file["content"], custom_dispatch_file["filename"])
            files["custom_dispatch_file"] = {"filename": custom_dispatch_file["filename"], "content": custom_dispatch}
        if custom_pricing_file is not None:
            custom_pricing = _parse_config(custom_pricing_file["content"], custom_pricing_file["filename"])
            files["custom_pricing_file"] = {"filename": custom_pricing_file["filename"], "content": custom_pricing}
        if custom_objective_file is not None:
            custom_objective = _parse_config(custom_objective_file["content"], custom_objective_file["filename"])
            files["custom_objective_file"] = {"filename": custom_objective_file["filename"], "content": custom_objective}
        if user_config is not None:
            if not user_config["filename"] or not user_config["filename"].lower().endswith((".yaml", ".yml", ".json")):
                raise ValueError("User configuration data must be a YAML, YML, or JSON file")
            parsed_config = _parse_config(user_config["content"], user_config["filename"])
            files["user_config"] = {"filename": user_config["filename"], "content": parsed_config}
        if task_file is not None:
            # Preserve binary XLSX uploads; the loader decides how to decode
            # the file based on its suffix at simulation start.
            files["task_file"] = {"filename": task_file["filename"] or "tasks", "content": task_file["content"]}
        return session.apply(dispatch_strategy=dispatch_strategy, pricing_strategy=pricing_strategy, optimization_model=optimization_model, objective_strategy=objective_strategy, topology=topology, user_config=parsed_config, pricing=pricing, files=files, custom_dispatch=custom_dispatch, custom_pricing=custom_pricing, custom_objective=custom_objective, uploaded_topology=uploaded_topology)
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/simulation/start")
def start_simulation(request: Request):
    try:
        _session(request).start()
    except (RuntimeError, ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "running"}


@router.post("/simulation/stop")
def stop_simulation(request: Request):
    _session(request).stop()
    return {"status": "stopped"}


@router.get("/simulation/status")
def simulation_status(request: Request):
    return _session(request).status()


@router.get("/export")
def export_results(request: Request, format: str = "json"):
    session = _session(request)
    if session.configuration is None:
        raise HTTPException(status_code=409, detail="Apply the configuration first")
    status_payload = session.status()
    engine = session.engine
    history = (
        list(engine.get_stats_history())
        if engine is not None and hasattr(engine, "get_stats_history")
        else [status_payload.get("latest_stats") or {}]
    )
    history = [record for record in history if record]
    if format == "json":
        payload = json.dumps(
            {**status_payload, "statistics_history": history},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        return StreamingResponse(io.BytesIO(payload), media_type="application/json", headers={"Content-Disposition": "attachment; filename=simulation-result.json"})
    if format not in {"csv", "xlsx", "zip"}:
        raise HTTPException(status_code=422, detail="Export format must be csv, xlsx, zip, or json")
    def flatten(value, prefix=""):
        flat = {}
        if isinstance(value, dict):
            for key, item in value.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                flat.update(flatten(item, name))
        elif isinstance(value, list):
            flat[prefix] = json.dumps(value, ensure_ascii=False)
        else:
            flat[prefix] = value
        return flat

    rows_data = [flatten(record) for record in history]
    if not rows_data:
        rows_data = [{"status": status_payload["status"]}]
    carbon_values = {
        f"carbon_{key}": value
        for key, value in (status_payload.get("carbon") or {}).items()
        if not isinstance(value, (dict, list))
    }
    for row in rows_data:
        row.update(carbon_values)
    fields = []
    for row in rows_data:
        for key in row:
            if key not in fields:
                fields.append(key)
    stream = io.StringIO()
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows_data)
    csv_bytes = stream.getvalue().encode("utf-8-sig")
    rows = [fields] + [[row.get(field, "") for field in fields] for row in rows_data]
    config_rows = [["key", "value"]]
    for key, value in session.configuration.items():
        config_rows.append([key, json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value])
    if format == "csv":
        return Response(csv_bytes, media_type="text/csv", headers={"Content-Disposition": "attachment; filename=simulation-result.csv"})
    if format == "zip":
        json_bytes = json.dumps(
            {**status_payload, "statistics_history": history},
            ensure_ascii=False,
            indent=2,
        ).encode("utf-8")
        archive_buffer = io.BytesIO()
        report_dir = Path(getattr(engine, "report_dir", "reports")) if engine is not None else Path("reports")
        with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("simulation-result.json", json_bytes)
            archive.writestr("simulation-result.csv", csv_bytes)
            archive.writestr("simulation-result.xlsx", _xlsx_bytes(rows, config_rows))
            if report_dir.exists():
                for report_path in sorted(report_dir.iterdir()):
                    if report_path.is_file() and report_path.suffix.lower() in {".csv", ".svg"}:
                        archive.write(report_path, f"reports/{report_path.name}")
        return Response(
            archive_buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": "attachment; filename=simulation-results.zip"},
        )
    return Response(_xlsx_bytes(rows, config_rows), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", headers={"Content-Disposition": "attachment; filename=simulation-result.xlsx"})
