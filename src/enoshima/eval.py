import numpy as np
from qate.core.model import Measurement, Metric, Tag
from qate.core.order import OrderResponse
from qate.trading.pnl_tracker import PnlUpdate
from qate.util.counter import Counter


def evaluate_strategy(updates: list, risk_free_rate=0):
    if len(updates) < 2:
        return {
            # "sharpe_ratio": 0.0,
            # "sortino_ratio": 0.0,
            # "calmar_ratio": 0.0,
            # "max_drawdown": 0.0,
            "sharpe_ratio_annualized": 0.0,
        }

    # Extract timestamps and returns
    timestamps = [x[0] for x in updates]
    returns = np.array([x[1] for x in updates])

    # Estimate trades per year from average time delta
    time_deltas = np.diff(sorted(timestamps))
    avg_delta = np.mean(time_deltas)
    seconds_per_year = 365.25 * 24 * 60 * 60
    trades_per_year = seconds_per_year / avg_delta if avg_delta > 0 else 0

    excess_returns = returns - risk_free_rate

    # Sharpe Ratio
    mean_excess_return = np.mean(excess_returns)
    std_dev = np.std(returns, ddof=1)
    sharpe_ratio = mean_excess_return / std_dev if std_dev != 0 else float("inf")

    # # Sortino Ratio
    # downside_returns = returns[returns < risk_free_rate] - risk_free_rate
    # downside_std = np.std(downside_returns, ddof=1)
    # sortino_ratio = mean_excess_return / downside_std if downside_std != 0 else float("inf")

    # cumulative_returns = np.cumsum(returns)
    # drawdowns = cumulative_returns - np.maximum.accumulate(cumulative_returns)
    # max_drawdown = np.min(drawdowns)
    # # Calmar Ratio
    # calmar_ratio = mean_excess_return / abs(max_drawdown) if max_drawdown != 0 else float("inf")

    result = {
        # "sharpe_ratio": sharpe_ratio,
        # "sortino_ratio": sortino_ratio,
        # "calmar_ratio": calmar_ratio,
        # "max_drawdown": abs(max_drawdown),
        "sharpe_ratio_annualized": sharpe_ratio * np.sqrt(trades_per_year) if trades_per_year > 0 else 0.0,
    }

    return result


class Evaluation:
    def __init__(self, extra_slippage_rate: float = 0.0):
        self.extra_slippage_rate = extra_slippage_rate
        self.suffix = str(int(self.extra_slippage_rate * 100000))

        self.win = Counter()
        self.loss = Counter()
        self.updates: list = []
        self.max_so_far = 0.0
        self.max_drawdown = 0.0

        self.overall_pnl = 0.0
        self.trading_pnl = 0.0
        self.total_fee = 0.0

        self.order_slippage = 0.0
        self.order_slippage_rate = 0.0
        self.order_cnt = 0
        self.extra_slippage = 0.0

    def on_order(self, order_response: OrderResponse):
        # extra_slippage is calculated in different places and it is not double counted:
        # - here we include it when accumulating order-level averages so the reported slippage rate
        #   reflects the total latency/infra penalty we expect in production.
        # - on_position we add the same surcharge to the overall PnL so the curve shows the net effect
        #   once fills settle with the added cushion.
        # Empirically, an extra_slippage_rate of 0.005% mirrors what the current trading stack incurs
        # (code paths, hosting, connectivity, etc.).
        #
        # NOTE: 0.005% may look surprisingly tight. That figure is derived from months of live execution logs,
        # The low penalty stems from two factors:
        #   1) this niche has limited competition, so urgency premia stay narrow;
        #   2) current order sizes are small enough to slip under most liquidity cliffs.
        # Expect the rate to climb once either assumption changes (larger tickets or more rivals).
        extra_slippage = order_response.notional * self.extra_slippage_rate
        slippage = order_response.slippage + extra_slippage
        slippage_rate = slippage / order_response.notional
        self.order_slippage += slippage
        self.order_slippage_rate += slippage_rate
        self.order_cnt += 1

    def _on_pnl_update(self, pnl_update: PnlUpdate):
        self.total_fee += pnl_update.fee
        self.extra_slippage += pnl_update.notional * self.extra_slippage_rate

    def on_open_pnl_update(self, pnl_update: PnlUpdate):
        self._on_pnl_update(pnl_update)

    def on_close_pnl_update(self, pnl_update: PnlUpdate) -> Metric:
        self._on_pnl_update(pnl_update)

        last_pnl = self.overall_pnl
        self.trading_pnl += pnl_update.pnl
        self.overall_pnl = self.trading_pnl - self.total_fee - self.extra_slippage
        pnl_change = self.overall_pnl - last_pnl

        return_pct = pnl_change / pnl_update.open_cost * (1.0 + self.extra_slippage_rate)
        self.updates.append((pnl_update.get_ts(), return_pct))

        if self.overall_pnl > self.max_so_far:
            self.max_so_far = self.overall_pnl
        drawdown = self.max_so_far - self.overall_pnl
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

        if pnl_change > 0.0:
            self.win.add(pnl_change)
        else:
            self.loss.add(pnl_change)

        return Metric(
            Measurement.EVAL.value,
            pnl_update.get_ts(),
            [
                Tag.EXCHANGE_NAME.value,
                pnl_update.market.exchange_name.value,
                Tag.SYMBOL.value,
                pnl_update.market.symbol.value,
            ],
            [
                f"pnl_change{self.suffix}",
                pnl_change,
            ],
        )

    def get_result(self) -> dict:
        return_result = evaluate_strategy(self.updates)
        total_cnt = self.win.cnt + self.loss.cnt
        win_rate = self.win.cnt * 100 / (total_cnt) if total_cnt > 0 else 0.0
        slipppage = 0.0 if self.order_cnt == 0 else self.order_slippage / self.order_cnt
        slippage_rate = 0.0 if self.order_cnt == 0 else self.order_slippage_rate / self.order_cnt
        # profit_factor = abs(self.win.avg / self.loss.avg) if (total_cnt > 0 and self.loss.cnt > 0) else 0.0
        result = {
            "pnl_cnt": total_cnt,
            "max_drawdown": self.max_drawdown,
            "pnl": self.overall_pnl,
            "fee": -self.total_fee,
            "slippage": slipppage,
            "slippage_rate": slippage_rate,
            "loss_avg": self.loss.avg,
            "loss_cnt": self.loss.cnt,
            "win_avg": self.win.avg,
            "win_cnt": self.win.cnt,
            "win_rate": win_rate,
            # "profit_factor": profit_factor,
        }
        result.update(return_result)
        output = {}
        for k, v in result.items():
            output[f"{k}{self.suffix}"] = v
        return output
