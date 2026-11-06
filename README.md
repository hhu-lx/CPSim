# Computing-Power Synergy Simulation Platform

This platform simulates how computing workloads interact with the power grid. It combines task scheduling, renewable energy use, storage dispatch, grid validation, and runtime reporting in one dashboard.

## Optimization modes

- **Baseline**: schedules tasks using resource availability and the selected dispatch policy. It provides a reference result without computing-power coordination.
- **Power-Guided**: lets grid conditions guide task placement and power dispatch together.
- **Computing-Power Synergy**: co-optimizes task placement, start times, generation, renewable usage, storage, and grid exchange in one coordinated decision.

The available dispatch policies are **Lowest Price**, **Low Carbon**, and **Custom**. The available objectives are **Minimum Operating Cost**, **Minimum Renewable Curtailment**, **Comprehensive Weighted Objective**, and **Custom Objective**.

## Required data

### Task data

Upload CSV, XLSX, or JSON data containing the task records. The task table should provide:

- task identifier
- submission or start time
- duration or deadline
- required compute capacity
- optional bandwidth, CPU, GPU, region, and preferred datacenter fields

### Time-of-use pricing

If time-of-use pricing is selected, upload a CSV with:

```text
start_hour,end_hour,price
```

### Power topology (optional)

Upload a topology JSON when the built-in IEEE39 grid is not appropriate. A custom topology should define buses, lines, generators, loads, renewable connections, and storage connections.

### User configuration (optional)

Upload a JSON or YAML configuration to customize simulation settings, datacenters, storage, optimization weights, pricing, or custom strategy parameters.

## How to use the platform

1. Open the dashboard.
2. Upload the task data and any optional pricing, topology, or user configuration files.
3. Select **Baseline**, **Power-Guided**, or **Computing-Power Synergy**.
4. Select a dispatch policy, pricing strategy, and optimization objective.
5. Configure custom weights or parameters when a custom option is selected.
6. Apply the configuration and start the simulation.
7. Monitor grid health, renewable utilization, storage behavior, task queues, datacenter utilization, and power flows in real time.
8. Stop the simulation when finished and export the results as JSON, CSV, Excel, or a ZIP package containing all result files and runtime reports.

## Results

The dashboard reports task progress, datacenter compute and power usage, renewable generation and curtailment, storage activity, grid exchange, grid health, operating cost, and carbon emissions. Exported files contain the recorded simulation results for further analysis.

