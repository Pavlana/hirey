import unittest

from prep import charts

EVIL = '<script>"x"&</script>'
ALL = (
    lambda: charts.funnel([(EVIL, 4, 1.0, "s5"), ("b", 1, .25, "s7"), ("c", 0, 0.0, "s9")]),
    lambda: charts.columns([(EVIL, 2), ("b", 5)], target=4),
)


class ChartsTest(unittest.TestCase):
    def test_labels_escaped_and_csp_safe(self) -> None:
        for make in ALL:
            out = make()
            with self.subTest(out=out[:30]):
                self.assertTrue(out)
                self.assertNotIn("<script>", out)
                self.assertNotIn('"x"', out)
                self.assertIn("&lt;script&gt;", out)
                self.assertNotIn("style=", out)
                self.assertNotIn("http", out)

    def test_unknown_shade_falls_back(self) -> None:
        self.assertIn('class="s5"', charts.columns([("a", 1)], color="evil onload"))

    def test_empty_data_draws_nothing(self) -> None:
        self.assertEqual(charts.funnel([("a", 0, None, "s5")]), "")
        self.assertEqual(charts.columns([]), "")
        self.assertEqual(charts.trend([]), "")

    def test_trend(self) -> None:
        out = charts.trend([("2026-03-01", 1), ("2026-03-05", 5), ("<x>", 3)])
        self.assertEqual(out.count("<circle "), 3)
        self.assertIn("<polyline ", out)
        self.assertIn('points="6,38 110,6 214,22"', out)  # 1 at the bottom, 5 at the top
        self.assertIn("<title>&lt;x&gt;: 3</title>", out)
        single = charts.trend([("2026-03-01", 4)])
        self.assertNotIn("<polyline", single)  # one score: a dot only
        self.assertEqual(single.count("<circle "), 1)

    def test_columns_mark_target_hits(self) -> None:
        out = charts.columns([("w1", 2), ("w2", 5)], target=4)
        self.assertIn('class="s6"', out)  # below target
        self.assertIn('class="s9"', out)  # target reached
        self.assertIn("target 4", out)

    def test_fit(self) -> None:
        self.assertEqual(charts._fit("short", 100), "short")
        self.assertTrue(charts._fit("a much longer label", 50).endswith("…"))
