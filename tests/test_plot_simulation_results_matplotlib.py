import pandas as pd

from scripts.plot_simulation_results_matplotlib import generation_stack_data


def test_generation_stack_groups_other_generation_and_signed_storage():
    data = pd.DataFrame({
        "wind_generation_mw": [10.0],
        "pv_generation_mw": [20.0],
        "conventional_generation_mw": [30.0],
        "base_generation_mw": [40.0],
        "grid_import_mw": [5.0],
        "storage_power_mw": [-7.0],
        "total_load": [90.0],
    })

    stack = generation_stack_data(data)

    assert list(stack) == ["Others", "Thermal Power Generation", "Wind", "PV"]
    assert stack["Thermal Power Generation"].iloc[0] == 70.0
    assert stack["Others"].iloc[0] == -2.0
