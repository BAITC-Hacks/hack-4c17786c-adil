"""Shared state and public campaign filtering, independent of the evaluator."""
from dataclasses import dataclass, field
import math

import numpy as np


FILTERS = {
    "filter_current_tariff": "current_tariff",
    "filter_arpu_segment": "arpu_segment",
    "filter_data_segment": "data_segment",
    "filter_call_segment": "call_segment",
}
VALID_SEGMENTS = {
    "filter_arpu_segment": {"LOW", "MID", "HIGH"},
    "filter_data_segment": {"NON_USER", "LITE", "HEAVY"},
    "filter_call_segment": {"LOW", "MEDIUM", "HIGH"},
}
# Public sampling-noise scale documented in the supplied environment.py.
OBSERVATION_STD = 0.804


def campaign_mask(profile, campaign):
    mask = np.ones(len(profile), dtype=bool)
    for key, column in FILTERS.items():
        value = campaign.get(key)
        if value is None:
            continue
        if key == "filter_current_tariff":
            wanted = [part.strip() for part in str(value).split(";") if part.strip()]
            mask &= profile[column].isin(wanted).to_numpy()
        else:
            mask &= profile[column].eq(value).to_numpy()
    return mask


@dataclass
class Arm:
    filters: dict
    target: str
    channel: str
    size: int
    arpu_sum: float
    prior_mean: float = 0.0
    prior_std: float = 0.25
    n: int = 0
    weighted_sum: float = 0.0
    trials: int = 0
    unavailable: bool = False

    @property
    def cell(self):
        return tuple(sorted(self.filters.items()))

    @property
    def key(self):
        return self.cell, self.target, self.channel

    @property
    def mean(self):
        prior_precision = 1.0 / self.prior_std ** 2
        precision = prior_precision + self.n / OBSERVATION_STD ** 2
        return (self.prior_mean * prior_precision + self.weighted_sum / OBSERVATION_STD ** 2) / precision

    @property
    def standard_error(self):
        return math.sqrt(1.0 / (1.0 / self.prior_std ** 2 + self.n / OBSERVATION_STD ** 2))

    def observe(self, ratio, n):
        if not math.isfinite(float(ratio)) or not math.isfinite(float(n)) or n <= 0:
            raise ValueError("Pilot returned non-finite effect or invalid sample size")
        self.n += int(n)
        self.weighted_sum += float(ratio) * int(n)
        self.trials += 1

    def campaign(self):
        return {**self.filters, "target_tariff": self.target, "channel": self.channel}


@dataclass
class State:
    profile: object
    tariffs: set
    channels: dict
    initial_budget: float
    initial_contacts: int
    arms: list = field(default_factory=list)
    pilots: list = field(default_factory=list)
    decisions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
