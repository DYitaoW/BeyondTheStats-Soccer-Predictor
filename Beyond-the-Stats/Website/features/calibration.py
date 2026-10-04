"""
Scikit-learn probability calibration module for match result models.
Wraps CalibratedClassifierCV with isotonic and sigmoid (Platt scaling) calibration,
preserving multiclass simplex normalization sum(P) == 1.
"""
from __future__ import annotations

import logging
from typing import Any, Literal
import numpy as np
import pandas as pd
from sklearn.calibration import CalibratedClassifierCV

logger = logging.getLogger(__name__)


def create_calibrated_classifier(
    estimator: Any,
    method: Literal["isotonic", "sigmoid"] = "sigmoid",
    cv: int | str = 5,
    ensemble: bool = True,
) -> CalibratedClassifierCV:
    """Create a CalibratedClassifierCV wrapper around a base classifier.
    
    Parameters:
        estimator: The base estimator (e.g., LogisticRegression, RandomForest, XGBoost/LightGBM).
        method: 'sigmoid' (Platt scaling) or 'isotonic' (non-parametric monotonic step).
        cv: Cross-validation generator or integer folds. Use 'prefit' if already trained.
        ensemble: Whether to average predictions of all cv models (True default in scikit-learn).
    """
    return CalibratedClassifierCV(
        estimator=estimator,
        method=method,
        cv=cv,
        ensemble=ensemble,
    )


def calibrate_probabilities_simplex(
    probabilities: dict[str, float] | np.ndarray,
    classes: list[str] | None = None,
    temperature: float = 1.0,
) -> dict[str, float]:
    """Ensure predicted probabilities satisfy simplex constraints:
    p_i in [0, 1] and sum(p_i) == 1.0, with optional temperature scaling.
    
    Parameters:
        probabilities: Dict mapping class -> prob or 1D numpy array.
        classes: List of class labels if probabilities is an array.
        temperature: Scaling factor (T > 1 softens probabilities towards uniform; T < 1 sharpens).
    """
    if isinstance(probabilities, dict):
        keys = list(probabilities.keys())
        raw_vals = np.array([max(1e-7, float(probabilities[k])) for k in keys], dtype=float)
    else:
        raw_vals = np.array([max(1e-7, float(x)) for x in probabilities], dtype=float)
        keys = classes if classes is not None else [str(i) for i in range(len(raw_vals))]

    if temperature != 1.0 and temperature > 0:
        # Apply temperature scaling in log-odds space (logits approximation)
        logits = np.log(raw_vals) / temperature
        exp_logits = np.exp(logits - np.max(logits))
        norm_vals = exp_logits / np.sum(exp_logits)
    else:
        total = np.sum(raw_vals)
        norm_vals = raw_vals / total if total > 0 else np.full_like(raw_vals, 1.0 / len(raw_vals))

    return {k: round(float(v), 5) for k, v in zip(keys, norm_vals)}


def fit_calibrated_model(
    estimator: Any,
    X_train: pd.DataFrame | np.ndarray,
    y_train: pd.Series | np.ndarray,
    method: Literal["isotonic", "sigmoid"] = "sigmoid",
    cv: int = 5,
) -> CalibratedClassifierCV:
    """Convenience pipeline helper to train and calibrate a classifier on training data."""
    calibrated_clf = create_calibrated_classifier(estimator, method=method, cv=cv)
    calibrated_clf.fit(X_train, y_train)
    return calibrated_clf
