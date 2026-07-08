"""Shared helpers for model-cache freshness checks and non-interactive rebuilds."""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from typing import Callable, Optional, Tuple

import joblib


def import_predict_match_module(script_path: str):
    """Load a Predict_Match.py module from an absolute path."""
    script_path = os.path.abspath(script_path)
    module_name = f"predict_match_{abs(hash(script_path))}"
    spec = importlib.util.spec_from_file_location(module_name, script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not import predictor module from {script_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def model_cache_status(pm_mod) -> Tuple[bool, str]:
    """Return ``(needs_rebuild, reason)`` for a Predict_Match module instance."""
    try:
        _matches, season_files = pm_mod.load_training_matches(pm_mod.PROCESSED_DIR)
    except Exception as exc:
        return True, f"cannot load training data ({exc.__class__.__name__})"
    if not season_files:
        return True, "no processed season files"
    if not os.path.exists(pm_mod.MODEL_CACHE):
        return True, "cache file missing"
    try:
        bundle = joblib.load(pm_mod.MODEL_CACHE)
    except Exception as exc:
        return True, f"cache unloadable ({exc.__class__.__name__})"
    fingerprint = pm_mod.data_fingerprint(season_files)
    if bundle.get("fingerprint") != fingerprint:
        bt = bundle.get("build_time")
        if bt is not None:
            age_h = (time.time() - bt) / 3600.0
            return True, f"fingerprint mismatch (cache age {age_h:.1f}h)"
        return True, "fingerprint mismatch (no build_time)"
    return False, "fresh"


def run_model_cache_build(
    predict_script: str,
    cwd: str,
    *,
    build_argv: Optional[list[str]] = None,
    input_text: Optional[str] = None,
    timeout: int = 3600,
) -> None:
    argv = [sys.executable, predict_script]
    if build_argv:
        argv.extend(build_argv)
    proc = subprocess.run(
        argv,
        cwd=cwd,
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        stdout = (proc.stdout or "").strip()
        message = stderr or stdout or f"exit code {proc.returncode}"
        raise RuntimeError(f"Model cache build failed: {message}")


def ensure_model_cache(
    pm_mod,
    predict_script: str,
    cwd: str,
    *,
    build_argv: Optional[list[str]] = None,
    input_text: Optional[str] = None,
    label: str = "model-cache",
) -> None:
    needs, reason = model_cache_status(pm_mod)
    if not needs:
        print(f"[{label}] cache is fresh ({pm_mod.MODEL_CACHE})")
        return
    print(f"[{label}] rebuilding model cache: {reason}")
    run_model_cache_build(
        predict_script,
        cwd,
        build_argv=build_argv,
        input_text=input_text,
    )
    needs_after, reason_after = model_cache_status(pm_mod)
    if needs_after:
        raise RuntimeError(f"Model cache still stale after rebuild: {reason_after}")
    print(f"[{label}] rebuild complete ({pm_mod.MODEL_CACHE})")


def load_model_cache_bundle(pm_mod, season_files, rebuild_fn: Callable[[], None]):
    """Load the model cache, rebuilding once when missing, unloadable, or stale."""
    fingerprint = pm_mod.data_fingerprint(season_files)

    def _reload():
        rebuild_fn()
        return joblib.load(pm_mod.MODEL_CACHE)

    if not os.path.exists(pm_mod.MODEL_CACHE):
        print("[model-cache] cache missing; rebuilding...")
        bundle = _reload()
    else:
        try:
            bundle = joblib.load(pm_mod.MODEL_CACHE)
        except Exception as exc:
            print(f"[model-cache] cache unloadable ({exc.__class__.__name__}); rebuilding...")
            bundle = _reload()

    if bundle.get("fingerprint") != fingerprint:
        print("[model-cache] fingerprint mismatch; rebuilding...")
        bundle = _reload()
        if bundle.get("fingerprint") != fingerprint:
            raise RuntimeError("Model cache fingerprint still mismatched after rebuild.")
    return bundle


def any_pipeline_cache_needs_rebuild(specs: list[tuple[str, str]]) -> Tuple[bool, list[str]]:
    """Check multiple pipelines. Each spec is ``(label, predict_script_path)``."""
    stale: list[str] = []
    for label, script_path in specs:
        if not os.path.exists(script_path):
            stale.append(f"{label}: predictor script missing")
            continue
        pm_mod = import_predict_match_module(script_path)
        needs, reason = model_cache_status(pm_mod)
        if needs:
            stale.append(f"{label}: {reason}")
    return bool(stale), stale
