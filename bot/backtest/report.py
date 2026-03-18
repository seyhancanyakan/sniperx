"""PinShot Bot — Backtest Report Generator"""

import csv


class ReportGenerator:
    def __init__(self, result):
        self.result = result

    def print_summary(self):
        r = self.result
        print()
        print("=" * 50)
        print("  PINSHOT BACKTEST REPORT")
        print("=" * 50)
        print(f"  Total Trades:   {r.total_trades}")
        print(f"  Wins:           {r.wins} | Losses: {r.losses}")
        print(f"  Win Rate:       {r.win_rate:.1f}%")
        print(f"  Total R:        {r.total_r:+.2f}R")
        print(f"  Expectancy:     {r.expectancy:.2f}R per trade")
        print(f"  Profit Factor:  {r.profit_factor:.2f}")
        print(f"  Max Drawdown:   {r.max_drawdown:.2f}R")
        print("=" * 50)
        print()

        if r.trades:
            print("  Last 10 trades:")
            for t in r.trades[-10:]:
                result_str = t.get("result", "?")
                r_val = t.get("r_multiple", 0)
                direction = t.get("direction", "?").upper()
                bars = t.get("bars", 0)
                print(f"    {direction:4s} | {r_val:+.2f}R | {result_str:8s} | {bars} bars")
            print()

    def to_dict(self) -> dict:
        r = self.result
        return {
            "total_trades": r.total_trades,
            "wins": r.wins,
            "losses": r.losses,
            "win_rate": r.win_rate,
            "total_r": r.total_r,
            "expectancy": r.expectancy,
            "profit_factor": r.profit_factor,
            "max_drawdown": r.max_drawdown,
            "trades": r.trades,
        }

    def save_csv(self, filepath: str):
        if not self.result.trades:
            return
        keys = self.result.trades[0].keys()
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(self.result.trades)
