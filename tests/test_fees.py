"""`enoshima.json`'s `fee_rates`: the only place a backtest's fee rate can come from.

A backtest installs no venue package, so before this every fill was costed at
zero. These hold that a traded market must have a rate, that the rate reaches
the fills, and that stating a zero rate keeps the folder of a run made without one.
"""

import json
from dataclasses import dataclass

import pytest
from qate.core.model import ExchangeName, Market
from qate.core.symbol import Symbol
from qate.exchange import FeeSchedule, factory
from test_run_end_to_end import FEES, MARKET, run_dirs, run_env  # noqa: F401 -- run_env is a fixture

from enoshima import fees, run_dir
from enoshima.runner import Runner
from enoshima.settings import SETTINGS_FILE, load_settings

GMO = Market(ExchangeName.GMO, Symbol.XRP_JPY)
BINANCE = Market(ExchangeName.BINANCE, Symbol.XRP_USDT)


@dataclass
class TwoVenueConfig:
    """Trades on GMO, reads a reference price from Binance -- Orion's shape."""

    def get_exchanges(self):
        return [ExchangeName.GMO]

    def get_markets(self):
        return [(GMO, ["MARKET_ORDER_BOOK"]), (BINANCE, ["MARKET_TRADE"])]


def test_the_file_states_rates_by_market_id(tmp_path):
    (tmp_path / SETTINGS_FILE).write_text(json.dumps({"fee_rates": {"GMO:XRP_JPY": {"maker": -0.0001, "taker": 0}}}))
    assert load_settings(tmp_path).fee_rates == {"GMO:XRP_JPY": FeeSchedule(maker=-0.0001, taker=0.0)}


@pytest.mark.parametrize(
    "rates",
    [
        {"GMO_XRP_JPY": {"maker": 0, "taker": 0}},  # not a market id
        {"NOWHERE:XRP_JPY": {"maker": 0, "taker": 0}},  # not an exchange
        {"GMO:XRP_JPY": {"maker": 0}},  # half a rate
        {"GMO:XRP_JPY": {"maker": 0, "taker": 0, "fee": 0}},  # a misspelt key
        {"GMO:XRP_JPY": {"maker": "0", "taker": 0}},  # not a number
        ["GMO:XRP_JPY"],
    ],
)
def test_a_malformed_rate_is_refused(tmp_path, rates):
    (tmp_path / SETTINGS_FILE).write_text(json.dumps({"fee_rates": rates}))
    with pytest.raises(ValueError, match="fee_rates"):
        load_settings(tmp_path)


def test_a_traded_market_without_a_rate_is_refused_and_a_reference_one_needs_none():
    with pytest.raises(ValueError, match="No fee rate for GMO:XRP_JPY"):
        fees.check({}, TwoVenueConfig())
    with pytest.raises(ValueError, match="No fee rate for GMO:XRP_JPY"):
        fees.check({BINANCE.id: FeeSchedule(0.0, 0.0)}, TwoVenueConfig())
    zero = FeeSchedule(0.0, 0.0)
    assert fees.check({GMO.id: zero, BINANCE.id: zero}, TwoVenueConfig()) == {GMO.id: zero}


def test_registered_rates_are_what_qate_costs_a_fill_with():
    fees.register({GMO.id: FeeSchedule(maker=-0.0001, taker=0.0005)})
    assert factory.get_fee_rate(GMO) == FeeSchedule(maker=-0.0001, taker=0.0005)
    assert factory.get_fee_rate(Market(ExchangeName.GMO, Symbol.BTC_JPY)) is None


def test_a_run_is_refused_before_it_replays_when_its_market_has_no_rate(run_env):
    env, dt_range = run_env
    with pytest.raises(ValueError, match=f"No fee rate for {MARKET.id}"):
        Runner(env, dt_range, fee_rates={}, chunk_days=1)
    assert run_dirs(env) == []


def test_a_charged_rate_reaches_the_trades_and_is_a_run_of_its_own(run_env):
    env, dt_range = run_env
    Runner(env, dt_range, fee_rates=FEES, chunk_days=1).run()
    [free] = run_dirs(env)
    Runner(env, dt_range, fee_rates={MARKET.id: FeeSchedule(maker=0.0, taker=0.001)}, chunk_days=1).run()
    [charged] = [p for p in run_dirs(env) if p != free]

    assert all(t.fee == 0 for t in run_dir.read_trades(free))
    assert all(t.fee > 0 for t in run_dir.read_trades(charged))
    meta = run_dir.read_json(charged, run_dir.META)
    assert meta["fee_rates"] == {MARKET.id: {"maker": 0.0, "taker": 0.001}}


def test_a_zero_rate_keeps_the_id_of_a_run_made_without_rates():
    from enoshima.run_log import BacktestLog

    class Env:
        name = "E"

    def run_id(rates):
        log = BacktestLog.__new__(BacktestLog)
        log.env, log.strategy_name, log.strategy_rev, log.params = Env(), "s.v1", "abc", {"a": 1}
        log.week_key, log.fee_rates = None, rates
        log.dt_range = type("R", (), {"start_str": "x", "end_str": "y"})()
        return log.get_id()

    assert run_id({}) == run_id({GMO.id: FeeSchedule(0.0, 0.0)})
    assert run_id({}) != run_id({GMO.id: FeeSchedule(0.0, 0.0003)})
