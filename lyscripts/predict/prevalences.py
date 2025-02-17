"""
Predict prevalences of diagnostic patterns using the samples that were inferred using
the model via MCMC sampling and compare them to the prevalence in the data.

This essentially amounts to computing the data likelihood under the model and comparing
it to the empirical likelihood of a given pattern of lymphatic progression.

Like `lyscripts.predict.risks`, the computation of the prevalences can be done for
different scenarios. How to define these scenarios can be seen in the
[`lynference`](https://github.com/rmnldwg/lynference) repository.
"""
import argparse
import logging
from pathlib import Path
from typing import Dict, Generator, List, Optional

import h5py
import lymph
import numpy as np
import pandas as pd
from rich.progress import track

from lyscripts.decorators import log_state
from lyscripts.predict.utils import complete_pattern
from lyscripts.utils import (
    LymphModel,
    create_model_from_config,
    flatten,
    get_lnls,
    load_data_for_model,
    load_hdf5_samples,
    load_yaml_params,
    report,
)

logger = logging.getLogger(__name__)


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
        "data", type=Path,
        help="Path to the data file to compare prediction and data prevalence"
    )
    parser.add_argument(
        "output", type=Path,
        help="Output path for predicted prevalences (HDF5 file)"
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


def get_match_idx(
    match_idx,
    pattern: Dict[str, Optional[bool]],
    data: pd.DataFrame,
    lnls: List[str],
    invert: bool = False,
) -> pd.Series:
    """Get the indices of the rows in the `data` where the diagnose matches the
    `pattern` of interest for every lymph node level in the `lnls`. An example:
    >>> pattern = {"II": True, "III": None}
    >>> lnls = ["II", "III"]
    >>> data = pd.DataFrame.from_dict({
    ...     "II":  [True, False],
    ...     "III": [False, False],
    ... })
    >>> get_match_idx(True, pattern, data, lnls)
    0     True
    1    False
    Name: II, dtype: bool
    """
    for lnl in lnls:
        if lnl not in pattern or pattern[lnl] is None:
            continue
        if invert:
            match_idx |= data[lnl] != pattern[lnl]
        else:
            match_idx &= data[lnl] == pattern[lnl]

    return match_idx


def does_t_stage_match(data: pd.DataFrame, t_stage: str) -> pd.Index:
    """Return the indices of the `data` where the `t_stage` of the patients matches."""
    if data.columns.nlevels == 3:
        if t_stage=="early/late":
            return data[("info","tumor", "t_stage")].isin(["early", "late"])
        else:
            return data["info", "tumor", "t_stage"] == t_stage
    
    elif data.columns.nlevels == 2:
        if t_stage=="early/late":
            return data[("info", "t_stage")].isin(["early", "late"])
        else:
            return data["info", "t_stage"] == t_stage
    else:
        raise ValueError("Data has neither 2 nor 3 header rows")


def does_midline_ext_match(
    data: pd.DataFrame,
    midline_ext: Optional[bool] = None
) -> pd.Index:
    """
    Return the indices of the `data` where the `midline_ext` of the patients matches.
    """
    if midline_ext is None or data.columns.nlevels == 2:
        return True

    try:
        return data["info", "tumor", "midline_extension"] == midline_ext
    except KeyError as key_err:
        raise KeyError(
            "Data does not seem to have midline extension information"
        ) from key_err


def get_midline_ext_prob(data: pd.DataFrame, t_stage: str) -> float:
    """Get the prevalence of midline extension from `data` for `t_stage`."""
    if data.columns.nlevels == 2:
        return None

    has_matching_t_stage = does_t_stage_match(data, t_stage)
    eligible_data = data[has_matching_t_stage]
    has_matching_midline_ext = does_midline_ext_match(eligible_data, midline_ext=True)
    matching_data = eligible_data[has_matching_midline_ext]
    return len(matching_data) / len(eligible_data)

def calculate_midline_ext_prob(diag_prob, midline_ext_prob_rates):
            num_timesteps = len(diag_prob)
            cumulative_probability = 0.0

            for diagnosis_timestep in range(num_timesteps):
                cumulative_probability_at_diagnosis = 1.0

                for t in range(diagnosis_timestep):
                    cumulative_probability_at_diagnosis *= (1 - midline_ext_prob_rates[t])

                cumulative_probability_at_diagnosis *= diag_prob[diagnosis_timestep]
                cumulative_probability += cumulative_probability_at_diagnosis

            return 1 - cumulative_probability

def get_early_prob(data: pd.DataFrame) -> float:
    """Get the prevalence of midline extension from `data` for `t_stage`."""

    has_matching_t_stage = does_t_stage_match(data, "early")
    matching_data = data[has_matching_t_stage]
    return len(matching_data) / len(data)


def create_patient_row(
    pattern: Dict[str, Dict[str, bool]],
    t_stage: str,
    midline_ext: Optional[bool] = None,
    make_unilateral: bool = False,
) -> pd.DataFrame:
    """
    Create a pandas `DataFrame` representing a single patient from the specified
    involvement `pattern`, along with their `t_stage` and `midline_ext` (if provided).
    If `midline_ext` is not provided, the function creates two patient rows. One of a
    patient _with_ and one of a patient _without_ a midline extention. And the returned
    `patient_row` will only contain the `ipsi` part of the pattern when one tells the
    function to `make_unilateral`.
    """
    if make_unilateral:
        flat_pattern = flatten({"prev": pattern["ipsi"]})
        patient_row = pd.DataFrame(flat_pattern, index=[0])
        if t_stage != "early/late":
            patient_row["info", "t_stage"] = t_stage
            return patient_row
        else:
            early_tstage = patient_row.copy()
            early_tstage["info", "t_stage"] = "early"
            late_tstage = patient_row.copy()
            late_tstage["info", "t_stage"] = "late"

            return pd.concat([early_tstage, late_tstage], ignore_index=True)

    elif t_stage != "early/late":
        flat_pattern = flatten({"prev": pattern})
        patient_row = pd.DataFrame(flat_pattern, index=[0])
        patient_row["info", "tumor", "t_stage"] = t_stage
        if midline_ext is not None:
            patient_row["info", "tumor", "midline_extension"] = midline_ext
            return patient_row

        with_midline_ext = patient_row.copy()
        with_midline_ext["info", "tumor", "midline_extension"] = True
        without_midline_ext = patient_row.copy()
        without_midline_ext["info", "tumor", "midline_extension"] = False

        return pd.concat([with_midline_ext, without_midline_ext], ignore_index=True)
    
    else:
        flat_pattern = flatten({"prev": pattern})
        patient_row = pd.DataFrame(flat_pattern, index=[0])
        early_tstage = patient_row.copy()
        early_tstage["info", "tumor", "t_stage"] = "early"
        late_tstage = patient_row.copy()
        late_tstage["info", "tumor", "t_stage"] = "late"
        if midline_ext is not None:
            early_tstage["info", "tumor", "midline_extension"] = midline_ext
            late_tstage["info", "tumor", "midline_extension"] = midline_ext
            return pd.concat([early_tstage, late_tstage], ignore_index=True)

        early_with_midline_ext = early_tstage.copy()
        early_with_midline_ext["info", "tumor", "midline_extension"] = True
        early_without_midline_ext = early_tstage.copy()
        early_without_midline_ext["info", "tumor", "midline_extension"] = False
        late_with_midline_ext = late_tstage.copy()
        late_with_midline_ext["info", "tumor", "midline_extension"] = True
        late_without_midline_ext = late_tstage.copy()
        late_without_midline_ext["info", "tumor", "midline_extension"] = False

        return pd.concat([early_with_midline_ext, late_with_midline_ext, early_without_midline_ext, late_without_midline_ext], ignore_index=True)

@log_state(logger=logger)
def compute_observed_prevalence(
    pattern: Dict[str, Dict[str, bool]],
    data: pd.DataFrame,
    lnls: List[str],
    t_stage: str = "early",
    modality: str = "max_llh",
    midline_ext: Optional[bool] = None,
    invert: bool = False,
    **_kwargs,
):
    """Extract the prevalence of a lymphatic `pattern` of progression for a given
    `t_stage` from the `data` as reported by the given `modality`.

    If the `data` contains bilateral information, one can choose to factor in whether
    or not the patient's `midline_ext` should be considered as well.

    By giving a list of `lnls`, one can restrict the matching algorithm to only those
    lymph node levels that are provided via this list.

    When `invert` is set to `True`, the function returns 1 minus the prevalence.
    """
    pattern = complete_pattern(pattern, lnls)

    has_matching_t_stage = does_t_stage_match(data, t_stage)
    has_matching_midline_ext = does_midline_ext_match(data, midline_ext)

    eligible_data = data.loc[has_matching_t_stage & has_matching_midline_ext, modality]
    eligible_data = eligible_data.dropna(axis="index", how="all")

    # filter the data by the LNL pattern they report
    do_lnls_match = not invert
    if data.columns.nlevels == 2:
        do_lnls_match = get_match_idx(
            do_lnls_match,
            pattern["ipsi"],
            eligible_data,
            lnls=lnls,
            invert=invert,
        )
    else:
        for side in ["ipsi", "contra"]:
            do_lnls_match = get_match_idx(
                do_lnls_match,
                pattern[side],
                eligible_data[side],
                lnls=lnls,
                invert=invert
            )

    try:
        matching_data = eligible_data.loc[do_lnls_match]
    except KeyError:
        # return X, X if no actual pattern was selected
        len_matching_data = 0 if invert else len(eligible_data)
        return len_matching_data, len(eligible_data)

    return len(matching_data), len(eligible_data)


def compute_predicted_prevalence(
    loaded_model: LymphModel,
    given_params: np.ndarray,
    midline_ext: bool,
    t_stage: str,
    midline_ext_prob: float = 0.3,
    early_prob: float = 0.5
) -> float:
    """
    Given a `loaded_model` with loaded patient data and modalities, compute the
    prevalence of the loaded data for a sample of `given_params`.

    If `midline_ext` is `True`, the prevalence is computed for the case where the
    tumor does extend over the mid-sagittal line, while if it is `False`, it is
    predicted for the case of a lateralized tumor.

    If `midline_ext` is set to `None`, the prevalence is marginalized over both cases,
    assuming the provided `midline_ext_prob`.
    """
    if isinstance(loaded_model, lymph.MidlineBilateral):
        loaded_model.check_and_assign(given_params)
        if midline_ext is None:
                # marginalize over patients with and without midline extension
                #only correct with new code of time evolution over midline extension
            if t_stage=="early/late":
                early_llhs = loaded_model.likelihood(log=False, t_stages=["early"], given_params=given_params, prevalence_calc=True)
                late_llhs = loaded_model.likelihood(log=False, t_stages=["late"], given_params=given_params, prevalence_calc=True)
                prevalence = (
                    early_prob * early_llhs[0] +
                    early_prob * early_llhs[1] +
                    (1 - early_prob) * late_llhs[0] +
                    (1 - early_prob) * late_llhs[1]
                )
            else:
                llhs = loaded_model.likelihood(log=False, given_params=given_params, prevalence_calc=True)
                prevalence = llhs[0] + llhs[1]

        elif midline_ext:
            if t_stage=="early/late":
                midline_ext_prob_early = calculate_midline_ext_prob(
                    loaded_model.ext.ipsi.diag_time_dists['early'].pmf,
                    ([given_params[-2]] * len(loaded_model.ext.ipsi.diag_time_dists['early'].pmf))
                )
                midline_ext_prob_late = calculate_midline_ext_prob(
                    loaded_model.ext.ipsi.diag_time_dists['late'].pmf,
                    ([given_params[-2]] * len(loaded_model.ext.ipsi.diag_time_dists['late'].pmf))
                )
                prevalence = (
                    early_prob * loaded_model.likelihood(
                        log=False,
                        given_params=given_params,
                        t_stages=["early"],
                        prevalence_calc=True
                    )/midline_ext_prob_early +
                    (1 - early_prob) * loaded_model.likelihood(
                        log=False,
                        given_params=given_params,
                        t_stages=["late"],
                        prevalence_calc=True
                    )/midline_ext_prob_late
                )
            else:
                midline_ext_prob = calculate_midline_ext_prob(
                    loaded_model.ext.ipsi.diag_time_dists[t_stage].pmf,
                    ([given_params[-2]] * len(loaded_model.ext.ipsi.diag_time_dists[t_stage].pmf))
                )
                prevalence = loaded_model.likelihood(log=False, given_params=given_params, prevalence_calc=True)/midline_ext_prob
        else:
            if t_stage=="early/late":
                midline_ext_prob_early = calculate_midline_ext_prob(
                    loaded_model.ext.ipsi.diag_time_dists['early'].pmf,
                    ([given_params[-2]] * len(loaded_model.ext.ipsi.diag_time_dists['early'].pmf))
                )
                midline_ext_prob_late = calculate_midline_ext_prob(
                    loaded_model.ext.ipsi.diag_time_dists['late'].pmf,
                    ([given_params[-2]] * len(loaded_model.ext.ipsi.diag_time_dists['late'].pmf))
                )
                prevalence = (
                    early_prob * loaded_model.likelihood(
                        log=False,
                        given_params=given_params,
                        t_stages=["early"],
                        prevalence_calc=True
                    )/(1-midline_ext_prob_early) +
                    (1 - early_prob) * loaded_model.likelihood(
                        log=False,
                        given_params=given_params,
                        t_stages=["late"],
                        prevalence_calc=True
                    )/(1-midline_ext_prob_late)
                )
            else:
                midline_ext_prob = calculate_midline_ext_prob(
                    loaded_model.ext.ipsi.diag_time_dists[t_stage].pmf,
                    ([given_params[-2]] * len(loaded_model.ext.ipsi.diag_time_dists[t_stage].pmf))
                )
                prevalence = loaded_model.likelihood(log=False, given_params=given_params, prevalence_calc=True)/(1-midline_ext_prob)
    else:
        if t_stage=="early/late":
            prevalence = early_prob * loaded_model.likelihood(
                given_params=given_params,
                log=False, t_stages=["early"]
            ) + (1-early_prob) * loaded_model.likelihood(
                given_params=given_params,
                log=False, t_stages=["late"]
            )
        else:
            prevalence = loaded_model.likelihood(
                given_params=given_params,
                log=False
            )
    return prevalence


@log_state(logger=logger)
def generate_predicted_prevalences(
    pattern: Dict[str, Dict[str, bool]],
    model: LymphModel,
    samples: np.ndarray,
    t_stage: str = "early",
    midline_ext: Optional[bool] = None,
    midline_ext_prob: float = 0.3,
    modality_spsn: Optional[List[float]] = None,
    invert: bool = False,
    early_prob: float = 0.5,
    **_kwargs,
) -> Generator[float, None, None]:
    """Compute the prevalence of a given `pattern` of lymphatic progression using a
    `model` and trained `samples`.

    Do this computation for the specified `t_stage` and whether or not the tumor has
    a `midline_ext`. `modality_spsn` defines the values for specificity & sensitivity
    of the diagnostic modality for which the prevalence is to be computed. Default is
    a value of 1 for both.

    Use `invert` to compute 1 - p.
    """
    lnls = get_lnls(model)
    pattern = complete_pattern(pattern, lnls)

    if modality_spsn is None:
        model.modalities = {"prev": [1., 1.]}
    else:
        model.modalities = {"prev": modality_spsn}

    is_unilateral = isinstance(model, lymph.Unilateral)
    patient_row = create_patient_row(
        pattern, t_stage, midline_ext, make_unilateral=is_unilateral
    )
    model.patient_data = patient_row

    # compute prevalence as likelihood of diagnose `prev`, which was defined above
    for sample in samples:
        prevalence = compute_predicted_prevalence(
            loaded_model=model,
            given_params=sample,
            midline_ext=midline_ext,
            midline_ext_prob=midline_ext_prob,
            t_stage=t_stage,
            early_prob = early_prob,
        )
        yield (1. - prevalence) if invert else prevalence


def main(args: argparse.Namespace):
    """
    This subprogram's call signature can be obtained via `lyscripts predict
    prevalences --help` and shows this:

    ```
    USAGE: lyscripts predict prevalences [-h] [--thin THIN] [--params PARAMS]
                                         model data output

    Predict prevalences of diagnostic patterns using the samples that were inferred
    using the model via MCMC sampling and compare them to the prevalence in the data.

    This essentially amounts to computing the data likelihood under the model and
    comparing it to the empirical likelihood of a given pattern of lymphatic
    progression.

    POSITIONAL ARGUMENTS:
    model            Path to drawn samples (HDF5)
    data             Path to the data file to compare prediction and data prevalence
    output           Output path for predicted prevalences (HDF5 file)

    OPTIONAL ARGUMENTS:
    -h, --help       show this help message and exit
    --thin THIN      Take only every n-th sample (default: 1)
    --params PARAMS  Path to parameter file (default: ./params.yaml)
    ```
    """
    params = load_yaml_params(args.params, logger=logger)
    model = create_model_from_config(params, logger=logger)
    samples = load_hdf5_samples(args.model, logger=logger)

    header_rows = [0,1] if isinstance(model, lymph.Unilateral) else [0,1,2]
    data = load_data_for_model(args.data, header_rows, logger=logger)

    args.output.parent.mkdir(exist_ok=True)
    num_prevalences = len(params["prevalences"])
    with h5py.File(args.output, mode="w") as prevalences_storage:
        for i,scenario in enumerate(params["prevalences"]):
            prevs_gen = generate_predicted_prevalences(
                model=model,
                samples=samples[::args.thin],
                midline_ext_prob=get_midline_ext_prob(data, scenario["t_stage"]),
                early_prob=get_early_prob(data),
                **scenario
            )
            prevs_progress = track(
                prevs_gen,
                total=len(samples[::args.thin]),
                description=f"Compute prevalences for scenario {i+1}/{num_prevalences}...",
                console=report,
                transient=True,
            )
            prevs_arr = np.array(list(p for p in prevs_progress))
            prevs_h5dset = prevalences_storage.create_dataset(
                name=scenario["name"],
                data=prevs_arr,
            )
            num_match, num_total = compute_observed_prevalence(
                data=data,
                lnls=get_lnls(model),
                **scenario,
            )
            for key,val in scenario.items():
                try:
                    prevs_h5dset.attrs[key] = val
                except TypeError:
                    pass

            prevs_h5dset.attrs["num_match"] = num_match
            prevs_h5dset.attrs["num_total"] = num_total

        logger.info(
            f"Computed prevalences of {num_prevalences} scenarios stored at "
            f"{args.output}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    _add_arguments(parser)

    args = parser.parse_args()
    args.run_main(args)
