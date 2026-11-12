import hashlib


def canary_hit(case_id: str, percent: int) -> bool:
    """Stable per-case bucket: the same case always lands in the same bucket."""
    bucket = int(hashlib.sha256(case_id.encode()).hexdigest()[:8], 16) % 100
    return bucket < percent
