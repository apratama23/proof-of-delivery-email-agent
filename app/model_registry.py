"""Active model choice and training history for the intent and number models.

The product owner chooses the active model per task (D-011); nothing is activated
automatically. Every training run is kept as a dated JSON file so runs can
be compared later.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.metrics import accuracy_score, classification_report, confusion_matrix

from . import config

TASKS = ("intent", "number")
# xgboost is the intent model; logreg stays as a yardstick: if xgboost ever
# falls to logreg's level after retraining, something is wrong (D-024).
INTENT_MODEL_TYPES = ("xgboost", "logreg")
# The number model is a CRF (app/number_crf.py); regex is its yardstick and
# fallback (D-026).
NUMBER_MODEL_TYPES = ("crf",)
REGEX = "regex"
CHOICES = {
    "intent": INTENT_MODEL_TYPES,
    "number": (*NUMBER_MODEL_TYPES, REGEX),
}


def _models_dir() -> Path:
    # Read at call time so tests can point config.MODELS_DIR at a temp folder.
    return Path(config.MODELS_DIR)


def model_path(task: str, name: str) -> Path:
    """Return where the trained model `name` for `task` is saved."""
    if task == "intent":
        return _models_dir() / f"classifier_model_{name}.pkl"
    if task == "number":
        return _models_dir() / "number" / f"model_{name}.pkl"
    raise ValueError(f"Unknown task: {task}")


def _active_path() -> Path:
    return _models_dir() / "active.json"


def _read_active() -> dict[str, str]:
    path = _active_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def get_active(task: str) -> str | None:
    """Return the chosen model name for `task`, or None if none was chosen."""
    return _read_active().get(task)


def set_active(task: str, name: str) -> None:
    """Record the product owner's model choice; refuses unknown or untrained models."""
    if name not in CHOICES.get(task, ()):
        allowed = ", ".join(CHOICES.get(task, ()))
        raise ValueError(f"'{name}' is not a {task} model. Choose one of: {allowed}")
    if name != REGEX and not model_path(task, name).exists():
        raise FileNotFoundError(
            f"No trained {task} model '{name}' at {model_path(task, name)}. Train first."
        )
    active = _read_active()
    active[task] = name
    path = _active_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(active, indent=2) + "\n", encoding="utf-8", newline="\n")


def load_active(task: str):
    """Load the chosen model, or None when none is chosen or it is `regex`.

    None for intent means cold start (every email goes to manual review);
    None for numbers means the regex extractor is used.
    """
    name = get_active(task)
    if name is None or name == REGEX:
        return None
    return joblib.load(model_path(task, name))


def evaluate(labels: list[str], true_labels, predictions) -> dict[str, object]:
    """Accuracy, per-class and macro precision/recall/F1, confusion matrix."""
    report = classification_report(
        true_labels,
        predictions,
        labels=list(labels),
        output_dict=True,
        zero_division=0,
    )
    return {
        "accuracy": float(accuracy_score(true_labels, predictions)),
        "macro_precision": float(report["macro avg"]["precision"]),
        "macro_recall": float(report["macro avg"]["recall"]),
        "macro_f1": float(report["macro avg"]["f1-score"]),
        "classification_report": report,
        "confusion_matrix": confusion_matrix(
            true_labels, predictions, labels=list(labels)
        ).tolist(),
    }


FOLDS = 5


def cross_validate(build, inputs, labels, folds, label_names) -> tuple[object, dict]:
    """Out-of-fold scores for one model type, and the final model fit on all data (D-022).

    `build(train_labels)` returns an unfitted model for those labels; `folds`
    yields (train, test) index pairs. Every example is predicted exactly once,
    by a model that never saw it, so the scores are honest while the final
    model still learns from 100% of the data. Each prediction and its
    confidence (highest probability) are kept, so precision and coverage at
    any threshold can be computed later without retraining.
    """
    inputs = np.asarray(list(inputs), dtype=object)
    labels = np.asarray([str(label) for label in labels], dtype=object)
    predictions = np.empty(len(labels), dtype=object)
    confidences = np.zeros(len(labels))
    for train_index, test_index in folds:
        model = build(labels[train_index]).fit(inputs[train_index], labels[train_index])
        predictions[test_index] = model.predict(inputs[test_index])
        confidences[test_index] = np.max(model.predict_proba(inputs[test_index]), axis=1)
    final = build(labels).fit(inputs, labels)
    return final, {
        "train": evaluate(label_names, labels, final.predict(inputs)),
        "test": evaluate(label_names, labels, predictions),
        "predictions": [str(label) for label in predictions],
        "confidences": [float(value) for value in confidences],
    }


def at_threshold(record: dict, model_name: str, threshold: float, positive: str = "POD_REQUEST") -> dict:
    """What the agent would do with this model's test predictions at a threshold.

    The agent acts only on emails predicted `positive` with confidence at or
    above the threshold. Precision: share of those that really are
    `positive`. Coverage: share of all real `positive` emails it acts on.
    """
    result = record["models"][model_name]
    acted = [
        truth
        for truth, predicted, confidence in zip(
            record["test_labels"], result["predictions"], result["confidences"]
        )
        if predicted == positive and confidence >= threshold
    ]
    total = record["test_labels"].count(positive)
    correct = acted.count(positive)
    return {
        "acted": len(acted),
        "correct": correct,
        "precision": correct / len(acted) if acted else None,
        "coverage": correct / total if total else None,
    }


def load_history(path: Path) -> dict:
    """One training run record."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def save_history(task: str, record: dict) -> Path:
    """Write one training run to models/history/<UTC timestamp>_<task>.json.

    On Windows, Python 3.10 reads the clock in steps of about 15 ms, so two
    runs saved close together can get the same time; the later one is moved
    on by a microsecond until its name is free, so no run overwrites another
    and the names still sort oldest first.
    """
    history_dir = _models_dir() / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    trained_at = datetime.now(timezone.utc)
    while (history_dir / f"{trained_at:%Y%m%dT%H%M%S%fZ}_{task}.json").exists():
        trained_at += timedelta(microseconds=1)
    path = history_dir / f"{trained_at:%Y%m%dT%H%M%S%fZ}_{task}.json"
    record = {"task": task, "trained_at": trained_at.isoformat(), **record}
    path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def list_history(task: str) -> list[Path]:
    """Training runs for `task`, oldest first."""
    history_dir = _models_dir() / "history"
    if not history_dir.exists():
        return []
    return sorted(history_dir.glob(f"*_{task}.json"))


def main() -> None:
    parser = argparse.ArgumentParser(description="Show or choose the active models")
    parser.add_argument("--task", choices=TASKS)
    parser.add_argument("--activate", help="Model name to make active for --task")
    args = parser.parse_args()
    if args.activate:
        if not args.task:
            parser.error("--activate needs --task")
        set_active(args.task, args.activate)
    for task in TASKS:
        print(f"{task}: {get_active(task) or '(none chosen)'}")


if __name__ == "__main__":
    main()
