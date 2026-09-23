"""Final independent check; export only the seven supported submission columns."""
import numpy as np

from .models import FILTERS, VALID_SEGMENTS, campaign_mask
from .optimizer import effective_indexes


class Validator:
    def validate(self, records, state, budget, contacts):
        campaigns, verified_records = [], []
        used = np.zeros(len(state.profile), dtype=bool)
        for record in records[:10]:
            campaign = {key: record[key] for key in ["campaign_name", *FILTERS, "target_tariff", "channel"] if key in record}
            target, channel = campaign.get("target_tariff"), campaign.get("channel")
            if target not in state.tariffs or channel not in state.channels:
                state.warnings.append("Контролёр исключил неизвестный тариф или канал.")
                continue
            if any(campaign.get(key) is not None and campaign[key] not in allowed for key, allowed in VALID_SEGMENTS.items()):
                state.warnings.append("Контролёр исключил недопустимый фильтр.")
                continue
            if "filter_current_tariff" in campaign and not set(campaign["filter_current_tariff"].split(";")).issubset(state.tariffs):
                state.warnings.append("Контролёр исключил неизвестный текущий тариф.")
                continue
            raw_indexes = np.flatnonzero(campaign_mask(state.profile, campaign))
            price = float(state.channels[channel]["cost_per_contact"])
            indexes = effective_indexes(raw_indexes, contacts, budget, price)
            if not len(indexes) or used[indexes].any():
                state.warnings.append("Контролёр исключил пустую кампанию или повторные финальные контакты.")
                continue
            cost = float(len(indexes) * price)
            budget -= cost
            contacts -= len(indexes)
            used[indexes] = True
            campaigns.append(campaign)
            verified_records.append({**record, "expected_contacts": int(len(indexes)), "expected_cost": cost})
        return campaigns, verified_records
