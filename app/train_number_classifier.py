"""Train the CRF number model on the labeled emails and compare it with regex."""

from __future__ import annotations

import argparse

from . import config, number_crf


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default=str(config.training_data_path()))
    parser.add_argument(
        "--pseudo-label",
        action="store_true",
        help="Write the active CRF's numbers for rows without a manual intent",
    )
    parser.add_argument("--threshold", type=float, default=config.NUMBER_CONFIDENCE_THRESHOLD)
    args = parser.parse_args()
    if args.pseudo_label:
        number_crf.generate_pseudo_labels(args.data, args.threshold)
    else:
        number_crf.train_and_compare(args.data)


if __name__ == "__main__":
    main()
