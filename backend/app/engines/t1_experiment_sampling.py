from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional


CALIBRATION_SAMPLER_VERSION = "t1_execution_calibration_v1"
SAMPLING_FRAME_VERSION = "t1_15m_eligible_frame_v1"
NET_EXPECTATION_MODEL_VERSION = "t1_conservative_net_expectation_v1"
DEFAULT_DIAGNOSTIC_BUDGET = 10
MAX_SAMPLES_PER_STRATUM = 2
MIN_BIN_SAMPLES = 30
MIN_TOTAL_SAMPLES = 150
SHRINKAGE_PRIOR_SAMPLES = 30


def prepare_execution_calibration(
    candidates: Iterable[Dict[str, Any]],
    certainty_roles: Dict[str, List[str]],
    *,
    run_id: int,
    recommendation_date: str,
    strategy_policy_version: str,
    base_market_state: str,
    budget: int = DEFAULT_DIAGNOSTIC_BUDGET,
) -> Dict[str, Any]:
    """Freeze a reproducible probability sample without reading outcome fields."""
    unique = _unique_candidates(candidates)
    seed = f"{int(run_id)}:{recommendation_date}:{CALIBRATION_SAMPLER_VERSION}"
    budget = max(0, int(budget or 0))
    certainty_codes = [code for code in sorted(certainty_roles) if code in unique]
    certainty_codes = certainty_codes[:budget]
    frame = [item for code, item in unique.items() if code not in set(certainty_codes)]
    strata: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for item in frame:
        strata[_stratum(item, base_market_state)].append(item)

    remaining = max(0, budget - len(certainty_codes))
    stratum_order = sorted(
        strata,
        key=lambda value: (_digest(f"{recommendation_date}:{strategy_policy_version}:{value}"), value),
    )
    quotas = {key: 0 for key in stratum_order}
    while remaining > 0:
        changed = False
        for key in stratum_order:
            capacity = min(MAX_SAMPLES_PER_STRATUM, len(strata[key]))
            if quotas[key] >= capacity:
                continue
            quotas[key] += 1
            remaining -= 1
            changed = True
            if remaining <= 0:
                break
        if not changed:
            break

    selected_by_stratum: Dict[str, set[str]] = {}
    for key, rows in strata.items():
        ordered = sorted(rows, key=lambda item: (_digest(f"{seed}:{_code(item)}"), _code(item)))
        selected_by_stratum[key] = {_code(item) for item in ordered[: quotas.get(key, 0)]}

    updates: List[Dict[str, Any]] = []
    selected: List[Dict[str, Any]] = []
    certainty_set = set(certainty_codes)
    for code, raw in unique.items():
        item = dict(raw)
        stratum = _stratum(item, base_market_state)
        certainty = code in certainty_set
        population = len(strata.get(stratum, []))
        sample_count = quotas.get(stratum, 0)
        probability = 1.0 if certainty else (sample_count / population if population else 0.0)
        sample_selected = certainty or code in selected_by_stratum.get(stratum, set())
        roles = list(certainty_roles.get(code) or [])
        if certainty:
            roles = _unique_strings([*roles, "policy_certainty"])
        if sample_selected and not certainty:
            roles = _unique_strings([*roles, "execution_calibration"])
        metadata = {
            "code": code,
            "sampleRole": roles,
            "selectionMechanism": "policy_certainty" if certainty else "stratified_stable_hash",
            "samplingFrameVersion": SAMPLING_FRAME_VERSION,
            "calibrationSamplerVersion": CALIBRATION_SAMPLER_VERSION,
            "sampleSelected": sample_selected,
            "stratum": stratum,
            "stratumPopulationCount": 1 if certainty else population,
            "stratumSampleCount": 1 if certainty else sample_count,
            "populationInclusionProbability": round(probability, 8),
            "certaintyUnit": certainty,
            "populationEstimationEligible": sample_selected and probability > 0,
            "sampleSeed": seed,
            "samplingStatus": (
                "certainty_unit" if certainty else "selected" if sample_selected
                else "not_sampled_budget_limited" if sample_count == 0 else "not_selected"
            ),
        }
        updates.append(metadata)
        if sample_selected:
            selected_item = dict(item)
            selected_item.update(metadata)
            selected.append(selected_item)

    selected.sort(key=lambda item: (not bool(item.get("certaintyUnit")), _code(item)))
    uncovered = [key for key in stratum_order if quotas.get(key, 0) == 0]
    return {
        "version": CALIBRATION_SAMPLER_VERSION,
        "samplingFrameVersion": SAMPLING_FRAME_VERSION,
        "sampleSeed": seed,
        "budget": budget,
        "eligibleCount": len(unique),
        "certaintyCount": len(certainty_codes),
        "frameCount": len(frame),
        "selectedCount": len(selected),
        "probabilitySampleCount": len(selected) - len(certainty_codes),
        "nonEmptyStratumCount": len(strata),
        "coveredStratumCount": len(strata) - len(uncovered),
        "uncoveredStrata": uncovered,
        "coverageStatus": "complete" if not uncovered else "budget_limited",
        "candidateUpdates": updates,
        "selectedCandidates": selected,
    }


def fit_conservative_net_expectation(samples: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    rows = [dict(row) for row in samples if _number(row.get("netReturnPct")) is not None]
    returns = [float(row["netReturnPct"]) for row in rows]
    global_mean = _weighted_mean(rows)
    global_std = _stddev(returns)
    by_stratum: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_stratum[str(row.get("stratum") or "unknown")].append(row)
    bins = {}
    for key, values in by_stratum.items():
        raw_mean = _weighted_mean(values)
        count = len(values)
        shrunk = (
            (raw_mean * count + global_mean * SHRINKAGE_PRIOR_SAMPLES)
            / (count + SHRINKAGE_PRIOR_SAMPLES)
            if global_mean is not None and raw_mean is not None
            else raw_mean
        )
        local_returns = [float(row["netReturnPct"]) for row in values]
        uncertainty = (_stddev(local_returns) or global_std or 0.0) / math.sqrt(max(1, count))
        left_tail = abs(mean(sorted(local_returns)[: max(1, math.ceil(count * 0.2))])) if local_returns else 0.0
        conservative = None if shrunk is None else shrunk - uncertainty - 0.25 * left_tail
        bins[key] = {
            "sampleCount": count,
            "rawExpectedNetReturnPct": _rounded(raw_mean),
            "shrunkExpectedNetReturnPct": _rounded(shrunk),
            "uncertaintyPenaltyPct": _rounded(uncertainty),
            "leftTailPenaltyPct": _rounded(0.25 * left_tail),
            "conservativeScorePct": _rounded(conservative),
            "positiveWeightEligible": count >= MIN_BIN_SAMPLES,
        }
    ready_bins = sum(1 for value in bins.values() if value["positiveWeightEligible"])
    return {
        "version": NET_EXPECTATION_MODEL_VERSION,
        "sampleCount": len(rows),
        "independentTradeDays": len({str(row.get("recommendationDate") or "") for row in rows}),
        "globalExpectedNetReturnPct": _rounded(global_mean),
        "globalStdPct": _rounded(global_std),
        "binCount": len(bins),
        "readyBinCount": ready_bins,
        "minimumBinSamples": MIN_BIN_SAMPLES,
        "minimumTotalSamples": MIN_TOTAL_SAMPLES,
        "rankingReady": len(rows) >= MIN_TOTAL_SAMPLES and ready_bins > 0,
        "bins": bins,
    }


def score_net_expectation_candidate(
    candidate: Dict[str, Any], model: Dict[str, Any], base_market_state: str
) -> Dict[str, Any]:
    stratum = _stratum(candidate, base_market_state)
    estimate = dict((model.get("bins") or {}).get(stratum) or {})
    eligible = bool(model.get("rankingReady") and estimate.get("positiveWeightEligible"))
    return {
        "stratum": stratum,
        "modelVersion": model.get("version") or NET_EXPECTATION_MODEL_VERSION,
        "sampleCount": int(estimate.get("sampleCount") or 0),
        "expectedNetReturnPct": estimate.get("shrunkExpectedNetReturnPct"),
        "uncertaintyPenaltyPct": estimate.get("uncertaintyPenaltyPct"),
        "leftTailPenaltyPct": estimate.get("leftTailPenaltyPct"),
        "conservativeScorePct": estimate.get("conservativeScorePct") if eligible else None,
        "rankingEligible": eligible,
        "status": "ready" if eligible else "observation_only",
    }


def candidate_stratum(candidate: Dict[str, Any], base_market_state: str) -> str:
    return _stratum(candidate, base_market_state)


def _stratum(item: Dict[str, Any], base_market_state: str) -> str:
    driver = str(item.get("primaryDriver") or "unknown")
    score = _number(item.get("t1Score"))
    if score is None:
        score = _number(item.get("score"))
    risk = _number((item.get("t1Dimensions") or {}).get("overnightRisk"))
    score_bucket = "lt60" if score is None or score < 60 else "60_69" if score < 70 else "70_79" if score < 80 else "gte80"
    risk_bucket = "lt48" if risk is None or risk < 48 else "48_67" if risk < 68 else "gte68"
    return f"{base_market_state or 'unknown'}|{driver}|{score_bucket}|{risk_bucket}"


def _unique_candidates(candidates: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for raw in candidates or []:
        code = _code(raw)
        if code and code not in result:
            result[code] = dict(raw)
    return result


def _code(item: Dict[str, Any]) -> str:
    value = str(item.get("code") or "").strip()
    return value.zfill(6) if value else ""


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _weighted_mean(rows: List[Dict[str, Any]]) -> Optional[float]:
    if not rows:
        return None
    weighted = []
    for row in rows:
        probability = _number(row.get("populationInclusionProbability"))
        weight = min(20.0, 1.0 / probability) if probability and probability > 0 else 1.0
        weighted.append((float(row["netReturnPct"]), weight))
    total = sum(weight for _, weight in weighted)
    return sum(value * weight for value, weight in weighted) / total if total else None


def _stddev(values: List[float]) -> Optional[float]:
    if not values:
        return None
    center = mean(values)
    return math.sqrt(sum((value - center) ** 2 for value in values) / len(values))


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rounded(value: Optional[float]) -> Optional[float]:
    return round(float(value), 6) if value is not None else None


def _unique_strings(values: Iterable[str]) -> List[str]:
    result = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
