"""Explain local runs using public observations and evaluator output only.

This module is part of Campaign Studio, not the submitted agent. A successful
contract check says nothing about the sign of the effect or hidden judging.
Missing evidence is never silently treated as a successful check.
"""

from __future__ import annotations

import math
import numbers


FILTERS = ("filter_current_tariff", "filter_arpu_segment",
           "filter_data_segment", "filter_call_segment")


def _number(value):
    return isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(float(value))


def _integer(value):
    return isinstance(value, numbers.Integral) and not isinstance(value, bool)


def build_validation(campaigns, score, report, agent_runtime_seconds, *, plan_validated=False):
    """Record checks actually performed for one run, independently of profit."""
    checks = []

    def check(identifier, label, observed, limit, passed):
        checks.append({"id": identifier, "label": label,
                       "status": "not_checked" if passed is None else "passed" if passed else "failed",
                       "observed": observed, "limit": limit})

    check("plan_schema", "Тарифы, каналы и фильтры CSV", "Проверены" if plan_validated else None,
          "Публичный контракт CSV", True if plan_validated else None)
    campaign_count = len(campaigns) if isinstance(campaigns, list) else None
    check("final_campaign_count", "Финальных кампаний", campaign_count, "1–10",
          1 <= campaign_count <= 10 if campaign_count is not None else None)

    score = score if isinstance(score, dict) else {}
    report = report if isinstance(report, dict) else {}
    pilots = report.get("public_pilot_history")
    valid_history = isinstance(pilots, list) and all(isinstance(item, dict) for item in pilots)
    pilot_count = score.get("n_pilots")
    check("pilot_count", "Проведённых пилотов", pilot_count, "1–20",
          1 <= pilot_count <= 20 if _integer(pilot_count) else None)
    check("pilot_history", "Пилоты подтверждены публичной историей", len(pilots) if valid_history else None,
          pilot_count, len(pilots) == pilot_count if valid_history and _integer(pilot_count) else None)
    sizes = [item.get("n_customers") for item in pilots] if valid_history else None
    known_sizes = sizes is not None and all(_integer(size) for size in sizes)
    size_summary = f"{min(sizes)}–{max(sizes)}" if known_sizes and sizes else "Нет пилотов" if known_sizes else None
    check("pilot_sizes", "Клиентов в каждом пилоте", size_summary, "10–200",
          bool(sizes) and all(10 <= size <= 200 for size in sizes) if known_sizes else None)

    total_cost, total_contacts = score.get("total_cost"), score.get("total_contacts")
    check("budget", "Расходы вместе с пилотами", total_cost, "≤ 100 000 у.е.",
          0 <= total_cost <= 100000 if _number(total_cost) else None)
    check("contacts", "Контакты вместе с пилотами", total_contacts, "1–15 000",
          1 <= total_contacts <= 15000 if _integer(total_contacts) else None)

    scored_count = score.get("n_campaigns")
    expected_count = (pilot_count + campaign_count
                      if _integer(pilot_count) and campaign_count is not None else None)
    details = score.get("campaigns_detail")
    valid_details = isinstance(details, list) and all(isinstance(item, dict) for item in details)
    counts_match = (scored_count == expected_count == len(details)
                    if _integer(scored_count) and expected_count is not None and valid_details else None)
    correspondence = [counts_match]
    if counts_match and valid_history and len(pilots) == pilot_count:
        for pilot, detail in zip(pilots, details[:pilot_count]):
            for source_key, scored_key in (("pilot", "name"), ("channel", "channel")):
                source, scored = pilot.get(source_key), detail.get(scored_key)
                correspondence.append(source == scored if isinstance(source, str) and isinstance(scored, str) else None)
            source_n, scored_n = pilot.get("n_customers"), detail.get("n_contacts")
            correspondence.append(source_n == scored_n if _integer(source_n) and _integer(scored_n) else None)
            source_cost, scored_cost = pilot.get("cost"), detail.get("cost")
            correspondence.append(math.isclose(source_cost, scored_cost, abs_tol=1e-7)
                                  if _number(source_cost) and _number(scored_cost) else None)
        for campaign, detail in zip(campaigns, details[pilot_count:]):
            if not isinstance(campaign, dict):
                correspondence.append(False)
                continue
            source_channel, scored_channel = campaign.get("channel"), detail.get("channel")
            correspondence.append(source_channel == scored_channel
                                  if isinstance(source_channel, str) and isinstance(scored_channel, str) else None)
            # campaign_name is optional. Compare it when supplied, without
            # inventing a name for pandas' missing optional value.
            if campaign.get("campaign_name") is not None:
                correspondence.append(campaign["campaign_name"] == detail.get("name"))
        # Target tariffs and filters are absent from campaigns_detail; their
        # schema is checked separately, not falsely claimed as scorer evidence.
    elif counts_match:
        correspondence.append(None)
    rows_match = (False if False in correspondence else None if None in correspondence else True)
    check("scored_plan", "Имена и каналы кампаний, контакты и расходы пилотов",
          f"{scored_count} кампаний / {len(details)} записей" if _integer(scored_count) and valid_details else None,
          expected_count,
          rows_match)

    final_details = (details[pilot_count:] if valid_details and _integer(pilot_count)
                     and 0 <= pilot_count <= len(details) else None)
    final_contacts = [item.get("n_contacts") for item in final_details] if final_details is not None else None
    known_final_contacts = final_contacts is not None and all(_integer(value) for value in final_contacts)
    final_summary = "; ".join(map(str, final_contacts)) if known_final_contacts else None
    check("final_campaign_contacts", "Контактов в каждой финальной кампании", final_summary,
          "1–5 000",
          len(final_contacts) == campaign_count and all(1 <= value <= 5000 for value in final_contacts)
          if known_final_contacts else None)

    detail_costs = [item.get("cost") for item in details] if valid_details else []
    detail_contacts = [item.get("n_contacts") for item in details] if valid_details else []
    known_accounting = (valid_details and _number(total_cost) and _integer(total_contacts)
                        and all(_number(value) for value in detail_costs)
                        and all(_integer(value) for value in detail_contacts))
    sums = {"cost": sum(detail_costs), "contacts": sum(detail_contacts)} if known_accounting else None
    check("accounting", "Итоги совпадают с деталями оценщика",
          f"{sums['cost']:g} у.е. / {sums['contacts']} контактов" if sums else None,
          f"{total_cost:g} у.е. / {total_contacts} контактов" if _number(total_cost) and _integer(total_contacts) else "Итоги оценщика",
          math.isclose(sums["cost"], total_cost, abs_tol=1e-7) and sums["contacts"] == total_contacts
          and all(value >= 0 for value in detail_costs) and all(value > 0 for value in detail_contacts)
          if known_accounting else None)
    check("runtime", "Вызов Agent.act короче 5 минут", agent_runtime_seconds, "< 300 секунд",
          0 <= agent_runtime_seconds < 300 if _number(agent_runtime_seconds) else None)
    status = ("failed" if any(item["status"] == "failed" for item in checks)
              else "not_checked" if any(item["status"] == "not_checked" for item in checks)
              else "passed")
    return {"status": status, "scope": "public_local_contract", "checks": checks,
            "hidden_judging": "not_checked"}


def _filters(value):
    return {key: value[key] for key in FILTERS if value.get(key) not in (None, "")}


def _contains(pilot_filters, campaign_filters):
    """Whether the pilot's public segment contains the planned subgroup."""
    for key, value in pilot_filters.items():
        selected = campaign_filters.get(key)
        if selected is None:
            return False
        if key == "filter_current_tariff":
            source = {part.strip() for part in str(value).split(";") if part.strip()}
            target = {part.strip() for part in str(selected).split(";") if part.strip()}
            if not target or not target.issubset(source):
                return False
        elif value != selected:
            return False
    return True


def _pilot_group(rows):
    latest = rows[-1][1]
    sizes = [item.get("n_customers") for _, item in rows]
    return {"pilot_indexes": [index for index, _ in rows],
            "pilot_count": len(rows),
            "sample_contacts": sum(sizes) if all(_integer(value) for value in sizes) else None,
            "latest_observed_lift_ratio": latest.get("observed_lift_ratio"),
            "posterior_mean": latest.get("posterior_mean"),
            "standard_error": latest.get("standard_error")}


def build_explanations(campaigns, report):
    """Link decisions to their actual pilot cell; do not invent comparisons.

    Posterior estimates are not scorer effects. Repeated pilot contacts may
    overlap, so sample_contacts is deliberately not named unique customers.
    An alternative is reported as observed, never as a proof of optimality.
    """
    report = report if isinstance(report, dict) else {}
    pilots = [(index, item) for index, item in enumerate(report.get("pilots", []), 1)
              if isinstance(item, dict) and isinstance(item.get("filters"), dict)]
    rich = {item.get("campaign_name"): item for item in report.get("campaigns", [])
            if isinstance(item, dict) and item.get("campaign_name")}
    explanations = []
    for index, campaign in enumerate(campaigns):
        filters = _filters(campaign)
        target, channel = campaign.get("target_tariff"), campaign.get("channel")
        candidates = [(position, pilot) for position, pilot in pilots
                      if pilot.get("target_tariff") == target and pilot.get("channel") == channel
                      and _contains(_filters(pilot["filters"]), filters)]
        # If several containing cells exist, prefer an exact cell, then the
        # most specific observed cell. Never pool estimates of different cells.
        selected_filters = None
        if candidates:
            selected_filters = max((_filters(pilot["filters"]) for _, pilot in candidates),
                                   key=lambda cell: (cell == filters, len(cell)))
        matched = [(position, pilot) for position, pilot in candidates
                   if _filters(pilot["filters"]) == selected_filters]
        alternatives = []
        if matched:
            groups = {}
            for position, pilot in pilots:
                if _filters(pilot["filters"]) != selected_filters:
                    continue
                other_target, other_channel = pilot.get("target_tariff"), pilot.get("channel")
                if other_target == target and other_channel != channel:
                    kind = "channel"
                elif other_channel == channel and other_target != target:
                    kind = "tariff"
                else:
                    continue
                groups.setdefault((kind, other_target, other_channel), []).append((position, pilot))
            for (kind, other_target, other_channel), rows in groups.items():
                alternatives.append({"kind": kind, "target_tariff": other_target,
                                     "channel": other_channel, **_pilot_group(rows)})
        record = rich.get(campaign.get("campaign_name"), {})
        explanations.append({
            "campaign_index": index, "campaign_name": campaign.get("campaign_name"),
            "filters": filters, "target_tariff": target, "channel": channel,
            "evidence_kind": "pilot_observations" if matched else "no_matching_pilot",
            "pilot_filters": selected_filters,
            "broader_segment": selected_filters != filters if matched else None,
            "pilot_evidence": _pilot_group(matched) if matched else None,
            "observed_alternatives": alternatives,
            "forecast": {key: record.get(key) for key in
                         ("eligible_customers", "expected_contacts", "expected_cost",
                          "expected_net_gain", "estimated_lift", "standard_error")},
            "reason": record.get("reason"),
        })
    return explanations
