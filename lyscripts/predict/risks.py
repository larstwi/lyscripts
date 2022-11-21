"""
Predict risks of involvements using the samples that were drawn during the inference
process and scenarios as defined in a YAML file.

The structure of these scenarios can is similar to how scenarios are defined for the
`lyscripts.predict.prevalences` script. Examples can be seen in an actual `params.yaml`
file over in the [`lynference`] repository.

[`lynference`]: https://github.com/rmnldwg/lynference
"""
import argparse
from pathlib import Path
from typing import Dict, Generator, List, Optional

import h5py
import lymph
import numpy as np
from rich.progress import track

from lyscripts.predict.utils import clean_pattern
from lyscripts.utils import (
    LymphModel,
    create_model_from_config,
    get_lnls,
    load_hdf5_samples,
    load_yaml_params,
    report,
)


def _add_parser(
    subparsers: argparse._SubParsersAction,
    help_formatter,
):
    """
    Add an `ArgumentParser` to the subparsers action.
    """
    parser = subparsers.add_parser(
        Path(__file__).name.replace(".py", ""),
        description=__doc__,
        help=__doc__,
        formatter_class=help_formatter,
    )
    _add_arguments(parser)

def _add_arguments(parser: argparse.ArgumentParser):
    """
    Add arguments needed to run this script to a `subparsers` instance
    and run the respective main function when chosen.
    """
    parser.add_argument(
        "model", type=Path,
        help="Path to drawn samples (HDF5)"
    )
    parser.add_argument(
        "output", default="./models/risks.hdf5", type=Path,
        help="Output path for predicted risks (HDF5 file)"
    )
    parser.add_argument(
        "--thin", default=1, type=int,
        help="Take only every n-th sample"
    )
    parser.add_argument(
        "--params", default="./params.yaml", type=Path,
        help="Path to parameter file"
    )

    parser.set_defaults(run_main=main)


def predicted_risk(
    involvement: Dict[str, Dict[str, bool]],
    model: LymphModel,
    samples: np.ndarray,
    t_stage: str,
    midline_ext: bool = False,
    given_diagnosis: Optional[Dict[str, Dict[str, bool]]] = None,
    given_diagnosis_spsn: Optional[List[float]] = None,
    invert: bool = False,
    **_kwargs,
) -> Generator[float, None, None]:
    """Compute the probability of arriving in a particular `involvement` in a given
    `t_stage` using a `model` with pretrained `samples`. This probability can be
    computed for a `given_diagnosis` that was obtained using a modality with
    specificity & sensitivity provided via `given_diagnosis_spsn`. If the model is an
    instance of `lymph.MidlineBilateral`, one can specify whether or not the primary
    tumor has a `midline_ext`.

    Both the `involvement` and the `given_diagnosis` should be dictionaries like this:

    ```python
    involvement = {
        "ipsi":  {"I": False, "II": True , "III": None , "IV": None},
        "contra: {"I": None , "II": False, "III": False, "IV": None},
    }
    ```

    The returned probability can be `invert`ed.

    Set `verbose` to `True` for a visualization of the progress.
    """
    lnls = get_lnls(model)
    involvement = clean_pattern(involvement, lnls)
    given_diagnosis = clean_pattern(given_diagnosis, lnls)

    if given_diagnosis_spsn is not None:
        model.modalities = {"risk": given_diagnosis_spsn}
    else:
        model.modalities = {"risk": [1., 1.]}

    if isinstance(model, lymph.Unilateral):
        given_diagnosis = {"risk": given_diagnosis["ipsi"]}

        for sample in samples:
            risk = model.risk(
                involvement=involvement["ipsi"],
                given_params=sample,
                given_diagnoses=given_diagnosis,
                t_stage=t_stage
            )
            yield 1. - risk if invert else risk

    elif isinstance(model, (lymph.Bilateral, lymph.MidlineBilateral)):
        given_diagnosis = {"risk": given_diagnosis}

        for sample in samples:
            risk = model.risk(
                involvement=involvement,
                given_params=sample,
                given_diagnoses=given_diagnosis,
                t_stage=t_stage,
                midline_extension=midline_ext,
            )
            yield (1. - risk) if invert else risk

    else:
        raise TypeError("Provided model is no valid `lymph` model.")


def main(args: argparse.Namespace):
    """
    The call signature to this script looks is shown below and can be generated by
    typng `lyscripts predict risks --help`.

    ```
    USAGE: lyscripts predict risks [-h] [--thin THIN] [--params PARAMS] model output

    Predict risks of involvements using the samples that were drawn during the
    inference process and scenarios as defined in a YAML file. The structure of these
    scenarios can be seen in an actual `params.yaml` file over in the
    (https://github.com/rmnldwg/lynference) repository.

    POSITIONAL ARGUMENTS:
    model            Path to drawn samples (HDF5)
    output           Output path for predicted risks (HDF5 file)

    OPTIONAL ARGUMENTS:
    -h, --help       show this help message and exit
    --thin THIN      Take only every n-th sample (default: 1)
    --params PARAMS  Path to parameter file (default: ./params.yaml)
    ```
    """
    params = load_yaml_params(args.params)
    model = create_model_from_config(params)
    samples = load_hdf5_samples(args.model)

    args.output.parent.mkdir(exist_ok=True)
    num_risks = len(params["risks"])
    with h5py.File(args.output, mode="w") as risks_storage:
        for i,scenario in enumerate(params["risks"]):
            risks_gen = predicted_risk(
                model=model,
                samples=samples[::args.thin],
                **scenario
            )
            risks_progress = track(
                risks_gen,
                total=len(samples[::args.thin]),
                description=f"Compute risks for scenario {i+1}/{num_risks}...",
                console=report,
                transient=True,
            )
            risks_arr = np.array(list(r for r in risks_progress))
            risks_h5dset = risks_storage.create_dataset(
                name=scenario["name"],
                data=risks_arr,
            )
            for key,val in scenario.items():
                try:
                    risks_h5dset.attrs[key] = val
                except TypeError:
                    pass
        report.success(
            f"Computed risks of {num_risks} scenarios stored at {args.output}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    _add_arguments(parser)

    args = parser.parse_args()
    args.run_main(args)
