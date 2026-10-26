"""Transparent monthly mortgage forecasting with expanding-origin backtesting.

No fitting with future labels; exogenous sensitivities are user-specified scenarios,
NOT estimated causality. Reported bands are empirical error envelopes, not CIs.
"""
from __future__ import annotations
from datetime import date
from math import isfinite, sqrt
from statistics import mean, median
from typing import Sequence


def next_month(d: date) -> date:
    return date(d.year + (d.month == 12), d.month % 12 + 1, 1)


def validate_monthly(points: Sequence[tuple[date, float]]) -> None:
    if len(points) < 24:
        raise ValueError("Для прогноза требуется не менее 24 месячных наблюдений")
    for (a, av), (b, bv) in zip(points, points[1:]):
        if b != next_month(a):
            raise ValueError(f"Временной ряд имеет пропуск или повтор между {a} и {b}")
    if any(not isfinite(x) or x < 0 for _, x in points):
        raise ValueError("Объём кредитования должен быть конечным неотрицательным числом")


def _predict(values: list[float], model: str, horizon: int) -> float:
    # Recursive forecasts are strictly functions of past observations (or earlier predictions).
    if model == 'seasonal_naive':
        return values[-12] if len(values) >= 12 else values[-1]
    if model == 'rolling_mean':
        return mean(values[-6:])
    if model == 'damped_trend':
        short = mean(values[-3:]); long = mean(values[-9:-3]) if len(values) >= 9 else short
        drift = (short - long) / 6
        return max(0., short + drift * min(horizon, 3) * .7)
    raise ValueError('Unknown model')


def forecast_monthly(points: Sequence[tuple[date, float]], horizon: int = 3,
                     scenario_pct: float = 0.0) -> dict:
    if not 1 <= horizon <= 6:
        raise ValueError('Горизонт прогноза 1–6 месяцев')
    if not -15 <= scenario_pct <= 15:
        raise ValueError('Сценарий от -15 до +15% ежемесячно')
    points = sorted(points)
    validate_monthly(points)
    vals = [float(v) for _,v in points]
    models = ['seasonal_naive', 'rolling_mean', 'damped_trend']
    # Train/holdout separation: each forecast sees history strictly BEFORE test month.
    origins = range(max(18, len(vals)-12), len(vals))
    diagnostics = {}
    for model in models:
        errs = [abs(_predict(vals[:i], model, 1) - vals[i]) for i in origins]
        diagnostics[model] = {'mae': round(mean(errs), 4), 'n': len(errs)}
    best = min(models, key=lambda m: (diagnostics[m]['mae'], models.index(m)))
    # Empirical absolute error quantile with small-sample caution; not a calibrated interval.
    errors = sorted(abs(_predict(vals[:i], best, 1) - vals[i]) for i in origins)
    qpos = max(0, int(.8 * len(errors) + .99999) - 1)
    error_scale = errors[qpos]
    future = []
    simulated = vals.copy()
    month = points[-1][0]
    for step in range(1, horizon+1):
        month = next_month(month)
        # Sensitivity scenario is applied only to projected path, not backtest statistics.
        raw = max(0., _predict(simulated, best, step))
        projected = max(0., raw * (1 + scenario_pct / 100) ** step)
        width = error_scale * sqrt(step)
        future.append({'date': month.isoformat(), 'value': round(projected, 3),
                       'low': round(max(0., projected-width), 3),
                       'high': round(projected+width, 3)})
        simulated.append(projected)
    return {'model': best, 'alternatives': diagnostics, 'backtest_mae': diagnostics[best]['mae'],
            'backtest_points': len(errors), 'interval_method': 'empirical_q80_absolute_error_scaled_sqrt_h',
            'interval_warning': 'Эвристический диапазон по прошлым ошибкам, не доверительный интервал и не гарантия покрытия.',
            'scenario_pct_per_month': scenario_pct,
            'scenario_warning': 'Сценарная поправка — заданное пользователем допущение, не оценённый эффект ключевой ставки.',
            'training_from': points[0][0].isoformat(), 'training_through': points[-1][0].isoformat(),
            'training_count': len(points), 'forecasts': future}
