import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from strategy_evaluation import evaluate


class EvaluationTests(unittest.TestCase):
    def test_validation_winner_cannot_change_development_selection(self):
        start = datetime(2025, 10, 1, tzinfo=timezone.utc)
        bars = [SimpleNamespace(time=start + timedelta(days=i)) for i in range(300)]
        original = {
            "parameters": {
                "start": start.isoformat(),
                "lot": 10,
                "fee_bps_per_side": 5,
                "slippage_bps_per_side": 5,
            },
            "summary": {"initial": 100000},
        }

        def simulation(values, **kwargs):
            # MA wins development; reversion wins validation. Selection must
            # not use the latter. No OHLC needed for this orchestration test.
            params = kwargs["params"]
            earlier = kwargs["start"] == start
            pnl = (
                (2 if params.kind == "ma" else 1)
                if earlier
                else (100 if params.kind == "reversion" else -1)
            )
            return {"summary": {"net_pnl": pnl, "closed_trades": 12}}

        with tempfile.TemporaryDirectory() as tmp, patch(
            "strategy_evaluation.read_experiment", return_value=(original, bars)
        ), patch("strategy_evaluation.simulate", side_effect=simulation) as sim:
            result = evaluate(Path(tmp))
            self.assertEqual(result["selected_by_development"], "ma_10_30")
            self.assertNotIn("ma_10_30", result["passed_validation"])
            self.assertTrue((Path(tmp) / "strategy_evaluation.md").exists())
            self.assertTrue(
                any(c.kwargs["slippage_bps"] == 10 for c in sim.call_args_list)
            )
            split = datetime.fromisoformat(result["split"])
            # Development never includes validation bars.
            self.assertTrue(all(v.time < split for v in sim.call_args_list[0].args[0]))
