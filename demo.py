"""Run the submitted agent and produce a self-contained, offline demo report.

    python demo.py --seed 42

The dashboard contains public inputs, pilot observations and the agent's own
estimates. It does not inspect the environment's hidden effects or score model.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import numbers
import os
import time
from pathlib import Path


LABELS = {
    "audience_size": "Клиентов в аудитории",
    "customers": "Клиентов",
    "customer_count": "Клиентов в аудитории",
    "n_customers": "Контактов",
    "n_candidates": "Кандидатов",
    "candidate_count": "Кандидатов",
    "observed_arms": "Проверенных гипотез",
    "hypotheses": "Гипотез",
    "history_rows": "Исторических наблюдений",
    "tariff_count": "Доступных тарифов",
    "mean_predicted_arpu": "Средний прогноз ARPU",
    "baseline_arpu": "Базовый ARPU аудитории",
    "total_budget": "Общий бюджет",
    "initial_budget": "Начальный бюджет",
    "remaining_budget": "Бюджет после пилотов",
    "remaining_budget_after_pilots": "Бюджет после пилотов",
    "remaining_contacts": "Охват после пилотов",
    "remaining_contacts_after_pilots": "Охват после пилотов",
    "initial_contacts": "Начальный лимит контактов",
    "max_total_contacts": "Лимит контактов",
    "pilot_cost": "Расходы на пилоты",
    "pilot_contacts": "Контакты в пилотах",
    "pilots_used": "Использовано пилотов",
    "pilots_left": "Осталось пилотов",
    "planned_cost": "Расходы по плану",
    "planned_contacts": "Контакты по плану",
    "final_cost": "Расходы на финальные кампании",
    "final_contacts": "Контакты в финальных кампаниях",
    "projected_remaining_budget": "Ожидаемый бюджет после финального плана",
    "projected_remaining_contacts": "Ожидаемый остаток контактов после плана",
    "expected_contacts": "Ожидаемый охват",
    "expected_cost": "Ожидаемые расходы",
    "expected_net_gain": "Прогноз чистого эффекта",
    "estimated_net_gain": "Оценка чистого эффекта",
    "estimated_lift": "Оценка относительного эффекта",
    "estimated_lift_ratio": "Оценка относительного эффекта",
    "observed_lift_ratio": "Наблюдаемый относительный эффект",
    "observed_lift_total": "Наблюдаемый общий прирост",
    "posterior_mean": "Средняя оценка эффекта",
    "posterior_std": "Неопределённость оценки",
    "standard_error": "Стандартная ошибка оценки",
    "filters": "Фильтры аудитории",
    "lower_bound": "Нижняя граница оценки",
    "cost": "Расходы",
    "contacts": "Контакты",
    "channel": "Канал",
    "target_tariff": "Целевой тариф",
    "current_tariff": "Текущий тариф",
    "arpu_segment": "Сегмент ARPU",
    "data_segment": "Интернет",
    "call_segment": "Звонки",
    "filter_arpu_segment": "Сегмент ARPU",
    "filter_data_segment": "Интернет",
    "filter_call_segment": "Звонки",
    "filter_current_tariff": "Текущий тариф",
    "campaign_name": "Кампания",
    "pilot": "Пилот",
    "reason": "Обоснование",
    "stage": "Этап",
    "action": "Действие",
    "message": "Пояснение",
    "runtime_seconds": "Время работы, с",
}


def _plain(value):
    """Convert report scalars to strict JSON without leaking runtime objects."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, numbers.Real):
        return float(value) if math.isfinite(float(value)) else None
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def _text(value):
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    if isinstance(value, numbers.Real):
        return f"{value:,.0f}".replace(",", " ") if float(value).is_integer() else f"{value:,.2f}".replace(",", " ")
    return str(value)


def _escape(value):
    return html.escape(_text(value), quote=True)


def _label(key):
    return LABELS.get(key, key.replace("_", " "))


def _detail(value):
    """Render arbitrary future report fields while escaping all data strings."""
    if isinstance(value, dict):
        return '<dl class="details-grid">' + "".join(
            f"<dt>{_escape(_label(key))}</dt><dd>{_detail(item)}</dd>"
            for key, item in value.items()
        ) + "</dl>"
    if isinstance(value, list):
        return '<ul class="detail-list">' + "".join(f"<li>{_detail(item)}</li>" for item in value) + "</ul>" if value else "—"
    return _escape(value)


def _rows(value):
    return value if isinstance(value, list) else []


def _percent(value):
    if isinstance(value, numbers.Real) and math.isfinite(value):
        return f"{value:+.1%}"
    return "—"


def _bar(label, value, maximum, annotation=""):
    value = max(0, float(value or 0))
    maximum = max(0, float(maximum or 0))
    width = 100 * min(1, value / maximum) if maximum else 0
    return (
        '<div class="bar-row">'
        f'<div class="bar-caption"><span>{_escape(label)}</span><strong>{_escape(annotation or _text(value))}</strong></div>'
        f'<div class="track"><div class="bar" style="width:{width:.2f}%"></div></div></div>'
    )


def build_report(agent, env, campaigns, seed, elapsed):
    """Read only the same public environment attributes available to the agent."""
    report = dict(getattr(agent, "last_report", {}) or {})
    profile = env.customer_profile
    audience = {
        "customer_count": int(len(profile)),
        "tariff_count": int(len(env.tariffs)),
        "mean_predicted_arpu": float(profile["predicted_arpu"].mean()) if len(profile) else 0,
        "segments": {
            str(key): int(value)
            for key, value in profile["arpu_segment"].value_counts().sort_index().items()
        },
    }
    report["demo"] = {
        "seed": seed,
        "mode": "public_mock_environment",
        "elapsed_seconds": elapsed,
        "note": "Синтетическая локальная среда. Наблюдения пилотов содержат шум. Прогноз агента не является результатом скрытого судейского скоринга.",
    }
    report["audience_summary"] = audience
    report["public_resource_snapshot"] = {
        "total_budget": env.total_budget,
        "max_total_contacts": env.max_total_contacts,
        "remaining_budget": env.remaining_budget,
        "remaining_contacts": env.remaining_contacts,
        "pilots_left": env.pilots_left,
        "pilots_used": len(env.pilot_history),
    }
    report["public_pilot_history"] = list(env.pilot_history)
    report["final_campaigns"] = campaigns
    return _plain(report)


def render_dashboard(report):
    audience = report["audience_summary"]
    resources = report["public_resource_snapshot"]
    pilots = report["public_pilot_history"]
    campaigns = report["final_campaigns"]
    rich_campaigns = _rows(report.get("campaigns"))
    seed = report["demo"]["seed"]
    runtime = report.get("runtime_seconds", report["demo"]["elapsed_seconds"])
    pilot_cost = resources["total_budget"] - resources["remaining_budget"]
    pilot_contacts = resources["max_total_contacts"] - resources["remaining_contacts"]
    cards = "".join(
        '<div class="metric">' + f'<div class="metric-label">{_escape(label)}</div><strong>{_escape(value)}</strong><span>{_escape(note)}</span></div>'
        for label, value, note in [
            ("Аудитория", audience["customer_count"], "клиентов в целевой выборке"),
            ("Пилоты", len(pilots), "проведено в этом прогоне"),
            ("Кампании", len(campaigns), "в итоговом плане агента"),
            ("Время работы", f"{runtime:.2f} с" if isinstance(runtime, numbers.Real) else runtime, "один полный цикл решений"),
        ]
    )
    segment_bars = "".join(
        _bar(key, value, audience["customer_count"], f"{_text(value)} клиентов")
        for key, value in audience["segments"].items()
    )
    budget_bars = _bar("Бюджет, потраченный на пилоты", pilot_cost, resources["total_budget"], f"{_text(pilot_cost)} / {_text(resources['total_budget'])}")
    budget_bars += _bar("Контакты в пилотах", pilot_contacts, resources["max_total_contacts"], f"{_text(pilot_contacts)} / {_text(resources['max_total_contacts'])}")
    resource_extra = ""
    if report.get("resources"):
        resource_extra = '<details><summary>Расчёт ресурсов с учётом плана</summary>' + _detail(report["resources"]) + "</details>"
    pilot_rows = ""
    for pilot in pilots:
        ratio = pilot.get("observed_lift_ratio")
        tone = "positive" if isinstance(ratio, numbers.Real) and ratio > 0 else "negative"
        pilot_rows += (
            f'<tr><td class="name">{_escape(pilot.get("pilot"))}</td>'
            f'<td>{_escape(pilot.get("target_tariff"))}</td><td><span class="badge">{_escape(pilot.get("channel"))}</span></td>'
            f'<td class="number">{_escape(pilot.get("n_customers"))}</td><td class="number">{_escape(pilot.get("cost"))}</td>'
            f'<td class="number {tone}">{_escape(_percent(ratio))}</td>'
            f'<td class="number">{_escape(pilot.get("observed_lift_total"))}</td></tr>'
        )
    if not pilot_rows:
        pilot_rows = '<tr><td colspan="7" class="empty">В этом прогоне пилоты не проводились.</td></tr>'
    campaign_cards = ""
    for index, campaign in enumerate(campaigns):
        filters = [
            f'<span class="filter">{_escape(_label(key))}: <b>{_escape(value)}</b></span>'
            for key, value in campaign.items() if key.startswith("filter_") and value is not None
        ]
        extra = next((item for item in rich_campaigns if isinstance(item, dict) and item.get("campaign_name") == campaign.get("campaign_name")), None)
        if extra is None and index < len(rich_campaigns):
            extra = rich_campaigns[index]
        forecast = ""
        if isinstance(extra, dict):
            forecast_fields = [
                ("Охват", extra.get("expected_contacts")),
                ("Расходы", extra.get("expected_cost")),
                ("Чистый эффект", extra.get("expected_net_gain")),
            ]
            if any(value is not None for _, value in forecast_fields):
                forecast = '<p class="forecast-caption">Прогноз агента для финальной кампании</p><div class="forecast">' + "".join(
                    f'<div><span>{_escape(label)}</span><strong>{_escape(value)}</strong></div>'
                    for label, value in forecast_fields
                ) + "</div>"
        campaign_cards += (
            '<article class="campaign">'
            f'<div class="campaign-heading"><span class="campaign-index">{index + 1:02d}</span><div><h3>{_escape(campaign.get("campaign_name", "Кампания"))}</h3>'
            f'<p>Предложить <strong>{_escape(campaign.get("target_tariff"))}</strong> через <strong>{_escape(campaign.get("channel"))}</strong></p></div></div>'
            '<div class="filters">' + ("".join(filters) or '<span class="filter">Вся доступная аудитория</span>') + "</div>"
            + forecast
            + (f'<details><summary>Оценка и обоснование агента</summary>{_detail(extra)}</details>' if extra else "")
            + "</article>"
        )
    if not campaign_cards:
        campaign_cards = '<div class="empty">Агент не выбрал финальные кампании. Причины и предупреждения приведены ниже.</div>'
    decision_items = report.get("decisions", [])
    decisions = _detail(decision_items) if decision_items else '<p class="muted">Подробный журнал решений не экспортирован агентом.</p>'
    if report.get("assumptions"):
        decisions += '<details><summary>Допущения при построении прогноза</summary>' + _detail(report["assumptions"]) + "</details>"
    warnings = report.get("warnings", [])
    warnings_html = '<aside class="warning"><strong>Замечания агента</strong>' + _detail(warnings) + "</aside>" if warnings else ""
    overview = '<details><summary>Аналитика и параметры поиска</summary>' + _detail(report["overview"]) + "</details>" if report.get("overview") else ""
    enriched_pilots = '<details><summary>Гипотезы и обновления оценок</summary>' + _detail(report["pilots"]) + "</details>" if report.get("pilots") else ""
    return f'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light">
<title>Campaign Lab · Агент тарифных кампаний</title>
<style>
:root{{--ink:#1d211e;--muted:#68706a;--yellow:#ffe047;--paper:#f3f4ef;--line:#e0e3d9;--white:#fff;--green:#256443;--red:#9e4237}}
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--paper);color:var(--ink);font:15px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}}a{{color:inherit}}header{{background:var(--ink);color:white}}.wrap{{max-width:1200px;margin:auto;padding:0 36px}}.brand-row{{display:flex;align-items:center;justify-content:space-between;padding:28px 0;border-bottom:1px solid #454b43;gap:16px}}.brand{{font-size:18px;font-weight:750;letter-spacing:-.5px;display:flex;gap:12px;align-items:center}}.mark{{display:block;width:30px;height:30px;border-radius:50%;background:repeating-linear-gradient(0deg,var(--yellow) 0 7px,var(--ink) 7px 12px)}}.eyebrow{{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--yellow);font-weight:700}}.run-badge{{font-size:12px;color:#d1d7ce;border:1px solid #5f685d;border-radius:100px;padding:6px 14px}}.hero{{padding:45px 0 40px;max-width:790px}}h1{{font-size:clamp(32px,5vw,54px);line-height:1.1;letter-spacing:-2px;margin:14px 0 20px;font-weight:700}}.hero p{{font-size:17px;color:#c9d0c6;max-width:660px;margin:0}}nav{{display:flex;gap:26px;overflow:auto;padding:20px 0;font-size:13px;white-space:nowrap}}nav a{{text-decoration:none;color:#d7dfd4}}nav a:hover{{color:var(--yellow)}}.metrics{{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid var(--line);border-radius:16px;background:white;margin:28px 0;overflow:hidden}}.metric{{padding:23px 25px;border-right:1px solid var(--line)}}.metric:last-child{{border:0}}.metric-label{{font-size:12px;color:var(--muted)}}.metric strong{{display:block;font-size:34px;line-height:1.3;letter-spacing:-1px;margin:7px 0}}.metric span{{font-size:11px;color:var(--muted)}}section{{scroll-margin-top:20px;margin:32px 0}}.section-heading{{display:flex;align-items:baseline;justify-content:space-between;gap:20px;margin-bottom:16px}}h2{{font-size:23px;margin:0;letter-spacing:-.7px}}.section-num{{font:12px ui-monospace,monospace;color:var(--muted);margin-right:12px}}.section-note{{font-size:12px;color:var(--muted)}}.columns{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}.panel{{background:white;border:1px solid var(--line);border-radius:16px;padding:26px}}.panel h3{{font-size:15px;margin:0 0 22px}}.bar-row{{margin:18px 0}}.bar-caption{{display:flex;justify-content:space-between;gap:14px;font-size:12px;margin-bottom:9px}}.bar-caption strong{{font-weight:600;white-space:nowrap}}.track{{height:8px;border-radius:5px;background:#eef0e9;overflow:hidden}}.bar{{height:100%;border-radius:5px;background:var(--yellow)}}.stat-line{{display:flex;justify-content:space-between;gap:14px;border-top:1px solid var(--line);padding:14px 0 0;margin:16px 0 0;font-size:12px;color:var(--muted)}}.stat-line b{{color:var(--ink)}}.table-wrap{{overflow:auto;background:white;border:1px solid var(--line);border-radius:16px}}table{{border-collapse:collapse;width:100%;white-space:nowrap;font-size:12px}}th{{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);font-weight:600;background:#fafbf7}}th,td{{padding:15px 17px;text-align:left;border-bottom:1px solid var(--line)}}tr:last-child td{{border:0}}.number{{text-align:right;font-variant-numeric:tabular-nums}}.name{{font-weight:650}}.badge{{border:1px solid #e3e7dc;border-radius:6px;padding:3px 8px;font-size:11px}}.positive{{color:var(--green);font-weight:650}}.negative{{color:var(--red);font-weight:650}}.notice{{font-size:12px;color:var(--muted);max-width:800px;margin:12px 0 0}}.campaigns{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}.campaign{{border:1px solid var(--line);border-radius:16px;padding:22px;background:white;min-width:0}}.campaign-heading{{display:flex;gap:16px}}.campaign-index{{width:34px;height:34px;background:var(--yellow);border-radius:10px;display:grid;place-items:center;font:600 12px ui-monospace,monospace;flex-shrink:0}}h3{{margin:0;font-size:14px;overflow-wrap:anywhere}}.campaign p{{font-size:12px;color:var(--muted);margin:5px 0 0}}.campaign p strong{{color:var(--ink);font-weight:600}}.filters{{display:flex;flex-wrap:wrap;gap:7px;margin:16px 0 0}}.filter{{font-size:10px;padding:4px 9px;border-radius:5px;background:#f1f3ed;color:var(--muted)}}.filter b{{color:var(--ink);font-weight:550}}details{{margin-top:18px;padding-top:15px;border-top:1px solid var(--line)}}summary{{font-size:12px;cursor:pointer;color:var(--muted)}}summary:hover{{color:var(--ink)}}.details-grid{{display:grid;grid-template-columns:minmax(120px,.9fr) minmax(0,1.4fr);gap:9px 18px;font-size:12px;line-height:1.6;margin:15px 0 0}}dt{{color:var(--muted);overflow-wrap:anywhere}}dd{{margin:0;overflow-wrap:anywhere}}dd .details-grid{{margin:0}}.detail-list{{margin:12px 0 0;padding-left:20px;font-size:13px}}.detail-list li{{padding:5px 0}}.detail-list .details-grid{{margin:0 0 10px}}.warning{{background:#fff5cf;border:1px solid #ead587;border-radius:12px;padding:18px 22px;margin:24px 0;font-size:13px}}.empty{{padding:28px;color:var(--muted);font-size:13px}}.muted{{color:var(--muted);font-size:13px}}.footer{{padding:30px 0 38px;margin-top:40px;border-top:1px solid var(--line);display:flex;justify-content:space-between;gap:24px;font-size:12px;color:var(--muted)}}.download{{display:inline-flex;align-items:center;border-radius:8px;background:var(--ink);color:white;padding:9px 14px;text-decoration:none;font-weight:600;white-space:nowrap;align-self:flex-start}}.footer p{{margin:0;max-width:740px}}@media(max-width:850px){{.wrap{{padding:0 22px}}.metrics{{grid-template-columns:1fr 1fr}}.metric:nth-child(2){{border-right:0}}.metric:nth-child(-n+2){{border-bottom:1px solid var(--line)}}.columns,.campaigns{{grid-template-columns:1fr}}.section-note{{display:none}}.hero{{padding:32px 0}}.metric{{padding:18px 20px}}}}@media(max-width:480px){{.wrap{{padding:0 16px}}.brand-row{{padding:20px 0}}.run-badge{{font-size:10px;padding:5px 10px}}.metric strong{{font-size:27px}}.metric span{{font-size:10px}}.panel{{padding:20px}}.footer{{flex-direction:column}}.details-grid{{grid-template-columns:1fr 1fr;gap:9px}}h1{{letter-spacing:-1px}}}}@media print{{header{{background:white;color:black}}.hero p,.eyebrow,nav a{{color:black}}nav,.download{{display:none}}.metrics,.panel,.campaign,.table-wrap{{break-inside:avoid}}.wrap{{max-width:none;padding:0}}body{{font-size:12px}}details{{display:block}}}}
.campaign .forecast-caption{{margin-top:20px;font-size:10px;color:var(--muted)}}.forecast{{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:8px;padding-top:10px;border-top:1px solid var(--line)}}.forecast span{{display:block;font-size:10px;color:var(--muted)}}.forecast strong{{display:block;font-size:18px;font-weight:600;letter-spacing:-.3px;margin-top:3px;font-variant-numeric:tabular-nums}}
</style>
</head>
<body>
<header><div class="wrap"><div class="brand-row"><div class="brand"><span class="mark" aria-hidden="true"></span>Campaign Lab</div><span class="run-badge">ЛОКАЛЬНЫЙ ПРОГОН · SEED {_escape(seed)}</span></div><div class="hero"><div class="eyebrow">HackAlem AI / Beeline · агентская система</div><h1>От гипотезы<br>к плану кампаний.</h1><p>Агент изучает аудиторию, проверяет предложения пилотами и выбирает кампании с учётом бюджета, охвата и неопределённости.</p></div><nav aria-label="Разделы отчёта"><a href="#audience">01 · Аудитория и ресурсы</a><a href="#pilots">02 · Пилоты</a><a href="#campaigns">03 · План кампаний</a><a href="#decisions">04 · Решения</a></nav></div></header>
<main class="wrap"><div class="metrics">{cards}</div>{warnings_html}
<section id="audience"><div class="section-heading"><h2><span class="section-num">01</span>Аудитория и ресурсы</h2><span class="section-note">Данные публичного интерфейса среды</span></div><div class="columns"><div class="panel"><h3>Распределение клиентов по ARPU</h3>{segment_bars}<div class="stat-line"><span>Средний прогноз ARPU</span><b>{_escape(audience['mean_predicted_arpu'])}</b></div><div class="stat-line"><span>Доступных тарифов</span><b>{_escape(audience['tariff_count'])}</b></div>{overview}</div><div class="panel"><h3>Стоимость исследования аудитории</h3>{budget_bars}<div class="stat-line"><span>Бюджет после пилотов</span><b>{_escape(resources['remaining_budget'])}</b></div><div class="stat-line"><span>Контакты после пилотов</span><b>{_escape(resources['remaining_contacts'])}</b></div><div class="stat-line"><span>Доступно новых пилотов</span><b>{_escape(resources['pilots_left'])}</b></div>{resource_extra}</div></div></section>
<section id="pilots"><div class="section-heading"><h2><span class="section-num">02</span>Что показали пилоты</h2><span class="section-note">{len(pilots)} наблюдений из этого прогона</span></div><div class="table-wrap"><table><thead><tr><th>Пилот</th><th>Тариф</th><th>Канал</th><th class="number">Контакты</th><th class="number">Расходы</th><th class="number">Эффект, %</th><th class="number">Общий прирост</th></tr></thead><tbody>{pilot_rows}</tbody></table></div><p class="notice">Эффект и прирост — шумные наблюдения, возвращённые пилотами. Они не являются точным эффектом кампаний или итоговым чистым результатом.</p>{enriched_pilots}</section>
<section id="campaigns"><div class="section-heading"><h2><span class="section-num">03</span>Итоговый план</h2><span class="section-note">{len(campaigns)} кампаний возвращено методом Agent.act(env)</span></div><div class="campaigns">{campaign_cards}</div><p class="notice">Это предложения агента для финального запуска. Остатки среды выше отражают уже проведённые пилоты; параметры финальных кампаний передаются оценщику отдельно.</p></section>
<section id="decisions"><div class="section-heading"><h2><span class="section-num">04</span>Почему агент выбрал этот план</h2></div><div class="panel">{decisions}</div></section>
<footer class="footer"><p>{_escape(report['demo']['note'])}<br>Отчёт работает без интернета. Для повторного прогона: <code>python demo.py --seed {_escape(seed)}</code>.</p><a class="download" href="demo_report.json" download>Скачать JSON отчёта ↓</a></footer></main>
</body></html>'''


def main(argv=None):
    parser = argparse.ArgumentParser(description="Запустить агента и собрать автономный HTML-отчёт.")
    parser.add_argument("--seed", type=int, default=42, help="Seed публичной mock-среды (по умолчанию: 42).")
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"), help="Папка для demo_report.json и dashboard.html.")
    args = parser.parse_args(argv)
    output_dir = args.output_dir.resolve()
    # The participant factory reads its data relative to the project directory.
    previous_cwd = Path.cwd()
    try:
        os.chdir(Path(__file__).resolve().parent)
        from agent import Agent
        from mock_environment import make_mock_env

        env, _ = make_mock_env(seed=args.seed)
        agent = Agent()
        started = time.perf_counter()
        campaigns = agent.act(env) or []
        report = build_report(agent, env, campaigns, args.seed, time.perf_counter() - started)
    finally:
        os.chdir(previous_cwd)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "demo_report.json"
    dashboard_path = output_dir / "dashboard.html"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    dashboard_path.write_text(render_dashboard(report), encoding="utf-8")
    print(f"JSON: {report_path}")
    print(f"HTML: {dashboard_path}")
    print(f"Пилотов: {len(report['public_pilot_history'])}; финальных кампаний: {len(campaigns)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
