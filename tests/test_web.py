"""`enoshima web`: what the viewer lists and serves from env folders, and what it refuses."""

import json
from datetime import datetime, timedelta

import numpy as np
import pytest

pytest.importorskip("flask")

from enoshima import web  # noqa: E402


@pytest.fixture(autouse=True)
def _viewer(monkeypatch):
    # serve() sets these from an env; put them back after every test.
    monkeypatch.setattr(web, "ENV_ROOT", web.ENV_ROOT)
    monkeypatch.setattr(web, "RESULTS_DIRS", ["."])
    monkeypatch.setattr(web, "GRAFANA_DASHBOARDS", [])


def make_run(env_dir, run_id, meta, summary, extra=None):
    path = env_dir / "run_log" / run_id
    path.mkdir(parents=True)
    (path / "meta.json").write_text(json.dumps(meta))
    (path / "summary.json").write_text(json.dumps(summary))
    for name, content in (extra or {}).items():
        (path / name).write_text(content)
    return path


def test_layouts_and_schema(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "ENV_ROOT", tmp_path)
    monkeypatch.setattr(web, "RESULTS_DIRS", [".", "backtest"])
    make_run(
        tmp_path / "NEW", "aaa",
        {"start_ts": 20, "influx_bucket": "new_bucket"},
        {"pnl5": 5, "sharpe_ratio_annualized5": float("inf")},
    )
    make_run(tmp_path / "backtest/OLD", "bbb", {"start_ts": 10, "influx_bucket": None}, {"pnl5": -1})
    logs = tmp_path / "NEW/run_log"
    # Not runs: a folder still being written, one without a summary, a flat file.
    (logs / "ccc.tmp-1").mkdir()
    (logs / "ccc.tmp-1/meta.json").write_text("{}")
    (logs / "ccc.tmp-1/summary.json").write_text("{}")
    (logs / "ddd").mkdir()
    (logs / "ddd/meta.json").write_text("{}")
    (logs / "old.json").write_text('{"result": {}}')
    make_run(tmp_path / "NEW", "broken", {"start_ts": 1}, {}).joinpath("summary.json").write_text("{")

    client = web.app.test_client()
    assert client.get("/api/envs").json == ["NEW", "OLD"]
    rows = client.get("/api/results").json
    assert [r["run_id"] for r in rows] == ["aaa", "bbb"]
    assert [r["bucket_name"] for r in rows] == ["new_bucket", None]
    assert rows[0]["result"]["sharpe_ratio_annualized5"] is None
    assert client.get("/api/results?env=OLD").json[0]["source"] == "backtest/OLD"
    assert client.get("/api/results?env=absent").json == []
    assert client.post("/api/backtest/NEW/execute", json={}).status_code == 404


def test_a_recommended_run_is_marked(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "ENV_ROOT", tmp_path)
    make_run(tmp_path / "ENV", "aaa", {"start_ts": 2}, {})
    make_run(tmp_path / "ENV", "bbb", {"start_ts": 1}, {})
    for batch in ("100", "200"):
        best = tmp_path / "ENV/best_result" / batch
        best.mkdir(parents=True)
        (best / "selection.json").write_text("{}")
        (best / "aaa.json").write_text("{}")
    rows = web.app.test_client().get("/api/results").json
    assert {r["run_id"]: r["recommended_in"] for r in rows} == {"aaa": "100,200", "bbb": None}


def test_run_detail_and_files(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "ENV_ROOT", tmp_path)
    make_run(
        tmp_path / "ENV", "aaa", {"params": {"x": 1}}, {"pnl5": 2.0},
        {"trades.csv": "close_ts,pnl_change5\n1,2.0\n", "by_hour.json": '{"00": {"trades": 1}}', "secret.txt": "no"},
    )
    client = web.app.test_client()
    detail = client.get("/api/runs/ENV/aaa?source=ENV").json
    assert detail["meta"] == {"params": {"x": 1}} and detail["summary"] == {"pnl5": 2.0}
    assert detail["files"] == ["by_hour.json", "meta.json", "summary.json", "trades.csv"]
    assert client.get("/api/runs/ENV/aaa/files/by_hour.json").json == {"00": {"trades": 1}}
    csv = client.get("/api/runs/ENV/aaa/files/trades.csv")
    assert csv.status_code == 200 and csv.data.startswith(b"close_ts")
    assert client.get("/api/runs/ENV/aaa/files/secret.txt").status_code == 400
    assert client.get("/api/runs/ENV/aaa/files/by_week.json").status_code == 404
    assert client.get("/api/runs/ENV/zzz").status_code == 404
    assert client.get("/api/runs/ENV/..").status_code in (400, 404)


def test_a_run_links_to_its_strategy_s_dashboard(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "ENV_ROOT", tmp_path)
    monkeypatch.setattr(web, "GRAFANA_DASHBOARDS", web._dashboards({"dashboards": [{
        "match": {"trading.module_name": "example.strategy"},
        "url": "/grafana/d/example",
        "query": {"var-bucket": "{influx_bucket}", "var-symbol": "{symbol}", "var-CRYPTO": "{base}",
                  "from": "{start_ms}", "to": "{end_ms}"},
    }]}))
    example = {"trading": {"module_name": "example.strategy"}, "config": {"market": "COINCHECK:BTC_SPOT"},
             "start_str": "2026-01-01 00:00:00", "end_str": "2026-01-01 23:59:59"}
    make_run(tmp_path / "ENV", "aaa", {**example, "start_ts": 3, "influx_bucket": "b1"}, {})
    # No bucket, so no value for {influx_bucket}; another strategy matches nothing.
    make_run(tmp_path / "ENV", "bbb", {**example, "start_ts": 2, "influx_bucket": None}, {})
    make_run(tmp_path / "ENV", "ccc", {**example, "start_ts": 1, "influx_bucket": "b3",
                                         "trading": {"module_name": "other.strategy"}}, {})
    client = web.app.test_client()
    links = {r["run_id"]: r["grafana_url"] for r in client.get("/api/results").json}
    assert links == {
        "aaa": "/grafana/d/example?var-bucket=b1&var-symbol=BTC_SPOT&var-CRYPTO=BTC"
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


def test_delete_a_run_folder_and_nothing_else(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "ENV_ROOT", tmp_path)
    run = make_run(tmp_path / "ENV", "aaa", {}, {})
    outside = make_run(tmp_path / "OTHER", "bbb", {}, {})
    (tmp_path / "ENV/run_log/link").symlink_to(outside)
    (tmp_path / "ENV/run_log/partial.tmp-1").mkdir()
    client = web.app.test_client()
    assert client.delete("/api/results/ENV/link?source=ENV").status_code == 400
    assert client.delete("/api/results/ENV/partial.tmp-1?source=ENV").status_code == 400
    assert client.delete("/api/results/ENV/aaa?source=wrong").status_code == 404
    assert client.delete("/api/results/ENV/aaa?source=ENV").status_code == 204
    assert not run.exists()
    assert outside.exists()


def test_duplicate_environment_requires_source(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "ENV_ROOT", tmp_path)
    monkeypatch.setattr(web, "RESULTS_DIRS", [".", "backtest"])
    for location in ["ENV", "backtest/ENV"]:
        make_run(tmp_path / location, "aaa", {}, {})
    client = web.app.test_client()
    assert client.delete("/api/results/ENV/aaa").status_code == 404
    assert client.delete("/api/results/ENV/aaa?source=backtest/ENV").status_code == 204
    assert (tmp_path / "ENV/run_log/aaa").exists()


def make_training(env_dir, train_id, meta, summary, extra=None):
    path = env_dir / "post_analysis/train" / train_id
    path.mkdir(parents=True)
    (path / "meta.json").write_text(json.dumps(meta))
    (path / "summary.json").write_text(json.dumps(summary))
    for name, content in (extra or {}).items():
        (path / name).write_text(content)
    return path


def test_trainings_are_listed_and_served(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "ENV_ROOT", tmp_path)
    make_run(tmp_path / "ENV", "aaa", {}, {})
    make_training(tmp_path / "ENV", "train_1_0", {"created_ts": 1}, {"trained": {"pnl5": 1.0}})
    make_training(
        tmp_path / "ENV", "train_2_0", {"created_ts": 2, "group": {"mode": "a"}},
        {"significance": {"p_sign": float("nan")}},
        {"folds.json": "[]", "curve.csv": "ts,series,trades,pnl0,pnl5,pnl10\n0,trained,1,1,1,1\n", "x.txt": "no"},
    )
    (tmp_path / "ENV/post_analysis/train/train_3_0.tmp-9").mkdir()
    (tmp_path / "ENV/post_analysis/train/train_3_0.tmp-9/meta.json").write_text("{}")
    (tmp_path / "ENV/post_analysis/train/train_3_0.tmp-9/summary.json").write_text("{}")

    client = web.app.test_client()
    rows = client.get("/api/trainings").json
    assert [r["train_id"] for r in rows] == ["train_2_0", "train_1_0"]
    assert rows[0]["source"] == "ENV" and rows[0]["result"]["significance"]["p_sign"] is None
    assert client.get("/api/trainings?env=OTHER").json == []

    detail = client.get("/api/trainings/ENV/train_2_0?source=ENV").json
    assert detail["train_id"] == "train_2_0" and detail["meta"]["group"] == {"mode": "a"}
    assert detail["files"] == ["curve.csv", "folds.json", "meta.json", "summary.json"]
    assert client.get("/api/trainings/ENV/train_2_0/files/folds.json").json == []
    assert client.get("/api/trainings/ENV/train_2_0/files/curve.csv").data.startswith(b"ts,series")
    assert client.get("/api/trainings/ENV/train_2_0/files/x.txt").status_code == 400
    assert client.get("/api/trainings/ENV/train_1_0/files/folds.json").status_code == 404
    assert client.get("/api/trainings/ENV/train_3_0.tmp-9").status_code == 400
    assert client.get("/api/trainings/ENV/zzz").status_code == 404
    # A run is not a training, nor the reverse.
    assert client.get("/api/trainings/ENV/aaa").status_code == 404
    assert client.get("/api/runs/ENV/train_1_0").status_code == 404


def test_a_run_links_to_the_newest_training_of_its_group(monkeypatch, tmp_path):
    monkeypatch.setattr(web, "ENV_ROOT", tmp_path)
    make_run(tmp_path / "ENV", "aaa", {"start_ts": 2}, {"pnl5": 1.0})
    make_run(tmp_path / "ENV", "bbb", {"start_ts": 1}, {"pnl5": 1.0})

    def against(best_pnl5):
        rules = [{"key": "rolling_3d_4h", "label": "rolling 3d, every 4h", "from": "2026-01-04 00:00", "pnl5": 1.0},
                 {"key": "rolling_1w_1d", "label": "rolling 1w, every 1d", "from": "2026-01-08 00:00",
                  "pnl5": best_pnl5}]
        held = {"rules": {"rolling_3d_4h": {"pnl5": 3.0}, "rolling_1w_1d": {"pnl5": 2.0}}}
        return json.dumps({"best": "rolling_1w_1d", "rules": rules, "runs": {"aaa": {"params": {"x": 1}, **held}}})

    make_training(tmp_path / "ENV", "train_1_0", {"created_ts": 1}, {}, {"runs.json": against(5.0)})
    make_training(tmp_path / "ENV", "train_2_0", {"created_ts": 2}, {}, {"runs.json": against(7.0)})

    client = web.app.test_client()
    rows = {r["run_id"]: r for r in client.get("/api/results").json}
    assert rows["aaa"]["walk_forward"] == {"train_id": "train_2_0", "best_pnl5": 7.0,
                                           "best_rule": "rolling 1w, every 1d"}
    assert "walk_forward" not in rows["bbb"]

    got = client.get("/api/runs/ENV/aaa/walk-forward").json
    assert got["train_id"] == "train_2_0" and got["params"] == {"x": 1} and got["best"] == "rolling_1w_1d"
    assert [(r["key"], r["held"]["pnl5"]) for r in got["rules"]] == [("rolling_3d_4h", 3.0), ("rolling_1w_1d", 2.0)]
    assert client.get("/api/runs/ENV/bbb/walk-forward").status_code == 404
    assert client.get("/api/runs/ENV/zzz/walk-forward").status_code == 404
    assert client.get("/api/trainings/ENV/train_2_0/files/runs.json").json["best"] == "rolling_1w_1d"


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


def test_setup_and_serve_read_the_viewer_s_env(monkeypatch, tmp_path):
    from qate_env.env import Env

    from enoshima import cli

    monkeypatch.setenv("QATE_ENV_ROOT", str(tmp_path))
    assert cli.main(["web", "--setup"]) == 0
    env_dir = tmp_path / "ENOSHIMA_API"
    assert json.loads((env_dir / "config.json").read_text()) == {"port": 9768, "results_dirs": ["."]}
    assert json.loads((env_dir / "grafana_dashboard.json").read_text()) == {"dashboards": []}

    ran = []
    monkeypatch.setattr(web.app, "run", lambda **kwargs: ran.append(kwargs["port"]))
    (env_dir / "config.json").write_text('{"port": 1234, "results_dirs": [".", "backtest"]}')
    web.serve(Env(str(tmp_path), web.ENV_NAME))
    assert ran == [1234] and web.RESULTS_DIRS == [".", "backtest"] and web.ENV_ROOT == tmp_path
    (env_dir / "config.json").write_text('{"results_dirs": ["../elsewhere"]}')
    with pytest.raises(ValueError):
        web.serve(Env(str(tmp_path), web.ENV_NAME))
