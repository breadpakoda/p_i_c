"""Score fusion for the prompt-injection detectors."""

import math

LAYER2_TEMPERATURE = 0.05
LAYER2_EVIDENCE_THRESHOLD = 0.35
LAYER1_ALERT_THRESHOLD = 0.8
COMBINED_ALERT_THRESHOLD = LAYER1_ALERT_THRESHOLD


def combine_detector_scores(
    layer1_attack_score: float,
    layer2_similarity_score: float,
) -> dict[str, float | bool]:
    """Fuse classifier probability and cosine similarity with a noisy-OR score.

    Layer 1 already returns an attack probability. Layer 2's cosine similarity
    is converted to soft evidence with a sigmoid centered on its configured
    threshold. A conservative combined threshold reduces false positives from
    a moderately confident classifier result while retaining strong evidence
    from either detector.
    """
    if not math.isfinite(layer1_attack_score) or not 0.0 <= layer1_attack_score <= 1.0:
        raise ValueError("Layer 1 attack score must be finite and between 0 and 1.")
    if not math.isfinite(layer2_similarity_score) or not -1.0 <= layer2_similarity_score <= 1.0:
        raise ValueError("Layer 2 similarity score must be finite and between -1 and 1.")

    scaled_similarity = (
        layer2_similarity_score - LAYER2_EVIDENCE_THRESHOLD
    ) / LAYER2_TEMPERATURE
    layer2_evidence = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, scaled_similarity))))
    combined_score = 1.0 - (
        (1.0 - layer1_attack_score) * (1.0 - layer2_evidence)
    )

    return {
        "layer1_score": layer1_attack_score,
        "layer2_score": layer2_similarity_score,
        "layer2_evidence": layer2_evidence,
        "combined_score": combined_score,
        "detected": combined_score >= COMBINED_ALERT_THRESHOLD,
    }
