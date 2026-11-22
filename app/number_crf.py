"""The number model: a CRF that tags connote and reference numbers in an email (D-026).

The email (subject and newest message) is split into tokens; the CRF labels
each token CONNOTE, REFERENCE or O from its shape, its digits and letters, its
place in the line, and the words around it ("connote", "ref", "PO"...). It
reads the email as a sequence, which is what let it beat regex on 150
labeled emails where per-token classifiers could not.
"""

from __future__ import annotations

import re
from pathlib import Path

import joblib
import pandas as pd
from sklearn.model_selection import KFold

from . import config, model_registry

NAME = "crf"
LABELS = ("CONNOTE", "REFERENCE", "OTHER")  # OTHER is the CRF's "O"
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]*|[^\sA-Za-z0-9]")
_SIGN_OFF_WORD = re.compile(r"^(regards|kind|thanks|thank|cheers)$", re.IGNORECASE)


def _shape(token: str) -> tuple[str, str]:
    full = re.sub(r"[a-z]", "a", re.sub(r"[A-Z]", "A", re.sub(r"\d", "9", token)))
    return full, re.sub(r"(.)\1+", r"\1", full)


def _word(token: str) -> str:
    """Words as themselves; anything with a digit by its short shape."""
    return _shape(token)[1] if any(c.isdigit() for c in token) else token.lower()


def tokens(subject: str, body: str) -> list[tuple[str, bool, bool, bool]]:
    """(token, in subject, first on its line, after a sign-off word) for each token."""
    text = f"{subject}\n{body}"
    items: list[tuple[str, bool, bool, bool]] = []
    after_sign_off, last_line = False, -1
    for match in _TOKEN.finditer(text):
        line = text.count("\n", 0, match.start())
        token = match.group(0)
        items.append((token, match.start() < len(subject), line != last_line, after_sign_off))
        last_line = line
        if _SIGN_OFF_WORD.match(token):
            after_sign_off = True
    return items


def _features(items: list, index: int) -> dict:
    token, in_subject, line_start, after_sign_off = items[index]
    full, short = _shape(token)
    digits = sum(c.isdigit() for c in token)
    features = {
        "bias": 1.0,
        "word": _word(token),
        "shape": full if len(full) <= 14 else short,
        "short_shape": short,
        "len": min(len(token), 20),
        "digits": min(digits, 15),
        "is_digit": token.isdigit(),
        "has_digit": digits > 0,
        "has_alpha": any(c.isalpha() for c in token),
        "has_hyphen": "-" in token,
        "prefix": re.match(r"[A-Za-z]*", token).group(0).upper()[:4],
        "first2": token[:2] if digits else "",
        "in_subject": in_subject,
        "line_start": line_start,
        "after_sign_off": after_sign_off,
    }
    for offset in (-2, -1, 1, 2):
        other = index + offset
        if 0 <= other < len(items):
            features[f"word{offset:+d}"] = _word(items[other][0])
        else:
            features[f"word{offset:+d}"] = "<BOS>" if other < 0 else "<EOS>"
    return features


def sequence(subject: str, body: str) -> tuple[list, list[dict]]:
    items = tokens(subject, body)
    return items, [_features(items, index) for index in range(len(items))]


def new_model():
    import sklearn_crfsuite

    return sklearn_crfsuite.CRF(
        algorithm="lbfgs", c1=0.1, c2=0.1, max_iterations=200, all_possible_transitions=True
    )


def tagged(model, subject: str, body: str) -> list[tuple[str, str, float]]:
    """(token, CONNOTE or REFERENCE, probability it is a number) for tokens with a digit."""
    items, features = sequence(subject, body)
    output = []
    for (token, *_), marginal in zip(items, model.predict_marginals_single(features)):
        if not any(c.isdigit() for c in token):
            continue
        label = "CONNOTE" if marginal.get("CONNOTE", 0) >= marginal.get("REFERENCE", 0) else "REFERENCE"
        output.append((token.upper(), label, 1.0 - marginal.get("O", 1.0)))
    return output


def extract(model, subject: str, body: str, threshold: float) -> list[str]:
    """Numbers the CRF is at least `threshold` sure of, in order, unique."""
    return list(dict.fromkeys(
        token for token, _, probability in tagged(model, subject, body) if probability >= threshold
    ))


# --- Training -----------------------------------------------------------------


def _split(value) -> set[str]:
    if value is None or pd.isna(value):
        return set()
    return {part.strip().upper() for part in re.split(r"[,;\n]+", str(value)) if part.strip()}


def _text(value) -> str:
    return "" if value is None or pd.isna(value) else str(value)


def labeled_emails(training_path: str | Path) -> list[dict]:
    """POD_REQUEST emails with at least one manual connote or reference."""
    data = pd.read_excel(training_path, dtype=str)
    required = {"subject", "body", "intent_label", "connotes_manual", "reference_number_manual"}
    missing = required.difference(data.columns)
    if missing:
        raise ValueError(f"Training file is missing required columns: {', '.join(sorted(missing))}")
    emails = []
    for _, row in data[data["intent_label"].fillna("").str.strip().eq("POD_REQUEST")].iterrows():
        connotes, references = _split(row["connotes_manual"]), _split(row["reference_number_manual"])
        if connotes or references:
            emails.append({
                "subject": _text(row["subject"]),
                "body": _text(row["body"]),
                "connotes": connotes,
                "references": references,
            })
    return emails


def _truth(email: dict, token: str) -> str:
    token = token.upper()
    if token in email["connotes"]:
        return "CONNOTE"
    return "REFERENCE" if token in email["references"] else "OTHER"


def _fit(emails: list[dict]):
    sequences = [sequence(email["subject"], email["body"]) for email in emails]
    labels = [
        [_truth(email, token).replace("OTHER", "O") for token, *_ in items]
        for email, (items, _) in zip(emails, sequences)
    ]
    model = new_model()
    model.fit([features for _, features in sequences], labels)
    return model


def _scored(model, email: dict, threshold: float) -> list[tuple[str, str, str, float]]:
    """(token, true label, predicted label, confidence) for each token holding a digit."""
    return [
        (
            token,
            _truth(email, token),
            label if probability >= threshold else "OTHER",
            max(probability, 1.0 - probability),
        )
        for token, label, probability in tagged(model, email["subject"], email["body"])
    ]


def _email_scores(found: list[set], truths: list[set]) -> dict:
    tp = sum(len(f & t) for f, t in zip(found, truths))
    fp = sum(len(f - t) for f, t in zip(found, truths))
    fn = sum(len(t - f) for f, t in zip(found, truths))
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    return {
        "emails": len(truths),
        "exact": sum(f == t for f, t in zip(found, truths)),
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision and recall else None,
        "no_number": sum(not f for f in found),
    }


def train_and_compare(training_path: str | Path) -> Path:
    """Cross-validate the CRF against regex by email, save the CRF fit on all emails.

    Folds split by email (D-022). Scores are kept per email (exact, precision,
    recall, requests left without a number), which is how the two are
    compared, and per token holding a digit (the dashboard's tables and
    confusion matrix). No model is activated; the product owner chooses `crf` or
    `regex`. Returns the history path.
    """
    from . import number_classifier

    emails = labeled_emails(training_path)
    if len(emails) < 2:
        raise ValueError("Need at least 2 POD_REQUEST emails with labeled numbers")
    folds = min(model_registry.FOLDS, len(emails))
    threshold = config.NUMBER_CONFIDENCE_THRESHOLD
    print(f"Cross-validating the CRF on {len(emails)} emails, {folds} folds by email...", flush=True)

    truths = [email["connotes"] | email["references"] for email in emails]
    found: dict[str, list[set]] = {NAME: [set() for _ in emails], "regex": [set() for _ in emails]}
    rows: list[tuple[str, str, float, str]] = []  # true, CRF predicted, CRF confidence, regex predicted
    for fold, (train, test) in enumerate(KFold(folds, shuffle=True, random_state=42).split(emails), start=1):
        model = _fit([emails[i] for i in train])
        for i in test:
            email = emails[i]
            found[NAME][i] = set(number_classifier.extract_numbers(model, email["subject"], email["body"], threshold))
            found["regex"][i] = set(number_classifier.extract_numbers(None, email["subject"], email["body"]))
            for token, truth, predicted, confidence in _scored(model, email, threshold):
                rows.append((truth, predicted, confidence, "CONNOTE" if token in found["regex"][i] else "OTHER"))
        print(f"Progress: {fold}/{folds} folds", flush=True)

    final = _fit(emails)
    final_rows = [row for email in emails for row in _scored(final, email, threshold)]
    true_labels = [row[0] for row in rows]
    regex_labels = [row[3] for row in rows]
    results = {
        NAME: {
            "train": model_registry.evaluate(LABELS, [row[1] for row in final_rows], [row[2] for row in final_rows]),
            "test": model_registry.evaluate(LABELS, true_labels, [row[1] for row in rows]),
            "predictions": [row[1] for row in rows],
            "confidences": [float(row[2]) for row in rows],
        },
        # Regex has nothing to train; its scores are the yardstick.
        "regex": {
            "train": model_registry.evaluate(LABELS, true_labels, regex_labels),
            "test": model_registry.evaluate(LABELS, true_labels, regex_labels),
            "predictions": regex_labels,
            "confidences": [1.0] * len(rows),
        },
    }
    destination = model_registry.model_path("number", NAME)
    destination.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(final, destination)
    per_email = {name: _email_scores(found[name], truths) for name in (NAME, "regex")}
    # The final model on the emails it learned from: compared with the
    # cross-validated scores, a big gap means overfitting, both low underfitting.
    train_found = [
        set(number_classifier.extract_numbers(final, email["subject"], email["body"], threshold))
        for email in emails
    ]
    per_email_train = {NAME: _email_scores(train_found, truths)}
    history_path = model_registry.save_history(
        "number",
        {
            "training_file": str(training_path),
            "evaluation": f"{folds}-fold cross-validation by email",
            "n_emails": len(emails),
            "n_train": len(rows),
            "n_test": len(rows),
            "labels": list(LABELS),
            "test_labels": true_labels,
            "models": results,
            "emails": per_email,
            "emails_train": per_email_train,
        },
    )

    print(f"\nNumbers found per email ({len(emails)} emails)")
    print("train = the saved CRF on the emails it learned from; "
          f"test = {folds}-fold cross-validation by email (emails the model never saw)")
    print(f"{'Method':<8} {'Set':<6} {'Exact':>7} {'Precision':>10} {'Recall':>8} {'F1':>7} {'No number':>10}")
    for name, split, scores in (
        (NAME, "train", per_email_train[NAME]),
        (NAME, "test", per_email[NAME]),
        ("regex", "-", per_email["regex"]),
    ):
        print(
            f"{name:<8} {split:<6} {scores['exact']:>7} {scores['precision'] or 0:>10.3f} "
            f"{scores['recall'] or 0:>8.3f} {scores['f1'] or 0:>7.3f} {scores['no_number']:>10}"
        )
    train_f1, test_f1 = results[NAME]["train"]["macro_f1"], results[NAME]["test"]["macro_f1"]
    print(f"\nCRF macro F1 per token: train {train_f1:.3f}, test {test_f1:.3f}, gap {train_f1 - test_f1:.3f}")
    print(
        "Reading it: train far above test means the CRF memorises the training emails (overfitting); "
        "both low means it has not learned enough (underfitting). What counts is test against regex."
    )
    print(f"History saved: {history_path}")
    print("No model was activated. Choose one with: python -m app.model_registry --task number --activate crf")
    return history_path


PSEUDO_COLUMNS = {
    "CONNOTE": ("connotes_pseudo", "connotes_pseudo_confidence"),
    "REFERENCE": ("reference_number_pseudo", "reference_pseudo_confidence"),
}


def generate_pseudo_labels(training_path: str | Path, threshold: float) -> dict[str, int]:
    """With the active CRF, suggest numbers for rows without a manual intent."""
    source = Path(training_path)
    data = pd.read_excel(source)
    if "intent_label" not in data.columns:
        raise ValueError("Training file is missing required column: intent_label")
    model = model_registry.load_active("number")
    if model is None:
        raise FileNotFoundError(
            "No CRF number model is active. Choose one with: "
            "python -m app.model_registry --task number --activate crf"
        )
    for columns in PSEUDO_COLUMNS.values():
        for column in columns:
            data[column] = pd.Series([None] * len(data), index=data.index, dtype="object")
    generated = {"CONNOTE": 0, "REFERENCE": 0}
    total = len(data)
    for position, (index, row) in enumerate(data.iterrows(), start=1):
        if _text(row.get("intent_label")).strip():
            continue
        found = [
            item for item in tagged(model, _text(row.get("subject")), _text(row.get("body")))
            if item[2] >= threshold
        ]
        for label, (numbers_column, confidence_column) in PSEUDO_COLUMNS.items():
            chosen = [(token, probability) for token, kind, probability in found if kind == label]
            data.at[index, numbers_column] = ";".join(dict.fromkeys(token for token, _ in chosen))
            data.at[index, confidence_column] = ";".join(f"{token}:{probability:.3f}" for token, probability in chosen)
            generated[label] += len(chosen)
        if position % max(1, total // 20) == 0 or position == total:
            print(f"Progress: {position}/{total}", flush=True)
    data.to_excel(source, index=False)
    print(f"Number pseudo-labels: {generated['CONNOTE']} connotes, {generated['REFERENCE']} references.", flush=True)
    return {"connotes": generated["CONNOTE"], "references": generated["REFERENCE"]}
