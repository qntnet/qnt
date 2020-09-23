from .data import f, ds, load_assets, sort_and_crop_output, get_env
import xarray as xr
import numpy as np
import bottleneck
import pandas as pd
import gzip, base64, json
from urllib import parse, request
from tabulate import tabulate
import numba

EPS = 10 ** -7


def calc_slippage(data, period_days=14, fract=0.05, points_per_year=None):
    """
    :param data: xarray with historical data
    :param period_days: period for atr
    :param fract: slippage factor
    :return: xarray with slippage
    """
    if points_per_year is None:
        points_per_year = calc_avg_points_per_year(data)

    time_series = np.sort(data.coords[ds.TIME])
    data = data.transpose(ds.FIELD, ds.TIME, ds.ASSET).loc[[f.CLOSE, f.HIGH, f.LOW], time_series, :]

    points_per_day = calc_points_per_day(points_per_year)
    daily_period = min(points_per_day, len(data.time))

    cl = data.loc[f.CLOSE].shift({ds.TIME: daily_period})
    hi = data.loc[f.HIGH].rolling({ds.TIME: daily_period}).max()
    lo = data.loc[f.LOW].rolling({ds.TIME: daily_period}).min()
    d1 = hi - lo
    d2 = abs(hi - cl)
    d3 = abs(cl - lo)
    dd = xr.concat([d1, d2, d3], dim='d').max(dim='d', skipna=False)

    atr_period = min(len(dd.time), period_days * points_per_day)

    dd = dd.rolling({ds.TIME: atr_period}, min_periods=atr_period).mean(skipna=False).ffill(ds.TIME)
    return dd * fract


def calc_relative_return(data, portfolio_history, slippage_factor=0.05, roll_slippage_factor=0.02,
                         per_asset=False, points_per_year=None):
    target_weights = portfolio_history.shift(**{ds.TIME: 1})[1:]  # shift and cut first point

    slippage = calc_slippage(data, 14, slippage_factor, points_per_year=points_per_year)

    data, target_weights, slippage = arrange_data(data, target_weights, slippage, per_asset)

    W = target_weights
    D = data

    OPEN = D.loc[f.OPEN].ffill(ds.TIME).fillna(0)
    CLOSE = D.loc[f.CLOSE].ffill(ds.TIME).fillna(0)
    DIVS = D.loc[f.DIVS].fillna(0) if f.DIVS in D.coords[ds.FIELD] else xr.full_like(D.loc[f.CLOSE], 0)
    ROLL = D.loc[f.ROLL].fillna(0) if f.ROLL in D.coords[ds.FIELD] else None
    ROLL_SLIPPAGE = slippage.where(ROLL > 0).fillna(0) * roll_slippage_factor / slippage_factor if ROLL is not None else None

    # boolean matrix when assets available for trading
    UNLOCKED = np.logical_and(np.isfinite(D.loc[f.OPEN].values), np.isfinite(D.loc[f.CLOSE].values))
    UNLOCKED = np.logical_and(np.isfinite(W.values), UNLOCKED)
    UNLOCKED = np.logical_and(np.isfinite(slippage.values), UNLOCKED)
    UNLOCKED = np.logical_and(OPEN > EPS, UNLOCKED)

    if per_asset:
        RR = W.copy(True)
        RR[:] = calc_relative_return_np_per_asset(W.values, UNLOCKED.values, OPEN.values, CLOSE.values, slippage.values,
                                                  DIVS.values,
                                                  ROLL.values if ROLL is not None else None,
                                                  ROLL_SLIPPAGE.values if ROLL_SLIPPAGE is not None else None
                                                  )
        return RR
    else:
        RR = xr.DataArray(
            np.full([len(W.coords[ds.TIME])], np.nan, np.double),
            dims=[ds.TIME],
            coords={ds.TIME: W.coords[ds.TIME]}
        )
        res = calc_relative_return_np(W.values, UNLOCKED.values, OPEN.values, CLOSE.values, slippage.values,
                                      DIVS.values,
                                      ROLL.values if ROLL is not None else None,
                                      ROLL_SLIPPAGE.values if ROLL_SLIPPAGE is not None else None
                                      )
        RR[:] = res
        return RR


@numba.njit
def calc_relative_return_np_per_asset(WEIGHT, UNLOCKED, OPEN, CLOSE, SLIPPAGE, DIVS, ROLL, ROLL_SLIPPAGE):
    N = np.zeros(WEIGHT.shape)  # shares count

    equity_before_buy = np.zeros(WEIGHT.shape)
    equity_after_buy = np.zeros(WEIGHT.shape)
    equity_tonight = np.zeros(WEIGHT.shape)

    for t in range(0, WEIGHT.shape[0]):
        unlocked = UNLOCKED[t]  # available for trading

        if t == 0:
            equity_before_buy[0] = 1
            N[0] = 0
        else:
            N[t] = N[t - 1]
            equity_before_buy[t] = equity_after_buy[t - 1] + (OPEN[t] - OPEN[t - 1] + DIVS[t]) * N[t]

        N[t][unlocked] = equity_before_buy[t][unlocked] * WEIGHT[t][unlocked] / OPEN[t][unlocked]
        dN = N[t]
        if t > 0:
            dN = dN - N[t - 1]
        S = SLIPPAGE[t] * np.abs(dN)  # slippage for this step
        equity_after_buy[t] = equity_before_buy[t] - S
        equity_tonight[t] = equity_after_buy[t] + (CLOSE[t] - OPEN[t]) * N[t]

        locked = np.logical_not(unlocked)
        if t == 0:
            equity_before_buy[0][locked] = 1
            equity_after_buy[0][locked] = 1
            equity_tonight[0][locked] = 1
            N[0][locked] = 0
        else:
            N[t][locked] = N[t - 1][locked]
            equity_after_buy[t][locked] = equity_after_buy[t - 1][locked]
            equity_before_buy[t][locked] = equity_before_buy[t - 1][locked]
            equity_tonight[t][locked] = equity_tonight[t - 1][locked]

        if ROLL is not None and t > 0:
            pN = np.where(np.sign(N[t]) == np.sign(N[t-1]), np.minimum(np.abs(N[t]), np.abs(N[t-1])), 0)
            R = pN * (ROLL[t] + ROLL_SLIPPAGE[t])
            equity_after_buy[t] -= R

    E = equity_tonight
    # Ep = np.roll(E, 1, axis=0)
    Ep = E.copy()
    for i in range(1, Ep.shape[0]):
        Ep[i] = E[i-1]
    Ep[0] = 1
    RR = E / Ep - 1
    RR = np.where(np.isfinite(RR), RR, 0)
    return RR


@numba.njit
def calc_relative_return_np(WEIGHT, UNLOCKED, OPEN, CLOSE, SLIPPAGE, DIVS, ROLL, ROLL_SLIPPAGE):
    N = np.zeros(WEIGHT.shape)  # shares count

    equity_before_buy = np.zeros(WEIGHT.shape[0])
    equity_operable_before_buy = np.zeros(WEIGHT.shape[0])
    equity_after_buy = np.zeros(WEIGHT.shape[0])
    equity_tonight = np.zeros(WEIGHT.shape[0])

    for t in range(WEIGHT.shape[0]):
        unlocked = UNLOCKED[t]  # available for trading
        locked = np.logical_not(unlocked)

        if t == 0:
            equity_before_buy[0] = 1
            N[0] = 0
        else:
            N[t] = N[t - 1]
            equity_before_buy[t] = equity_after_buy[t - 1] + np.nansum((OPEN[t] - OPEN[t - 1] + DIVS[t]) * N[t])

        w_sum = np.nansum(np.abs(WEIGHT[t]))
        w_free_cash = max(1, w_sum) - w_sum
        w_unlocked = np.nansum(np.abs(WEIGHT[t][unlocked]))
        w_operable = w_unlocked + w_free_cash

        equity_operable_before_buy[t] = equity_before_buy[t] - np.nansum(OPEN[t][locked] * np.abs(N[t][locked]))

        if w_operable < EPS:
            equity_after_buy[t] = equity_before_buy[t]
        else:
            N[t][unlocked] = equity_operable_before_buy[t] * WEIGHT[t][unlocked] / (w_operable * OPEN[t][unlocked])
            dN = N[t][unlocked]
            if t > 0:
                dN = dN - N[t - 1][unlocked]
            S = np.nansum(SLIPPAGE[t][unlocked] * np.abs(dN))  # slippage for this step
            equity_after_buy[t] = equity_before_buy[t] - S

        if ROLL is not None and t > 0:
            pN = np.where(np.sign(N[t]) == np.sign(N[t-1]), np.minimum(np.abs(N[t]), np.abs(N[t-1])), 0)
            R = pN * (ROLL[t] + ROLL_SLIPPAGE[t])
            equity_after_buy[t] -= np.nansum(R)

        equity_tonight[t] = equity_after_buy[t] + np.nansum((CLOSE[t] - OPEN[t]) * N[t])

    E = equity_tonight
    Ep = np.roll(E, 1)
    Ep[0] = 1
    RR = E / Ep - 1
    RR = np.where(np.isfinite(RR), RR, 0)
    return RR


def arrange_data(data, target_weights, additional_series=None, per_asset=False):
    """
    arranges data for proper calculations
    :param per_asset:
    :param data:
    :param target_weights:
    :param additional_series:
    :return:
    """
    min_date = target_weights.coords[ds.TIME].min().values
    max_date = data.coords[ds.TIME].max().values

    if additional_series is not None:
        additional_series_without_nan = additional_series.dropna(ds.TIME, 'all')
        min_date = max(min_date, additional_series_without_nan.coords[ds.TIME].min().values)
        max_date = min(max_date, additional_series_without_nan.coords[ds.TIME].max().values)

    time_series = data.coords[ds.TIME]

    time_series = time_series.where(np.logical_and(time_series >= min_date, time_series <= max_date)).dropna(ds.TIME)
    time_series.values = np.sort(time_series)

    assets = np.intersect1d(target_weights.coords[ds.ASSET].values, data.coords[ds.ASSET].values, True)
    assets = np.sort(assets)

    adjusted_data = data.transpose(ds.FIELD, ds.TIME, ds.ASSET)
    adjusted_data = adjusted_data.loc[:, time_series, assets]

    adjusted_tw = xr.DataArray(
        np.full([len(time_series), len(assets)], np.nan, dtype=np.float64),
        dims=[ds.TIME, ds.ASSET],
        coords={
            ds.TIME: time_series,
            ds.ASSET: assets
        }
    )

    time_intersected = np.intersect1d(time_series.values, target_weights.coords[ds.TIME].values, True)

    weights_intersection = target_weights.transpose(ds.TIME, ds.ASSET).loc[time_intersected, assets]
    weights_intersection = weights_intersection.where(np.isfinite(weights_intersection)).fillna(0)

    adjusted_tw.loc[time_intersected, assets] = weights_intersection
    adjusted_tw = adjusted_tw.where(np.isfinite(adjusted_tw), 0)
    if f.IS_LIQUID in adjusted_data.coords[ds.FIELD]:
        adjusted_tw = adjusted_tw.where(adjusted_data.loc[f.IS_LIQUID] > 0, 0)

    if per_asset:
        adjusted_tw = xr.where(adjusted_tw > 1, 1, adjusted_tw)
        adjusted_tw = xr.where(adjusted_tw < -1, -1, adjusted_tw)
    else:
        s = abs(adjusted_tw).sum(ds.ASSET)
        s = xr.where(s < 1, 1, s)
        adjusted_tw = adjusted_tw / s

    if additional_series is not None:
        additional_series = additional_series.loc[time_series]
        if ds.ASSET in additional_series.dims:
            additional_series = additional_series.loc[:, assets]

    try:
        adjusted_tw = adjusted_tw.drop(ds.FIELD)
    except ValueError:
        pass

    return (adjusted_data, adjusted_tw, additional_series)


def calc_equity(relative_return):
    """
    :param relative_return: daily return
    :return: daily portfolio equity
    """
    return (relative_return + 1).cumprod(ds.TIME)


def calc_volatility(relative_return, max_periods=None, min_periods=2, points_per_year=None):
    """
    :param relative_return: daily return
    :param max_periods: maximal number of days
    :param min_periods: minimal number of days
    :return: portfolio volatility
    """
    if points_per_year is None:
        points_per_year = calc_avg_points_per_year(relative_return)
    if max_periods is None:
        max_periods = points_per_year
    max_periods = min(max_periods, len(relative_return.coords[ds.TIME]))
    min_periods = min(min_periods, max_periods)
    return relative_return.rolling({ds.TIME: max_periods}, min_periods=min_periods).std()


def calc_volatility_annualized(relative_return, max_periods=None, min_periods=2, points_per_year=None):
    """
    :param relative_return: daily return
    :param min_periods: minimal number of days
    :return: annualized volatility
    """
    if points_per_year is None:
        points_per_year = calc_avg_points_per_year(relative_return)
    if max_periods is None:
        max_periods = points_per_year
    return calc_volatility(relative_return, max_periods, min_periods, points_per_year=points_per_year) * pow(
        points_per_year, 1. / 2)


def calc_underwater(equity):
    """
    :param equity: daily portfolio equity
    :return: daily underwater
    """
    mx = equity.rolling({ds.TIME: len(equity)}, min_periods=1).max()
    return equity / mx - 1


def calc_max_drawdown(underwater):
    """
    :param underwater: daily underwater
    :return: daily maximum drawdown
    """
    return (underwater).rolling({ds.TIME: len(underwater)}, min_periods=1).min()


def calc_sharpe_ratio_annualized(relative_return, max_periods=None, min_periods=2, points_per_year=None):
    """
    :param relative_return: daily return
    :param max_periods: maximal number of days
    :param min_periods: minimal number of days
    :return: annualized Sharpe ratio
    """
    if points_per_year is None:
        points_per_year = calc_avg_points_per_year(relative_return)
    if max_periods is None:
        max_periods = points_per_year
    m = calc_mean_return_annualized(relative_return, max_periods, min_periods, points_per_year=points_per_year)
    v = calc_volatility_annualized(relative_return, max_periods, min_periods, points_per_year=points_per_year)
    sr = m / v
    return sr


def calc_mean_return(relative_return, max_periods=None, min_periods=1, points_per_year=None):
    """
    :param relative_return: daily return
    :param max_periods: maximal number of days
    :param min_periods: minimal number of days
    :return: daily mean return
    """
    if points_per_year is None:
        points_per_year = calc_avg_points_per_year(relative_return)
    if max_periods is None:
        max_periods = points_per_year
    max_periods = min(max_periods, len(relative_return.coords[ds.TIME]))
    min_periods = min(min_periods, max_periods)
    return xr.ufuncs.exp(
        xr.ufuncs.log(relative_return + 1).rolling({ds.TIME: max_periods}, min_periods=min_periods).mean(
            skipna=True)) - 1


def calc_mean_return_annualized(relative_return, max_periods=None, min_periods=1, points_per_year=None):
    """
    :param relative_return: daily return
    :param min_periods: minimal number of days
    :return: annualized mean return
    """
    if points_per_year is None:
        points_per_year = calc_avg_points_per_year(relative_return)
    if max_periods is None:
        max_periods = points_per_year
    power = func_np_to_xr(np.power)
    return power(calc_mean_return(relative_return, max_periods, min_periods, points_per_year=points_per_year) + 1,
                 points_per_year) - 1


def calc_bias(portfolio_history, per_asset=False):
    """
    :param per_asset:
    :param portfolio_history: portfolio weights set for every day
    :return: daily portfolio bias
    """
    if per_asset:
        return portfolio_history
    ph = portfolio_history
    sum = ph.sum(ds.ASSET)
    abs_sum = abs(ph).sum(ds.ASSET)
    res = sum / abs_sum
    res = res.where(np.isfinite(res)).fillna(0)
    return res


def calc_instruments(portfolio_history, per_asset=False):
    """
    :param per_asset:
    :param portfolio_history: portfolio weights set for every day
    :return: daily portfolio instrument count
    """
    if per_asset:
        I = portfolio_history.copy(True)
        I[:] = 1
        return I
    ph = portfolio_history.copy().fillna(0)
    ic = ph.where(ph == 0).fillna(1)
    ic = ic.cumsum(ds.TIME)
    ic = ic.where(ic == 0).fillna(1)
    ic = ic.sum(ds.ASSET)
    return ic


def calc_avg_turnover(portfolio_history, equity, data, max_periods=None, min_periods=1, per_asset=False,
                      points_per_year=None):
    '''
    Calculates average capital turnover, all args must be adjusted
    :param portfolio_history: history of portfolio changes
    :param equity: equity of changes
    :param data:
    :param max_periods:
    :param min_periods:
    :param per_asset:
    :return:
    '''
    if points_per_year is None:
        points_per_year = calc_avg_points_per_year(portfolio_history)
    if max_periods is None:
        max_periods = points_per_year

    W = portfolio_history.transpose(ds.TIME, ds.ASSET)
    W = W.shift({ds.TIME: 1})
    W[0] = 0

    Wp = W.shift({ds.TIME: 1})
    Wp[0] = 0

    OPEN = data.transpose(ds.TIME, ds.FIELD, ds.ASSET).loc[W.coords[ds.TIME], f.OPEN, W.coords[ds.ASSET]]
    OPENp = OPEN.shift({ds.TIME: 1})
    OPENp[0] = OPEN[0]

    E = equity

    Ep = E.shift({ds.TIME: 1})
    Ep[0] = 1

    turnover = abs(W - Wp * Ep * OPEN / (OPENp * E))
    if not per_asset:
        turnover = turnover.sum(ds.ASSET)
    max_periods = min(max_periods, len(turnover.coords[ds.TIME]))
    min_periods = min(min_periods, len(turnover.coords[ds.TIME]))
    turnover = turnover.rolling({ds.TIME: max_periods}, min_periods=min_periods).mean()
    try:
        turnover = turnover.drop(ds.FIELD)
    except ValueError:
        pass
    return turnover


def calc_avg_holding_time(portfolio_history,  # equity, data,
                          max_periods=None, min_periods=1, per_asset=False, points_per_year=None):
    '''
    Calculates holding time.
    :param portfolio_history:
    :param max_periods:
    :param min_periods:
    :param per_asset:
    :param points_per_year:
    :return:
    '''
    if points_per_year is None:
        points_per_year = calc_avg_points_per_year(portfolio_history)
    if max_periods is None:
        max_periods = points_per_year

    ph = portfolio_history.copy(True)

    try:
        ph[-2] = 0  # avoids NaN for buy-and-hold
    except:
        pass

    log = calc_holding_log_np_nb(ph.values)  # , equity.values, data.sel(field='open').values)

    log = xr.DataArray(log, dims=[ds.TIME, ds.FIELD, ds.ASSET], coords={
        ds.TIME: portfolio_history.time,
        ds.FIELD: ['cost', 'duration'],
        ds.ASSET: portfolio_history.asset
    })

    if not per_asset:
        log2d = log.isel(asset=0).copy(True)
        log2d.loc[{ds.FIELD: 'cost'}] = log.sel(field='cost').sum(ds.ASSET)
        log2d.loc[{ds.FIELD: 'duration'}] = (log.sel(field='cost') * log.sel(field='duration')).sum(ds.ASSET) / \
                                            log2d.sel(field='cost')
        log = log2d

        try:
            log = log.drop(ds.ASSET)
        except ValueError:
            pass

    max_periods = min(max_periods, len(log.coords[ds.TIME]))
    min_periods = min(min_periods, len(log.coords[ds.TIME]))

    res = (log.sel(field='cost') * log.sel(field='duration')) \
              .rolling({ds.TIME: max_periods}, min_periods=min_periods).sum() / \
          log.sel(field='cost') \
              .rolling({ds.TIME: max_periods}, min_periods=min_periods).sum()

    try:
        res = res.drop(ds.FIELD)
    except ValueError:
        pass

    points_per_day = calc_points_per_day(points_per_year)

    return res / points_per_day

@numba.jit
def calc_holding_log_np_nb(weights: np.ndarray) -> np.ndarray:  # , equity: np.ndarray, open: np.ndarray) -> np.ndarray:
    prev_pos = np.zeros(weights.shape[1])
    holding_time = np.zeros(weights.shape[1])  # position holding time
    holding_log = np.zeros(weights.shape[0] * 2 * weights.shape[1])  # time, field (position_cost, holding_time), asset
    holding_log = holding_log.reshape(weights.shape[0], 2, weights.shape[1])

    for t in range(1, weights.shape[0]):
        holding_time[:] += 1
        for a in range(weights.shape[1]):
            # price = open[t][a]
            # if not np.isfinite(price):
            #     continue
            pos = weights[t - 1][a]  # * equity[t] / price
            ppos = prev_pos[a]
            if not np.isfinite(pos):
                continue
            dpos = pos - ppos
            if abs(dpos) < EPS:
                continue
            if ppos > 0 > dpos or ppos < 0 < dpos:  # opposite change direction
                if abs(dpos) > abs(ppos):
                    holding_log[t][0][a] = abs(ppos)  # * price
                    holding_log[t][1][a] = holding_time[a]
                    holding_time[a] = 0
                else:
                    holding_log[t][0][a] = abs(dpos)  # * price
                    holding_log[t][1][a] = holding_time[a]
            elif pos != 0:
                holding_time[a] = holding_time[a] * abs(ppos) / abs(pos)
            prev_pos[a] = pos
    return holding_log


def calc_non_liquid(data, portfolio_history):
    (adj_data, adj_ph, ignored) = arrange_data(data, portfolio_history, None)
    if f.IS_LIQUID in list(adj_data.coords[ds.FIELD]):
        non_liquid = adj_ph.where(
            np.logical_and(np.isfinite(adj_data.loc[f.IS_LIQUID]), adj_data.loc[f.IS_LIQUID] == 0))
        non_liquid = non_liquid.dropna(ds.ASSET, 'all')
        non_liquid = non_liquid.dropna(ds.TIME, 'all')
        if abs(non_liquid).sum() > 0:
            return non_liquid
    return None


def find_missed_dates(output, data):
    out_ts = np.sort(output.coords[ds.TIME].values)

    min_out_ts = min(out_ts)

    data_ts = data.coords[ds.TIME]
    data_ts = data_ts.where(data_ts >= min_out_ts).dropna(ds.TIME)
    data_ts = np.sort(data_ts.values)
    return np.array(np.setdiff1d(data_ts, out_ts))


def calc_avg_points_per_year(data: xr.DataArray):
    t = np.sort(data.coords[ds.TIME].values)
    tp = np.roll(t, 1)
    dh = (t[1:] - tp[1:]).mean().item() / (10 ** 9) / 60 / 60  # avg diff in hours
    return round(365.25 * 24 / dh)


def calc_points_per_day(days_per_year):
    if days_per_year < 400:
        return 1
    else:
        return 24


def func_np_to_xr(origin_func):
    '''
    Decorates numpy function for xarray
    '''
    func = xr.ufuncs._UFuncDispatcher(origin_func.__name__)
    func.__name__ = origin_func.__name__
    doc = origin_func.__doc__
    func.__doc__ = ('xarray specific variant of numpy.%s. Handles '
                    'xarray.Dataset, xarray.DataArray, xarray.Variable, '
                    'numpy.ndarray and dask.array.Array objects with '
                    'automatic dispatching.\n\n'
                    'Documentation from numpy:\n\n%s' % (origin_func.__name__, doc))
    return func


class StatFields:
    RELATIVE_RETURN = "relative_return"
    EQUITY = "equity"
    VOLATILITY = "volatility"
    UNDERWATER = "underwater"
    MAX_DRAWDOWN = "max_drawdown"
    SHARPE_RATIO = "sharpe_ratio"
    MEAN_RETURN = "mean_return"
    BIAS = "bias"
    INSTRUMENTS = "instruments"
    AVG_TURNOVER = "avg_turnover"
    AVG_HOLDINGTIME = 'avg_holding_time'


stf = StatFields


def calc_stat(data, portfolio_history, slippage_factor=0.05, roll_slippage_factor=0.02,
              min_periods=1, max_periods=None,
              per_asset=False, points_per_year=None):
    """
    :param data: xarray with historical data, data must be split adjusted
    :param portfolio_history: portfolio weights set for every day
    :param slippage_factor:
    :param min_periods: minimal number of days
    :param max_periods: max number of days for rolling
    :param per_asset: calculate stats per asset
    :return: xarray with all statistics
    """
    if points_per_year is None:
        points_per_year = calc_avg_points_per_year(data)
    if max_periods is None:
        max_periods = (points_per_year * 3) if points_per_year == 252 else (points_per_year * 7)

    portfolio_history = sort_and_crop_output(portfolio_history, per_asset)
    if f.IS_LIQUID in data.coords[ds.FIELD]:
        non_liquid = calc_non_liquid(data, portfolio_history)
        if non_liquid is not None:
            print("WARNING: Strategy trades non-liquid assets.")

    missed_dates = find_missed_dates(portfolio_history, data)
    if len(missed_dates) > 0:
        print("WARNING: some dates are missed in the portfolio_history")

    RR = calc_relative_return(data, portfolio_history, slippage_factor, roll_slippage_factor, per_asset, points_per_year)

    E = calc_equity(RR)
    V = calc_volatility_annualized(RR, max_periods=max_periods, min_periods=min_periods,
                                   points_per_year=points_per_year)
    U = calc_underwater(E)
    DD = calc_max_drawdown(U)
    SR = calc_sharpe_ratio_annualized(RR, max_periods=max_periods, min_periods=min_periods,
                                      points_per_year=points_per_year)
    MR = calc_mean_return_annualized(RR, max_periods=max_periods, min_periods=min_periods,
                                     points_per_year=points_per_year)
    (adj_data, adj_ph, ignored) = arrange_data(data, portfolio_history, E, per_asset)
    B = calc_bias(adj_ph, per_asset)
    I = calc_instruments(adj_ph, per_asset)
    T = calc_avg_turnover(adj_ph, E, adj_data, min_periods=min_periods, max_periods=max_periods, per_asset=per_asset,
                          points_per_year=points_per_year)

    HT = calc_avg_holding_time(adj_ph,  # E, adj_data,
                               min_periods=min_periods, max_periods=max_periods,
                               per_asset=per_asset,
                               points_per_year=points_per_year)

    stat = xr.concat([
        E, RR, V,
        U, DD, SR,
        MR, B, I, T, HT
    ], pd.Index([
        stf.EQUITY, stf.RELATIVE_RETURN, stf.VOLATILITY,
        stf.UNDERWATER, stf.MAX_DRAWDOWN, stf.SHARPE_RATIO,
        stf.MEAN_RETURN, stf.BIAS, stf.INSTRUMENTS, stf.AVG_TURNOVER, stf.AVG_HOLDINGTIME
    ], name=ds.FIELD))

    dims = [ds.TIME, ds.FIELD]
    if per_asset:
        dims.append(ds.ASSET)
    return stat.transpose(*dims)


def calc_sector_distribution(portfolio_history, timeseries=None):
    """
    :param portfolio_history: portfolio weights set for every day
    :param timeseries: time range
    :return: sector distribution
    """
    ph = abs(portfolio_history.transpose(ds.TIME, ds.ASSET)).fillna(0)
    s = ph.sum(ds.ASSET)
    s[s < 1] = 1
    ph = ph / s

    if timeseries is not None:  # arrange portfolio to timeseries
        _ph = xr.DataArray(np.full([len(timeseries), len(ph.coords[ds.ASSET])], 0, dtype=np.float64),
                           dims=[ds.TIME, ds.ASSET],
                           coords={
                               ds.TIME: timeseries,
                               ds.ASSET: ph.coords[ds.ASSET]
                           })
        intersection = np.intersect1d(timeseries, ph.coords[ds.TIME], True)
        _ph.loc[intersection] = ph.loc[intersection]
        ph = _ph.ffill(ds.TIME).fillna(0)

    max_date = str(portfolio_history.coords[ds.TIME].max().values)[0:10]
    min_date = str(portfolio_history.coords[ds.TIME].min().values)[0:10]

    assets = load_assets(min_date=min_date, max_date=max_date)
    assets = dict((a['id'], a) for a in assets)

    sectors = []

    SECTOR_FIELD = 'sector'

    for aid in portfolio_history.coords[ds.ASSET].values:
        sector = "Other"
        if aid in assets:
            asset = assets[aid]
            s = asset[SECTOR_FIELD]
            if s is not None and s != 'n/a' and s != '':
                sector = s
        sectors.append(sector)

    uq_sectors = sorted(list(set(sectors)))
    sectors = np.array(sectors)

    CASH_SECTOR = 'Cash'
    sector_distr = xr.DataArray(
        np.full([len(ph.coords[ds.TIME]), len(uq_sectors) + 1], 0, dtype=np.float64),
        dims=[ds.TIME, SECTOR_FIELD],
        coords={
            ds.TIME: ph.coords[ds.TIME],
            SECTOR_FIELD: uq_sectors + [CASH_SECTOR]
        }
    )

    for sector in uq_sectors:
        sum_by_sector = ph.loc[:, sectors == sector].sum(ds.ASSET)
        sector_distr.loc[:, sector] = sum_by_sector

    sector_distr.loc[:, CASH_SECTOR] = 1 - ph.sum(ds.ASSET)

    return sector_distr


def print_correlation(portfolio_history, data):
    """ Checks correlation for current output. """
    portfolio_history = sort_and_crop_output(portfolio_history)
    rr = calc_relative_return(data, portfolio_history)

    cr_list = calc_correlation(rr)

    print()

    if len(cr_list) == 0:
        print("Ok. This strategy does not correlate with other strategies.")
        return

    print("WARNING! This strategy correlates with other strategies.")
    print("The number of systems with a larger Sharpe ratio and correlation larger than 0.8:", len(cr_list))
    print("The max correlation value (with systems with a larger Sharpe ratio):", max([i['cofactor'] for i in cr_list]))
    my_cr = [i for i in cr_list if i['my']]

    print("Current sharpe ratio(3y):",
          calc_sharpe_ratio_annualized(rr, calc_avg_points_per_year(data) * 3)[-1].values.item())

    print()

    if len(my_cr) > 0:
        print("My correlated submissions:\n")
        headers = ['Name', "Coefficient", "Sharpe ratio"]
        rows = []

        for i in my_cr:
            rows.append([i['name'], i['cofactor'], i['sharpe_ratio']])

        print(tabulate(rows, headers))


def calc_correlation(relative_returns):
    try:

        ENGINE_CORRELATION_URL = get_env("ENGINE_CORRELATION_URL",
                                         "http://localhost:8080/referee/submission/forCorrelation")
        STATAN_CORRELATION_URL = get_env("STATAN_CORRELATION_URL", "http://localhost:8081/statan/correlation")
        PARTICIPANT_ID = get_env("PARTICIPANT_ID", "0")

        with request.urlopen(ENGINE_CORRELATION_URL + "?participantId=" + PARTICIPANT_ID) as response:
            submissions = response.read()
            submissions = json.loads(submissions)
            submission_ids = [s['id'] for s in submissions]

        rr = relative_returns.to_netcdf(compute=True)
        rr = gzip.compress(rr)
        rr = base64.b64encode(rr)
        rr = rr.decode()

        r = {"relative_returns": rr, "submission_ids": submission_ids}
        r = json.dumps(r)
        r = r.encode()

        with request.urlopen(STATAN_CORRELATION_URL, r) as response:
            cofactors = response.read()
            cofactors = json.loads(cofactors)

        result = []
        for c in cofactors:
            sub = next((s for s in submissions if str(c['id']) == str(s['id'])))
            sub['cofactor'] = c['cofactor']
            sub['sharpe_ratio'] = c['sharpe_ratio']
            result.append(sub)

        return result
    except:
        import logging
        logging.exception("network error")
        return []


def check_exposure(portfolio_history,
                   soft_limit=0.05, hard_limit=0.1,
                   days_tolerance=0.02, excess_tolerance=0.02,
                   avg_period=252, check_period=252*3
                   ):
    """
    Checks exposure according to the submission filters.
    :param portfolio_history: output DataArray
    :param soft_limit: soft limit for exposure
    :param hard_limit: hard limit for exposure
    :param days_tolerance: the number of days when exposure may be in range 0.05..0.1
    :param excess_tolerance: max allowed average excess
    :param avg_period: period for the ratio calculation
    :param check_period: period for checking
    :return:
    """
    portfolio_history = portfolio_history.loc[{ds.TIME:np.sort(portfolio_history.coords[ds.TIME])}]

    exposure = calc_exposure(portfolio_history)
    max_exposure = exposure.max(ds.ASSET)

    max_exposure_over_limit = max_exposure.where(max_exposure > soft_limit).dropna(ds.TIME)
    if len(max_exposure_over_limit) > 0:
        max_exposure_asset = exposure.sel({ds.TIME: max_exposure_over_limit.coords[ds.TIME]}).idxmax(ds.ASSET)
        print("Positions with max exposure over the limit:")
        pos = xr.concat([max_exposure_over_limit, max_exposure_asset], pd.Index(['exposure', 'asset'], name='field'))
        print(pos.to_pandas().T)

    periods = min(avg_period, len(portfolio_history.coords[ds.TIME]))

    bad_days = xr.where(max_exposure > soft_limit, 1.0, 0.0)
    bad_days_proportion = bad_days[-check_period:].rolling(dim={ds.TIME: periods}).mean()
    days_ok = xr.where(bad_days_proportion > days_tolerance, 1, 0).sum().values == 0

    excess = exposure - soft_limit
    excess = excess.where(excess > 0, 0).sum(ds.ASSET)
    excess = excess[-check_period:].rolling(dim={ds.TIME: periods}).mean()
    excess_ok = xr.where(excess > excess_tolerance, 1, 0).sum().values == 0

    hard_limit_ok = xr.where(max_exposure > hard_limit, 1, 0).sum().values == 0

    if hard_limit_ok and (days_ok or excess_ok):
        print("Ok. The exposure check succeed.")
        return True
    else:
        print("WARNING! The exposure check failed.")
        print("Hard limit check: ", hard_limit_ok)
        print("Days check: ", days_ok)
        print("Excess check:", excess_ok)
        return False


def calc_exposure(portfolio_history):
    """
    Calculates exposure per position (range: 0..1)
    :param portfolio_history:
    :return:
    """
    sum = abs(portfolio_history).sum(ds.ASSET)
    sum = sum.where(sum > EPS, 1) # prevents div by zero
    return abs(portfolio_history) / sum

