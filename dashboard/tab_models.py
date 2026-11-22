"""Models tab: training runs, scores and the active model (D-011, D-018)."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from app import config, dashboard_data as dd, model_registry
from dashboard.common import _LIGHT, _chart, _color, _style


def _intent_performance(record: dict) -> tuple[pd.DataFrame, pd.DataFrame]:
    performance, by_intent = [], []
    for name, result in record["models"].items():
        for split in ("train", "test"):
            if split not in result:
                continue
            scores = result[split]
            report = scores["classification_report"]
            matrix = scores["confusion_matrix"]
            total = sum(int(report[label]["support"]) for label in record["labels"])
            correct = sum(matrix[index][index] for index in range(len(record["labels"])))
            performance.append({
                "model": name,
                "set": split,
                "emails fully right": f"{correct} of {total}",
                "precision": f"{scores['macro_precision']:.1%}",
                "recall": f"{scores['macro_recall']:.1%}",
                "F1": f"{scores['macro_f1']:.1%}",
            })
            by_intent.extend({
                "model": name,
                "set": split,
                "intent": label,
                "emails": int(report[label]["support"]),
                "precision": f"{report[label]['precision']:.1%}",
                "recall": f"{report[label]['recall']:.1%}",
                "F1": f"{report[label]['f1-score']:.1%}",
            } for label in record["labels"])
    return pd.DataFrame(performance), pd.DataFrame(by_intent)


def render_models() -> None:
    task = st.radio("Model", model_registry.TASKS, horizontal=True, format_func=str.title, key="model_task")
    trained = [
        name for name in model_registry.CHOICES[task]
        if name == model_registry.REGEX or model_registry.model_path(task, name).exists()
    ]
    current = model_registry.get_active(task)
    left, right = st.columns([2, 1])
    choice = left.selectbox(
        f"Active {task} model (used from the next run)",
        trained,
        index=trained.index(current) if current in trained else None,
        placeholder="None chosen",
        key=f"active_{task}",
    )
    if choice and choice != current and right.button("Use this model", type="primary"):
        model_registry.set_active(task, choice)
        st.success(f"{task.title()} model set to {choice}.")
        st.rerun()

    runs = list(reversed(model_registry.list_history(task)))
    if not runs:
        st.info("No training run recorded yet. Train in the Jobs tab.")
        return
    path = st.selectbox("Training run", runs, format_func=lambda p: p.stem.split("_")[0], key=f"run_{task}")
    record = model_registry.load_history(path)
    support = ", ".join(f"{label} {record['test_labels'].count(label)}" for label in record["labels"])
    if "evaluation" in record:
        st.caption(
            f"Scores from {record['evaluation']} on {record['n_test']} examples ({support}): each was "
            "predicted by a model that never saw it. The saved models learned from all of them; "
            "\"train macro F1\" is that final model on its own training data."
        )
    else:  # runs recorded before cross-validation (one 80/20 split)
        st.caption(f"{record['n_train']} training and {record['n_test']} test examples; test support: {support}")
    if "emails" in record:  # number runs: the comparison that matters (D-026)
        st.subheader("Numbers found per email")
        percent = lambda value: "n/a" if value is None else f"{value:.1%}"
        # Train rows (the saved model on its own emails) next to test rows
        # (emails it never saw) show over- or underfitting. Older runs have no train rows.
        rows_by_set = [
            *((name, "train", scores) for name, scores in record.get("emails_train", {}).items()),
            *((name, "test" if name != "regex" else "-", scores) for name, scores in record["emails"].items()),
        ]
        st.dataframe(
            pd.DataFrame([
                {
                    "method": name,
                    "set": split,
                    "emails fully right": f"{scores['exact']} of {scores['emails']}",
                    "precision": percent(scores["precision"]),
                    "recall": percent(scores["recall"]),
                    # Older runs saved no F1.
                    "F1": percent(scores.get("f1")),
                    "requests left without a number": scores["no_number"],
                }
                for name, split, scores in rows_by_set
            ]),
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "Precision: share of the numbers found that are right (a wrong one holds the email and "
            "lands in POD Outstanding as Not in The System). Recall: share of the requested numbers "
            "found. The CRF falls back to regex when it finds nothing. Train: the saved model on the "
            "emails it learned from; test: emails it never saw. Train far above test means "
            "overfitting; both low means underfitting. Compare test with regex. The tables below "
            "score each token that holds a digit."
        )
    if task == "intent":
        st.subheader("Emails classified")
        performance, by_intent = _intent_performance(record)
        st.dataframe(performance, hide_index=True, width="stretch")
        st.caption(
            "Precision, recall, and F1 are macro averages across intents. Train scores are on the saved "
            "model's training emails; test scores are cross-validated, with each email predicted by a "
            "model that did not train on it."
        )
        st.subheader("Scores by intent")
        st.dataframe(by_intent, hide_index=True, width="stretch")
    else:
        rows = []
        for name, result in record["models"].items():
            test = result["test"]
            report = test["classification_report"]
            rows.append({
                "model": name,
                "accuracy": test["accuracy"],
                "macro precision": test["macro_precision"],
                "macro recall": test["macro_recall"],
                "macro F1": test["macro_f1"],
                "train macro F1": result["train"]["macro_f1"],
                **{f"F1 {label}": report[label]["f1-score"] for label in record["labels"]},
            })
        st.dataframe(pd.DataFrame(rows).style.format(precision=3), hide_index=True, width="stretch")

    model_name = st.selectbox("Confusion matrix of", list(record["models"]), key=f"cm_{task}")
    matrix = record["models"][model_name]["test"]["confusion_matrix"]
    cells = pd.DataFrame(
        [
            {"actual": actual, "predicted": predicted, "emails": matrix[i][j]}
            for i, actual in enumerate(record["labels"])
            for j, predicted in enumerate(record["labels"])
        ]
    )
    style = _style()
    heat = alt.Chart(cells).mark_rect(stroke=style["surface"], strokeWidth=2).encode(
        x=alt.X("predicted:N", sort=record["labels"], title="Predicted", axis=alt.Axis(labelAngle=0)),
        y=alt.Y("actual:N", sort=record["labels"], title="Actual"),
        color=alt.Color("emails:Q", scale=alt.Scale(range=style["sequential"]), legend=None),
        tooltip=["actual:N", "predicted:N", "emails:Q"],
    )
    text = alt.Chart(cells).mark_text(fontSize=14).encode(
        x=alt.X("predicted:N", sort=record["labels"]),
        y=alt.Y("actual:N", sort=record["labels"]),
        text="emails:Q",
        # Dark cells get white text, light cells black; which end is dark
        # depends on the mode, because the dark ramp runs the other way.
        color=alt.condition(
            alt.datum.emails > max(max(row) for row in matrix) / 2,
            alt.value("white" if style is _LIGHT else "black"),
            alt.value("black" if style is _LIGHT else "white"),
        ),
    )
    _chart(heat + text, cells, f"confusion matrix, {model_name}")

    if task != "intent":
        return
    st.subheader("What the agent would do at a confidence threshold")
    threshold = st.slider(
        "Threshold", 0.50, 0.99, float(config.current_setting("POD_CONFIDENCE_THRESHOLD", config.DEFAULT_CONFIDENCE_THRESHOLD)), 0.01, key="model_threshold"
    )
    table = pd.DataFrame(
        [{"model": name, **model_registry.at_threshold(record, name, threshold)} for name in record["models"]]
    )
    # Shown as text so "nothing acted on" reads as a sentence, not "None".
    for column in ("precision", "coverage"):
        table[column] = ["nothing acted on" if pd.isna(value) else f"{value:.1%}" for value in table[column]]
    st.dataframe(table, hide_index=True, width="stretch")
    st.caption(
        "Precision: share of the emails the agent acts on that really are POD requests. "
        f"Coverage: share of all POD requests it acts on. The precision target is {config.precision_target():.0%} "
        "(Settings tab, D-054)."
    )
    curve = pd.DataFrame(
        [
            {"threshold": t / 100, "measure": measure, "value": model_registry.at_threshold(record, model_name, t / 100)[key]}
            for t in range(50, 100)
            for measure, key in (("Precision", "precision"), ("Coverage", "coverage"))
        ]
    ).dropna()
    lines = alt.Chart(curve).mark_line(strokeWidth=2).encode(
        x=alt.X("threshold:Q", title="Threshold", scale=alt.Scale(domain=[0.5, 1])),
        y=alt.Y("value:Q", title=None, axis=alt.Axis(format="%"), scale=alt.Scale(domain=[0, 1])),
        color=_color("measure", ("Precision", "Coverage")),
        tooltip=["threshold:Q", "measure:N", alt.Tooltip("value:Q", format=".1%")],
    )
    rule = alt.Chart(pd.DataFrame({"threshold": [threshold]})).mark_rule(strokeDash=[4, 4]).encode(x="threshold:Q")
    _chart(lines + rule, curve, f"precision and coverage, {model_name}")

    history = []
    for run_number, run_path in enumerate(model_registry.list_history(task), start=1):
        run = model_registry.load_history(run_path)
        for name, result in run["models"].items():
            history.append({
                "run": f"#{run_number}",
                "trained": pd.Timestamp(run["trained_at"]).tz_convert(dd.local_now().tz).strftime("%d %b %Y %H:%M"),
                "model": name,
                "macro F1": result["test"]["macro_f1"],
            })
    if len({row["run"] for row in history}) > 1:
        st.subheader("Test macro F1 over training runs")
        frame = pd.DataFrame(history)
        chart = alt.Chart(frame).mark_line(strokeWidth=2, point=alt.OverlayMarkDef(size=64, filled=True)).encode(
            x=alt.X("run:O", title="Training run", sort=None, axis=alt.Axis(labelAngle=0)),
            y=alt.Y("macro F1:Q", scale=alt.Scale(domain=[0, 1])),
            # Current candidates first, then models seen only in older runs,
            # so a colour always belongs to the same model (palette has five).
            color=_color(
                "model",
                tuple(dict.fromkeys([
                    *(model_registry.INTENT_MODEL_TYPES if task == "intent" else model_registry.NUMBER_MODEL_TYPES),
                    *sorted(frame["model"].unique()),
                ]))[:5],
            ),
            tooltip=["run:O", "trained:N", "model:N", alt.Tooltip("macro F1:Q", format=".3f")],
        )
        _chart(chart, frame, "macro F1 per run")
