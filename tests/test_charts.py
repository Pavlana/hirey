import unittest

from prep import charts

EVIL = '<script>"x"&</script>'
ALL = (
    lambda: charts.stacked_columns([(EVIL, [3, 1]), ("b", [2, 0])], [(EVIL, "s4"), ("b", EVIL)]),
    lambda: charts.share_bar([(EVIL, 3, "s6"), ("b", 1, EVIL)]),
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
        self.assertIn('class="s5"', charts.share_bar([("a", 1, "evil onload")]))

    def test_empty_data_draws_nothing(self) -> None:
        self.assertEqual(charts.stacked_columns([("d", [0, 0])], [("a", "s4"), ("b", "s6")]), "")
        self.assertEqual(charts.share_bar([("a", 0, "s5")]), "")
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

    def test_stacked_columns(self) -> None:
        out = charts.stacked_columns([("d1", [3, 1]), ("d2", [0, 2])], [("Read", "s4"), ("Write", "s6")],
                                     fmt=lambda v: f"{v}t")
        self.assertEqual(out.count('<rect class="s4"'), 1)  # a zero block is not drawn
        self.assertEqual(out.count('<rect class="s6"'), 2)
        self.assertIn("<title>d1 · Read: 3t</title>", out)
        self.assertIn(">4t</text>", out)  # the column total
        self.assertIn('viewBox="0 0 1000 240"', out)  # wide: it fills the card

    def test_share_bar(self) -> None:
        out = charts.share_bar([("A", 3, "s8"), ("B", 1, "s4"), ("C", 0, "s2")])
        self.assertEqual(out.count("<rect class="), 2)
        self.assertIn("<title>A: 75%</title>", out)
        self.assertIn('width="300"', out)

    def test_columns_mark_target_hits(self) -> None:
        out = charts.columns([("w1", 2), ("w2", 5)], target=4)
        self.assertIn('class="s6"', out)  # below target
        self.assertIn('class="s9"', out)  # target reached
        self.assertIn("target 4", out)

    def test_fit(self) -> None:
        self.assertEqual(charts._fit("short", 100), "short")
        self.assertTrue(charts._fit("a much longer label", 50).endswith("…"))
