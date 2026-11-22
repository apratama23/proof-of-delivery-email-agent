"""Train and run the email-intent classifier."""

import argparse
import re
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import snowballstemmer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import StratifiedKFold
from sklearn.pipeline import Pipeline
from sklearn.base import BaseEstimator, ClassifierMixin
from xgboost import XGBClassifier

from . import config, model_registry


INTENT_CLASSES = (
    "POD_REQUEST",
    "OTHER",
)
PSEUDO_LABEL_COLUMNS = (
    "intent_label_pseudo",
    "pseudo_confidence",
    "pseudo_verified",
)


class IntentXGBClassifier(ClassifierMixin, BaseEstimator):
    """Use numeric labels internally while preserving intent labels externally."""

    def __init__(self):
        self.classes_ = np.asarray(INTENT_CLASSES)
        self.classifier = XGBClassifier(
            n_estimators=300,
            max_depth=6,
            learning_rate=0.05,
            subsample=0.8,
            colsample_bytree=0.8,
            objective="binary:logistic",
            eval_metric="logloss",
            random_state=42,
            n_jobs=-1,
        )

    def fit(self, features, labels):
        label_to_number = {label: index for index, label in enumerate(self.classes_)}
        numeric_labels = np.asarray([label_to_number[label] for label in labels])
        self.classifier.fit(features, numeric_labels)
        return self

    def predict(self, features):
        numeric_predictions = self.classifier.predict(features).astype(int)
        return self.classes_[numeric_predictions]

    def predict_proba(self, features):
        return self.classifier.predict_proba(features)


if __name__ == "__main__":
    from app.classifier import IntentXGBClassifier as _ImportedIntentXGBClassifier

    IntentXGBClassifier = _ImportedIntentXGBClassifier


def _text_value(value) -> str:
    return "" if value is None or pd.isna(value) else str(value)


_URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_HAS_DIGIT_PATTERN = re.compile(r"\b[\w-]*\d[\w-]*\b")


def normalize_text(text: str) -> str:
    """Replace one-off values with shared tokens, collapse whitespace, lower-case.

    Every connote, reference, date, URL and email address is unique, so as raw
    words they teach the model nothing and bloat the vocabulary; as
    `numtoken`, `urltoken` and `emailtoken` they carry the signal "a number /
    link / address is here". Measured on the real training data (5-fold CV,
    D-016): same F1, higher precision and coverage at the 0.90 threshold.
    Tokenizing and bigrams are done by the TF-IDF vectorizer.
    """
    text = _URL_PATTERN.sub(" urltoken ", text)
    text = _EMAIL_PATTERN.sub(" emailtoken ", text)
    text = _HAS_DIGIT_PATTERN.sub(" numtoken ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def sender_feature(sender) -> str:
    """The sender as the intent model sees it: "internal" or the sender's domain.

    Older pulls stored internal senders as Exchange paths (/O=...), while the
    live agent reads their SMTP address; both become "internal" so training
    and prediction see the same input. External senders count by domain, so
    a new person at a known customer is still recognised.
    """
    address = _text_value(sender).strip().lower()
    if address.startswith("/o="):
        return "internal"
    if "@" not in address:
        return "unknown"
    domain = address.rpartition("@")[2]
    own = config.POD_SHARED_MAILBOX_ADDRESS.lower().rpartition("@")[2]
    return "internal" if own and domain == own else domain


def build_email_text(
    subject: str = "",
    sender_email: str = "",
    to_recipients: str = "",
    body: str = "",
) -> str:
    """Build the shared NLP input used during training and live prediction.

    Subject and body are normalized; the sender is reduced to "internal" or
    its domain (`sender_feature`); the recipients are kept as they are,
    because who wrote to which address is itself a signal.
    """
    return (
        f"Subject: {normalize_text(_text_value(subject))}\n"
        f"Sender: {sender_feature(sender_email)}\n"
        f"To: {_text_value(to_recipients)}\n"
        f"Body: {normalize_text(_text_value(body))}"
    ).strip()


def build_training_features(training_df: pd.DataFrame) -> pd.Series:
    """Combine email subject, sender, recipients, and body into model features."""
    return training_df.apply(
        lambda row: build_email_text(
            row.get("subject", ""),
            row.get("sender_email", ""),
            row.get("to_recipients", ""),
            row.get("body", ""),
        ),
        axis=1,
    )


def load_training_data(path: str | Path) -> pd.DataFrame:
    """Load manual labels and pseudo-labels that have been verified."""
    if not Path(path).exists():
        return pd.DataFrame(columns=["body", "intent_label"])
    data = pd.read_excel(path)
    required_columns = {"body", "intent_label"}
    missing_columns = required_columns.difference(data.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Training file is missing required columns: {missing}")

    labeled = data[data["body"].notna()].copy()
    labeled["body"] = labeled["body"].astype(str)
    labeled["intent_label"] = labeled["intent_label"].fillna("").astype(str).str.strip()
    if set(PSEUDO_LABEL_COLUMNS).issubset(data.columns):
        pseudo_verified = data["pseudo_verified"].astype(str).str.casefold().isin(
            {"true", "1", "yes"}
        )
        pseudo_labels = data["intent_label_pseudo"].fillna("").astype(str).str.strip()
        use_pseudo = labeled["intent_label"].eq("") & pseudo_verified & pseudo_labels.ne("")
        labeled.loc[use_pseudo, "intent_label"] = pseudo_labels[use_pseudo]
    return labeled[labeled["intent_label"] != ""]


def has_enough_training_data(
    training_df: pd.DataFrame, min_per_class: int
) -> bool:
    """Return whether every supported intent has enough labeled examples."""
    counts = training_df["intent_label"].value_counts()
    return all(counts.get(intent, 0) >= min_per_class for intent in INTENT_CLASSES)


_STEMMER = snowballstemmer.stemmer("english")


def stem_text(text: str) -> str:
    """The text with each word stemmed, then lower-cased, for the intent model (D-016).

    Words are split at spaces and stemmed as written, so "plans" is plan and
    "sending" is send; the vectorizer then drops punctuation. Measured: as
    good as without stemming at the threshold used, slightly fewer non-POD
    emails answered when fewer POD requests are answered automatically.
    Part of the saved model: a model trained before this keeps its own text
    rule, so stemming starts with the next retraining.
    """
    return " ".join(_STEMMER.stemWords(text.split())).lower()


if __name__ == "__main__":
    from app.classifier import stem_text as _ImportedStemText

    stem_text = _ImportedStemText


def _build_model(model_type: str) -> Pipeline:
    """xgboost is the intent model; logreg is its simple yardstick (D-024)."""
    if model_type == "xgboost":
        classifier = IntentXGBClassifier()
    elif model_type == "logreg":
        classifier = LogisticRegression(max_iter=1000, class_weight="balanced")
    else:
        # An unknown name must fail, not silently become another model.
        raise ValueError(f"Unknown intent model: {model_type}")
    return Pipeline(
        [
            ("tfidf", TfidfVectorizer(preprocessor=stem_text, ngram_range=(1, 2))),
            ("classifier", classifier),
        ]
    )


def train_and_compare(training_df: pd.DataFrame, training_file: str | Path = "") -> Path:
    """Cross-validate every intent model, save each fit on all data, record history.

    Scores come from 5-fold cross-validation (D-022); the saved models learn
    from every labeled email. No model is activated; the product owner chooses one
    (model_registry.set_active). Returns the history file path.
    """
    if training_df.empty:
        raise ValueError("Cannot train classifiers with no labeled emails")

    features = build_training_features(training_df).tolist()
    labels = training_df["intent_label"].astype(str).tolist()
    smallest_class = min(labels.count(label) for label in set(labels))
    if len(set(labels)) < 2 or smallest_class < 2:
        raise ValueError("Need at least 2 labeled emails in each intent class")
    folds = min(model_registry.FOLDS, smallest_class)
    splits = list(StratifiedKFold(folds, shuffle=True, random_state=42).split(features, labels))
    print(f"Cross-validating on {len(labels)} labeled emails, {folds} folds.", flush=True)

    results: dict[str, object] = {}
    model_types = model_registry.INTENT_MODEL_TYPES
    for model_type in model_types:
        print(f"Training and cross-validating {model_type}...", flush=True)
        model, results[model_type] = model_registry.cross_validate(
            lambda _labels, name=model_type: _build_model(name), features, labels, splits, INTENT_CLASSES
        )
        destination = model_registry.model_path("intent", model_type)
        destination.parent.mkdir(parents=True, exist_ok=True)
        print(f"Saving {model_type} model...", flush=True)
        joblib.dump(model, destination)

    history_path = model_registry.save_history(
        "intent",
        {
            "training_file": str(training_file),
            "evaluation": f"{folds}-fold cross-validation",
            "n_train": len(labels),
            "n_test": len(labels),
            "labels": list(INTENT_CLASSES),
            "test_labels": labels,
            "models": results,
        },
    )

    print(f"\nClassifier comparison ({folds}-fold cross-validation; train = final model on all data)")
    print(
        f"{'Model':<14} {'Set':<6} {'Accuracy':>10} "
        f"{'Macro Prec.':>12} {'Macro Recall':>13} {'Macro F1':>10}"
    )
    for model_type in model_types:
        for split in ("train", "test"):
            metrics = results[model_type][split]
            print(
                f"{model_type:<14} {split:<6} "
                f"{metrics['accuracy']:>10.3f} "
                f"{metrics['macro_precision']:>12.3f} "
                f"{metrics['macro_recall']:>13.3f} "
                f"{metrics['macro_f1']:>10.3f}"
            )
    print(f"History saved: {history_path}")
    print(
        "No model was activated. Choose one with: "
        "python -m app.model_registry --task intent --activate <model>"
    )
    return history_path


def predict(model, text: str) -> tuple[str, float]:
    """Return the predicted intent and its probability-based confidence."""
    if model is None:
        raise ValueError("A trained classifier model is required")

    predicted_label = str(model.predict([text])[0])
    probabilities = model.predict_proba([text])[0]
    confidence = float(max(probabilities))
    return predicted_label, confidence


def generate_pseudo_labels(
    training_path: str | Path,
    threshold: float = 0.95,
) -> int:
    """Replace pseudo-labels for rows that do not have a manual intent label."""
    source = Path(training_path)
    data = pd.read_excel(source)
    required_columns = {"body", "intent_label"}
    missing_columns = required_columns.difference(data.columns)
    if missing_columns:
        missing = ", ".join(sorted(missing_columns))
        raise ValueError(f"Training file is missing required columns: {missing}")

    model = model_registry.load_active("intent")
    if model is None:
        raise FileNotFoundError(
            "No intent model is active. Choose one with: "
            "python -m app.model_registry --task intent --activate <model>"
        )
    for column in PSEUDO_LABEL_COLUMNS:
        if column not in data.columns:
            data[column] = pd.Series(pd.NA, index=data.index, dtype="object")
        else:
            data[column] = pd.Series(
                data[column].tolist(), index=data.index, dtype="object"
            )

    # Reset old pseudo-labels, except the agent's decisions awaiting review.
    system_rows = (
        data["label_source"].fillna("").astype(str).eq("System decision")
        if "label_source" in data.columns
        else pd.Series(False, index=data.index)
    )
    data.loc[~system_rows, "intent_label_pseudo"] = None
    data.loc[~system_rows, "pseudo_confidence"] = None
    data.loc[~system_rows, "pseudo_verified"] = False

    generated = 0
    total_rows = len(data)
    progress_step = max(1, total_rows // 20)
    print(f"Generating pseudo-labels for {total_rows} rows...", flush=True)
    for row_number, (index, row) in enumerate(data.iterrows(), start=1):
        manual_value = row.get("intent_label", "")
        body_value = row.get("body", "")
        manual_label = "" if pd.isna(manual_value) else str(manual_value).strip()
        body = "" if pd.isna(body_value) else str(body_value).strip()
        if manual_label or not body:
            continue
        if str(row.get("label_source", "") or "") == "System decision":
            # Keep the agent's own decision for the product owner to review (D-013).
            continue
        text = build_email_text(
            row.get("subject", ""),
            row.get("sender_email", ""),
            row.get("to_recipients", ""),
            body,
        )
        label, confidence = predict(model, text)
        data.at[index, "intent_label_pseudo"] = (
            label if confidence >= threshold else ""
        )
        data.at[index, "pseudo_confidence"] = confidence
        data.at[index, "pseudo_verified"] = False
        if confidence >= threshold:
            generated += 1
        if row_number % progress_step == 0 or row_number == total_rows:
            progress = row_number / total_rows if total_rows else 1.0
            print(
                f"Progress: {row_number}/{total_rows} ({progress:.0%}) - "
                f"{generated} labels generated",
                flush=True,
            )

    print(f"Saving pseudo-label results to {source.name}...", flush=True)
    data.to_excel(source, index=False)
    print(f"Pseudo-labeling complete: {generated} labels generated.", flush=True)
    return generated


if __name__ == "__main__":
    from . import config

    config.training_data_path()  # stops naming POD_TRAINING_DATA_FILE when it is not set (D-060)
    parser = argparse.ArgumentParser(description="Train and compare intent classifiers")
    parser.add_argument(
        "--pseudo-label",
        action="store_true",
        help="Write predictions of the active model for rows without a manual label",
    )
    parser.add_argument("--threshold", type=float, default=0.95)
    args = parser.parse_args()
    # The sender feature needs the POD mailbox's domain to recognise internal senders.
    config.shared_mailbox_address()
    if args.pseudo_label:
        generated = generate_pseudo_labels(config.TRAINING_DATA_PATH, args.threshold)
        print(f"Generated {generated} pseudo-labels")
    elif not Path(config.TRAINING_DATA_PATH).exists():
        # load_training_data returns no rows for a missing file (the agent relies on that).
        raise SystemExit(f"Training file not found: {config.TRAINING_DATA_PATH}")
    else:
        print("Loading training data...", flush=True)
        training_data = load_training_data(config.TRAINING_DATA_PATH)
        print(f"Loaded {len(training_data)} labeled rows.", flush=True)
        train_and_compare(training_data, config.TRAINING_DATA_PATH)
