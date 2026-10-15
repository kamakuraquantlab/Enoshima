"""A point's timestamp must not depend on what else is being written, or a re-run doubles it."""

from influxdb_client import Point

from enoshima.influx import TimestampSpreader


def eval_point(ts_ms: int, field: str, value: float) -> Point:
    return Point("EVAL").tag("SYMBOL", "XRP_JPY").field(field, value).time(ts_ms)


def bar_point(ts_ms: int) -> Point:
    return Point("BAR").tag("SYMBOL", "XRP_JPY").field("close", 1.0).time(ts_ms)


def stamps(points):
    return [(p._name, tuple(p._fields), p._time) for p in points]


def test_ties_within_one_series_are_spread():
    spread = TimestampSpreader()
    points = [eval_point(1000, "pnl_change5", 1.0), eval_point(1000, "pnl_change5", 2.0)]
    spread(points)
    assert [p._time for p in points] == [1_000_000, 1_000_001]


def test_other_series_and_fields_at_the_same_time_are_left_alone():
    """InfluxDB keeps different fields and series apart; spreading them only made offsets fragile."""
    spread = TimestampSpreader()
    points = [eval_point(1000, "pnl_change0", 1.0), eval_point(1000, "pnl_change5", 1.0), bar_point(1000)]
    spread(points)
    assert [p._time for p in points] == [1_000_000] * 3


def test_offsets_do_not_depend_on_neighbours_or_flushes():
    """The same trades, written with other series interleaved and split across flushes differently."""
    trades = [(1000, 1.0), (1000, 2.0), (1000, 3.0), (2000, 4.0)]

    alone = [eval_point(t, "pnl_change5", v) for t, v in trades]
    TimestampSpreader()(alone)

    mixed_spread = TimestampSpreader()
    first = [bar_point(1000), eval_point(1000, "pnl_change5", 1.0), bar_point(1000)]
    second = [eval_point(1000, "pnl_change5", 2.0), bar_point(2000)]
    third = [eval_point(1000, "pnl_change5", 3.0), eval_point(2000, "pnl_change5", 4.0)]
    for flush in (first, second, third):
        mixed_spread(flush)
    mixed = [p for p in first + second + third if p._name == "EVAL"]

    assert [p._time for p in mixed] == [p._time for p in alone] == [1_000_000, 1_000_001, 1_000_002, 2_000_000]


def test_a_re_flush_after_a_failed_write_moves_nothing():
    spread = TimestampSpreader()
    points = [eval_point(1000, "pnl_change5", 1.0), eval_point(1000, "pnl_change5", 2.0)]
    spread(points)
    later = eval_point(1000, "pnl_change5", 3.0)
    spread(points + [later])  # the buffer kept after a failed write, plus one more point
    assert [p._time for p in points + [later]] == [1_000_000, 1_000_001, 1_000_002]
