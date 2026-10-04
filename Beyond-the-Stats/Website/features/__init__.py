"""
Features package initialization.
Exports congestion, exponential recency, and probability calibration modules.
"""
from __future__ import annotations

from .congestion import (
    build_team_schedule_index,
    calculate_team_rest_and_congestion,
)
from .recency import (
    calculate_recency_weight,
    compute_weighted_team_form,
)
from .calibration import (
    create_calibrated_classifier,
    calibrate_probabilities_simplex,
    fit_calibrated_model,
)

__all__ = [
    "build_team_schedule_index",
    "calculate_team_rest_and_congestion",
    "calculate_recency_weight",
    "compute_weighted_team_form",
    "create_calibrated_classifier",
    "calibrate_probabilities_simplex",
    "fit_calibrated_model",
]
