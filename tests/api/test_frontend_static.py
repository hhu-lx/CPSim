from fastapi.testclient import TestClient

from src.app import create_app


def test_dashboard_and_assets_expose_required_controls():
    client = TestClient(create_app())
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    html = page.text
    for label in ("Start Simulation", "Stop Simulation", "Submit User Configuration", "Import TOU Pricing CSV", "Task Dispatch Strategy", "Pricing Strategy", "Optimization Model", "Predefined: Equipment Power Time Series", "Export Data"):
        assert label in html
    assert "暂停" not in html
    assert html.count('class="chart"') == 4
    assert "Power Supply and Demand" in html
    assert "Renewable Utilization" in html
    assert "Datacenter Compute and Power" in html
    assert "24-Hour Generation" in html
    assert "Generation Power Curve" not in html
    assert "Load Power Curve" not in html
    assert "Dynamic Pricing" in html
    assert "Marginal Pricing" not in html
    assert "flow-legend" not in html
    assert "More ›" not in html
    assert "<small>SOC —</small>" in html
    for marker in ('runtimeOverview', 'resourcePanel', 'queuePanel', 'alertsPanel', 'totalCompute'):
        assert marker in html
    assert 'id="effectiveDataCenters"' not in html
    assert 'id="effectiveDataHint"' not in html
    assert 'id="topologyFile"' in html
    assert 'accept=".json"' in html
    assert 'id="topologyState"' in html
    assert '/static/app.js?v=' in html
    assert '/static/status_view.js?v=' in html

    asset = client.get("/static/app.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "no-store"
    assert "simulation/status" in asset.text
    assert "File staged; click “Apply Configuration” to submit" in asset.text
    assert "running_compute_by_datacenter" in asset.text
    assert "stackedBarChart" in asset.text
    assert "datacenter_runtime" in asset.text
    assert "renderHealth" in asset.text
    assert "effective_data_centers" in asset.text
    assert "form.append('topology_file'" in asset.text
    assert "topology_summary" in asset.text
    assert "buildTrendSeries" in asset.text
    assert "upsertRollingHistory" in asset.text
    assert "upsertLongTermHistory" in asset.text
    assert "combinedComputeChart" in asset.text
    assert "longTermPowerChart" in asset.text

    trend_asset = client.get("/static/trend_charts.js")
    assert trend_asset.status_code == 200

    status_asset = client.get("/static/status_view.js")
    assert status_asset.status_code == 200
    assert status_asset.headers["cache-control"] == "no-store"
