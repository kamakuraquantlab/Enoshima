"""`enoshima web`: what the viewer lists and serves from the data lake, and what it refuses."""

import json
from datetime import datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("flask")

from enoshima import lake, web  # noqa: E402
from enoshima.lake import WHOLE, Store  # noqa: E402
from enoshima.ledger import Trade  # noqa: E402


@pytest.fixture(autouse=True)
def _viewer(monkeypatch, tmp_path):
    # serve() sets these from an env; put them back after every test.
    monkeypatch.setattr(web, "LAKE_ROOT", tmp_path)
    monkeypatch.setattr(web, "GRAFANA_DASHBOARDS", [])


def make_run(root, env, run_id, meta, summary, trades=(), records=None):
    store = Store(root, env)
    store.write_part(run_id, WHOLE, meta, summary, list(trades), records)
    store.write_run(run_id, meta, summary, list(trades))
    return store


def test_layouts_and_schema(tmp_path):
    make_run(tmp_path, "NEW", "aaa", {"start_ts": 20, "param_set_id": "NEW_1"},
             {"pnl5": 5, "sharpe_ratio_annualized5": float("inf")})
    make_run(tmp_path, "OLD", "bbb", {"start_ts": 10}, {"pnl5": -1})
    # Not runs: a run with parts but no row (a weekly run with a gap), one set aside by a merge.
    store = Store(tmp_path, "NEW")
    store.write_part("ccc", "2026-W02", {"start_ts": 1}, {}, [])
    make_run(tmp_path, "NEW", "ddd.merged", {"start_ts": 1}, {})

    client = web.app.test_client()
    assert client.get("/api/envs").json == ["NEW", "OLD"]
    rows = client.get("/api/results").json
    assert [r["run_id"] for r in rows] == ["aaa", "bbb"]
    assert [r["param_set_id"] if "param_set_id" in r else None for r in rows] == ["NEW_1", None]
    assert rows[0]["result"]["sharpe_ratio_annualized5"] is None
    assert client.get("/api/results?env=OLD").json[0]["source"] == "OLD"
    assert client.get("/api/results?env=absent").json == []
    assert client.post("/api/backtest/NEW/execute", json={}).status_code == 404


def test_run_detail_and_files(tmp_path):
    trade = Trade(1.0, 0.5, "GMO", "BTC_JPY", "long", 1.0, 1.0, 2.0, 0.0, 2.0, 2.0, 2.0, 100.0)
    orders = lake.table_of({"ts": [0.6, 1.0], "SIDE": ["BUY", "SELL"], "EXEC_PRICE": [99.0, 100.0]})
    make_run(tmp_path, "ENV", "aaa", {"params": {"x": 1}}, {"pnl5": 2.0}, [trade], {lake.ORDER: orders})
    client = web.app.test_client()
    detail = client.get("/api/runs/ENV/aaa?source=ENV").json
    assert detail["meta"] == {"params": {"x": 1}} and detail["summary"] == {"pnl5": 2.0}
    assert "trades.csv" in detail["files"] and "by_hour.json" in detail["files"] and "orders.csv" in detail["files"]
    assert "bars.csv" not in detail["files"], "a dataset the run has no rows of is not offered"
    assert client.get("/api/runs/ENV/aaa/files/by_hour.json").json["09"]["trades"] == 1   # 1970-01-01 09:00 JST
    csv = client.get("/api/runs/ENV/aaa/files/trades.csv")
    assert csv.status_code == 200 and csv.data.startswith(b"close_ts,open_ts")
    assert csv.data.decode().splitlines()[1].startswith("1.0,0.5,GMO,BTC_JPY,long")
    assert client.get("/api/runs/ENV/aaa/files/orders.csv").data.decode().splitlines() == [
        "ts,SIDE,EXEC_PRICE,part", "0.6,BUY,99.0,all", "1.0,SELL,100.0,all"]
    assert client.get("/api/runs/ENV/aaa/files/bars.csv").data == b""
    assert client.get("/api/runs/ENV/aaa/files/secret.txt").status_code == 400
    assert client.get("/api/runs/ENV/zzz").status_code == 404
    assert client.get("/api/runs/ENV/..").status_code in (400, 404)
    assert client.get("/api/runs/NOWHERE/aaa").status_code == 404


def test_results_over_a_date_range_are_summarized_from_the_trades_closed_in_it(tmp_path):
    def at(day, hour):  # epoch seconds of a JST time
        return datetime.fromisoformat(f"{day}T{hour:02d}:00:00+09:00").timestamp()

    def trade(close, change):
        return Trade(close, close - 60, "GMO", "BTC_JPY", "long", 1.0, 1.0, change, 0.0, change, change, change)

    trades = [trade(at("2026-01-05", 8), 1.0), trade(at("2026-01-06", 23), -2.0), trade(at("2026-01-07", 0), 4.0)]
    meta = {"start_ts": 1, "start_str": "2026-01-05 00:00:00", "end_str": "2026-01-07 23:59:59"}
    # Two fills on the 6th, each 100 notional: 1.0 and 3.0 of slippage; one on the 5th.
    orders = lake.table_of({"ts": [at("2026-01-05", 7), at("2026-01-06", 22), at("2026-01-06", 23)],
                            "SLIPPAGE": [9.0, 1.0, 3.0], "EXEC_PRICE": [100.0] * 3, "EXEC_SIZE": [1.0] * 3})
    make_run(tmp_path, "ENV", "aaa", meta, {"pnl5": 3.0, "slippage5": 0.1}, trades, {lake.ORDER: orders})
    client = web.app.test_client()

    whole = client.get("/api/results").json[0]
    assert whole["result"] == {"pnl5": 3.0, "slippage5": 0.1} and "range_covered" not in whole
    one_day = client.get("/api/results?start=2026-01-06&end=2026-01-06").json[0]["result"]
    assert one_day["pnl5"] == -2.0 and one_day == pytest.approx({**web.summarize(trades[1:2], {}), **{
        f"slippage{s}": 2.0 + 100 * rate for s, rate in web.RATES.items()}, **{
        f"slippage_rate{s}": 0.02 + rate for s, rate in web.RATES.items()}}), "the fills on the day, as Evaluation averages them"
    # A run imported from before the lake recorded no fills: its slippage is unknown, not zero.
    make_run(tmp_path, "OLD", "bbb", meta, {}, trades)
    assert client.get("/api/results?env=OLD&start=2026-01-06").json[0]["result"]["slippage5"] is None
    rows = client.get("/api/results?start=2026-01-06").json
    assert rows[0]["result"]["pnl_cnt5"] == 2 and rows[0]["range_covered"]
    assert not client.get("/api/results?start=2026-01-01&end=2026-01-06").json[0]["range_covered"]
    assert client.get("/api/results?start=2026-02-01").json[0]["result"]["pnl_cnt5"] == 0
    assert client.get("/api/results?start=2026-01-07&end=2026-01-06").status_code == 400
    assert client.get("/api/results?start=yesterday").status_code == 400


def test_a_run_links_to_its_strategy_s_dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "GRAFANA_DASHBOARDS", web._dashboards({"dashboards": [{
        "match": {"trading.module_name": "example.strategy"},
        "url": "/grafana/d/example",
        "query": {"var-run": "{id}", "var-symbol": "{symbol}", "var-CRYPTO": "{base}",
                  "from": "{start_ms}", "to": "{end_ms}"},
    }]}))
    example = {"trading": {"module_name": "example.strategy"}, "config": {"market": "COINCHECK:BTC_SPOT"},
               "start_str": "2026-01-01 00:00:00", "end_str": "2026-01-01 23:59:59"}
    make_run(tmp_path, "ENV", "aaa", {**example, "start_ts": 3, "id": "aaa"}, {})
    # No id, so no value for {id}; another strategy matches nothing.
    make_run(tmp_path, "ENV", "bbb", {**example, "start_ts": 2}, {})
    make_run(tmp_path, "ENV", "ccc", {**example, "start_ts": 1, "id": "ccc",
                                      "trading": {"module_name": "other.strategy"}}, {})
    client = web.app.test_client()
    links = {r["run_id"]: r["grafana_url"] for r in client.get("/api/results").json}
    assert links == {
        "aaa": "/grafana/d/example?var-run=aaa&var-symbol=BTC_SPOT&var-CRYPTO=BTC"
               "&from=1767193200000&to=1767279599000",
        "bbb": None, "ccc": None,
    }
    assert client.get("/api/runs/ENV/aaa").json["grafana_url"] == links["aaa"]
    assert "grafana_url" not in client.get("/api/runs/ENV/aaa").json["meta"]
    monkeypatch.setattr(web, "GRAFANA_DASHBOARDS", [])
    assert client.get("/api/runs/ENV/aaa").json["grafana_url"] is None


def test_a_malformed_dashboard_is_refused():
    assert web._dashboards(None) == []
    for dashboard in ({"match": {}}, {"url": "/d", "query": []}):
        try:
            web._dashboards({"dashboards": [dashboard]})
        except ValueError:
            continue
        raise AssertionError(dashboard)


def test_delete_a_run_and_nothing_else(tmp_path):
    store = make_run(tmp_path, "ENV", "aaa", {}, {}, records={lake.ORDER: lake.table_of({"ts": [1.0]})})
    other = make_run(tmp_path, "ENV", "bbb", {}, {})
    elsewhere = make_run(tmp_path, "OTHER", "aaa", {}, {})
    client = web.app.test_client()
    assert client.delete("/api/results/ENV/partial.tmp-1").status_code == 400
    assert client.delete("/api/results/ENV/zzz").status_code == 404
    assert client.delete("/api/results/ENV/aaa?source=ENV").status_code == 204
    assert not any(store.run_dir(d, "aaa").exists() for d in lake.DATASETS)
    assert other.run_ids() == ["bbb"] and elsewhere.run_ids() == ["aaa"]


def test_prices_are_the_last_trade_of_each_step(monkeypatch):
    from hase import bronze

    day = 1_790_000_000 // 86400 * 86400 - 9 * 3600  # a JST midnight
    traded = datetime.fromtimestamp(day, web.JST).date()
    minutes = day + np.arange(0, 3 * 3600, 60)
    monkeypatch.setattr(bronze, "dates", lambda market, data_type: [str(traded - timedelta(days=1)), str(traded)])
    monkeypatch.setattr(web, "_minute_prices", lambda market, d: (
        (minutes, minutes - day) if d == str(traded) else (np.empty(0), np.empty(0))))
    client = web.app.test_client()

    # Two hours fit in 5,000 minutes: every minute in range comes back.
    body = client.get(f"/api/prices?market=COINCHECK:BTC_SPOT&start={day}&end={day + 7200}").get_json()
    assert body["step"] == 60 and len(body["prices"]) == 120
    # A year does not: four-hour steps, each the last minute's price at its own time.
    body = client.get(f"/api/prices?market=COINCHECK:BTC_SPOT&start={day - 364 * 86400}&end={day + 86400}").get_json()
    assert body["step"] == 4 * 3600
    assert body["prices"] == [[(day + 3 * 3600 - 60) * 1000, 3 * 3600 - 60]]

    assert client.get("/api/prices?market=NOWHERE:X&start=1&end=2").status_code == 400
    assert client.get(f"/api/prices?market=COINCHECK:BTC_SPOT&start={day}&end={day}").status_code == 400


def test_setup_and_serve_read_the_viewer_s_env(monkeypatch, tmp_path, data_root):
    from qate_env.env import Env

    from enoshima import cli

    monkeypatch.setenv("QATE_ENV_ROOT", str(tmp_path))
    assert cli.main(["web", "--setup"]) == 0
    env_dir = tmp_path / "ENOSHIMA_API"
    assert json.loads((env_dir / "config.json").read_text()) == {"port": 9768}
    assert json.loads((env_dir / "grafana_dashboard.json").read_text()) == {"dashboards": []}

    ran = []
    monkeypatch.setattr(web.app, "run", lambda **kwargs: ran.append(kwargs["port"]))
    # A config from before the lake still serves: its results_dirs are no longer asked.
    (env_dir / "config.json").write_text('{"port": 1234, "results_dirs": [".", "backtest"]}')
    web.serve(Env(str(tmp_path), web.ENV_NAME))
    assert ran == [1234] and web.LAKE_ROOT == data_root
