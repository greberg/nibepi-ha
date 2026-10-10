"""Cost optimiser (Phase G6): plan heating and hot water against price.

Pure Python like the rest of ``logic``: no Home Assistant, no numpy.
Design and rationale: docs/optimizer.md in the workspace.

- ``profile``: house profile (area, floors, era, location) -> model priors
- ``heatcurve``: Nibe curve + offset -> supply temperature, calibration
- ``house``: first-order house model and its fit from logged history
- ``energy``: emitter heat and COP -> electricity, calibration
- ``meter``: the user's power/energy meter -> kWh per slot, heat pump share
- ``solar``: sun elevation and cloud -> solar gain proxy
- ``horizon``: price slots + forecast -> planning slots
- ``planner``: dynamic programming over curve offsets, baseline, savings
- ``tank``: hot water tank model and charge planner
- ``explain``: plan blocks with reasons in plain English
- ``gate``: when the optimiser may steer instead of the Phase 1 logic
- ``backtest``: closed-loop simulation against a "true" house
"""
