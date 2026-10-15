import unittest

from kalshi_market_maker.model import EwmaVol, FairValueModel


class FairValueTests(unittest.TestCase):
    def setUp(self):
        self.model = FairValueModel()

    def test_at_the_money_is_near_half(self):
        for minute in (0, 5, 12.5):
            self.assertAlmostEqual(self.model.p_yes(100_000, 100_000, 0.0007, minute), 0.5, delta=0.01)

    def test_monotone_in_spot(self):
        for minute in (0, 3.5, 7, 13):
            prices = [self.model.p_yes(100_000 + d, 100_000, 0.0007, minute) for d in range(-400, 401, 20)]
            self.assertEqual(prices, sorted(prices))

    def test_more_certain_as_time_runs_out(self):
        early = self.model.p_yes(100_050, 100_000, 0.0007, 2)
        late = self.model.p_yes(100_050, 100_000, 0.0007, 13)
        self.assertGreater(late, early)

    def test_rejects_time_outside_fitted_range(self):
        with self.assertRaises(ValueError):
            self.model.p_yes(100_000, 100_000, 0.0007, 14.5)

    def test_ewma_vol(self):
        vol = EwmaVol(halflife_minutes=10)
        self.assertIsNone(vol.sigma)
        for close in (100.0, 100.1, 100.0, 100.1, 100.0):
            vol.update(close)
        self.assertAlmostEqual(vol.sigma, 0.001, delta=0.0002)


if __name__ == "__main__":
    unittest.main()
