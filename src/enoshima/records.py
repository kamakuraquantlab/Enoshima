"""What a run records beside its trades: fills, PnL updates and the strategy's metrics.

`RecordReporter` is a `qate.trading.reporter.Reporter` every backtest has. It
keeps what the strategy and the runtime report, column by column, and hands
the columns to `lake.Store.write_part` when the run has finished -- so the
records of a part are written with its trades, whole or not at all, and a run
that fails writes none of them. One dataset per kind (`lake`):

| recorded | dataset | columns |
|---|---|---|
| a fill (`report_order`) | `BacktestOrder` | `ts`, qate's `ORDER` tags and fields |
| a PnL update (`report_pnl`) | `BacktestPnl` | `ts`, qate's `PNL` tags and fields, `open_ts`, `notional`, `open_cost` |
| a `BAR` metric | `BacktestBar` | `ts` -- the bar's own, when it has one -- and its fields as named |
| any other metric | `BacktestMetric` | `ts`, `measurement`, its tags and fields |

Tags and field keys arrive as enum values, because the hot path packs
integers, and are written by name, enum tag values by name too (`GMO`, not
its number): a file must not need qate's enum order to be read. A row does not
say which of its columns were tags; each file does, as the JSON list under
`enoshima.tags` in its Parquet metadata (`TAGS_KEY`), which is what a loader
into a database with tags -- InfluxDB -- needs and SQL does not.

The range rules are a live log's, for a backtest:

- **Nothing from the warmup period**, which is replayed for the indicators
  but is not part of the requested range.
- **With `by_open`, a weekly run's**, a fill or PnL update is the part's if
  its position opened in it -- as `ResultReporter` counts it -- so a part's
  orders and PnL are its own trades' legs, wherever they filled, and add up
  across weeks.

A close is recorded once, at its close, with its `open_ts`: an InfluxDB copy
aligned to the open, as the live dashboards draw it, is the loader's to make.

`MARKET_PRICE` and `TRADE` metrics are dropped: they are emitted per market
event, are already in the bronze layer, and would outweigh everything else.
"""

import json

from qate.core.model import DataType, ExchangeName, Field, Measurement, Metric, SettleType, Side, Tag
from qate.core.order import OrderResponse, OrderType
from qate.core.symbol import Symbol
from qate.trading.pnl_tracker import PnlUpdate
from qate.trading.reporter import Reporter
from qate.util.dt_range import DtRange

from . import lake

# Emitted per market event, and already in the bronze layer: never recorded.
# The runtime also tells the strategy, so it need not build them at all.
UNRECORDED_MEASUREMENTS = frozenset({Measurement.MARKET_PRICE.value, Measurement.TRADE.value})

TAGS_KEY = b"enoshima.tags"

_DATASET = {
    Measurement.ORDER.value: lake.ORDER,
    Measurement.PNL.value: lake.PNL,
    Measurement.BAR.value: lake.BAR,
}

_TAG_ENUMS = {
    Tag.DATA_TYPE: DataType,
    Tag.EXCHANGE_NAME: ExchangeName,
    Tag.ORDER_TYPE: OrderType,
    Tag.SETTLE_TYPE: SettleType,
    Tag.SIDE: Side,
    Tag.SYMBOL: Symbol,
}


def _pairs(flat: list) -> list[tuple]:
    if len(flat) % 2:
        raise ValueError(f"A metric's key/value list has odd length {len(flat)}")
    return [(flat[i], flat[i + 1]) for i in range(0, len(flat), 2)]


def _tag(key, value) -> tuple[str, object]:
    if type(key) is not int:
        return str(key), value
    tag = Tag(key)
    enum = _TAG_ENUMS.get(tag)
    if enum is not None and not isinstance(value, enum):
        value = enum(value)
    return tag.name, value.name if enum is not None else value


def flatten(metric: Metric) -> tuple[dict, list[str]]:
    """A metric as one row, `ts` then its tags and fields by name, and its tags' names.

    A field named `ts` replaces the time: a `BAR` carries its own, the
    completed bar's close, and that is the time a bar is at.
    """
    row = {"ts": metric.ts}
    tags = []
    for key, value in _pairs(metric.tags):
        name, value = _tag(key, value)
        row[name] = value
        tags.append(name)
    for key, value in _pairs(metric.fields):
        row[Field(key).name if type(key) is int else str(key)] = value
    return row, tags


class Columns:
    """Rows kept column by column, a column added when first seen and padded with None."""

    def __init__(self):
        self.columns: dict[str, list] = {}
        self.tags: set[str] = set()
        self.n = 0

    def table(self):
        table = lake.table_of(self.columns)
        if table is None:
            return None
        return table.replace_schema_metadata({TAGS_KEY: json.dumps(sorted(self.tags)).encode()})

    def add(self, row: dict, tags=()) -> None:
        self.tags.update(tags)
        for name, value in row.items():
            column = self.columns.get(name)
            if column is None:
                column = self.columns[name] = [None] * self.n
            column.append(value)
        self.n += 1
        for column in self.columns.values():
            if len(column) < self.n:
                column.append(None)


class RecordReporter(Reporter):
    """A backtest's fills, PnL updates and metrics, kept for its part's files."""

    def __init__(self, dt_range: DtRange, by_open: bool = False):
        super().__init__()
        # Its own floats, as `ResultReporter`'s: a shared `DtRange` asked per metric is contended.
        self._start_ts = dt_range.start_ts
        self._end_ts = dt_range.end_ts
        self.by_open = by_open
        self._fill: OrderResponse | None = None  # see `ResultReporter.report_order`
        # Per reporter, not shared: see the note in `BacktestRuntime.__init__`.
        self._skip_measurements = frozenset(tuple(UNRECORDED_MEASUREMENTS))
        self._columns = {dataset: Columns() for dataset in lake.RECORDS}

    def _in_range(self, ts: float) -> bool:
        return self._start_ts <= ts <= self._end_ts

    def add_metric(self, metric: Metric) -> None:
        """Record a metric as it is, in range or not: `ResultReporter`'s `EVAL`s, which it has ranged."""
        dataset = _DATASET.get(metric.measurement, lake.METRIC)
        row, tags = flatten(metric)
        if dataset == lake.METRIC:
            row = {"ts": row.pop("ts"), "measurement": Measurement(metric.measurement).name, **row}
        self._columns[dataset].add(row, tags)

    def report_metrics(self, metrics: list[Metric]) -> None:
        for metric in metrics:
            if metric.measurement in self._skip_measurements:
                continue
            if self._in_range(metric.ts):
                self.add_metric(metric)

    def report_order(self, order_response: OrderResponse) -> None:
        if self.by_open:
            self._fill = order_response
        elif self._in_range(order_response.get_ts()):
            self.add_metric(order_response.to_metric())

    def report_pnl(self, pnl_update: PnlUpdate) -> None:
        if self.by_open:
            fill, self._fill = self._fill, None
            opened = pnl_update.get_ts() if pnl_update.settle_type == SettleType.OPEN else pnl_update.open_ts
            if not self._in_range(opened):
                return
            if fill is not None:
                self.add_metric(fill.to_metric())
        elif not self._in_range(pnl_update.get_ts()):
            return
        row, tags = flatten(pnl_update.to_metric())
        row.update(open_ts=pnl_update.open_ts, notional=pnl_update.notional, open_cost=pnl_update.open_cost)
        self._columns[lake.PNL].add(row, tags)

    def tables(self) -> dict:
        """What was recorded, one table per dataset; None for a dataset with no rows."""
        return {dataset: columns.table() for dataset, columns in self._columns.items()}
