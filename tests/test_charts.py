import unittest

from prep import charts

EVIL = '<script>"x"&</script>'
ALL = (
    lambda: charts.donut([(EVIL, 3, "s6"), ("b", 1, EVIL)], EVIL),
    lambda: charts.treemap([(EVIL, EVIL, [(EVIL, 5, "s2"), ("b", 2, "s9")]), ("g2", "s8", [("c", 3, "s4")])]),
    lambda: charts.funnel([(EVIL, 4, 1.0, "s5"), ("b", 1, .25, "s7"), ("c", 0, 0.0, "s9")]),
    lambda: charts.columns([(EVIL, 2), ("b", 5)], target=4),
    lambda: charts.line([(EVIL, 2), ("b", 0), ("c", 3)]),
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
        self.assertIn('class="s5"', charts.donut([("a", 1, "evil onload")], "x"))

    def test_treemap_text_uses_matching_ink(self) -> None:
        out = charts.treemap([("A", "s9", [("dark block", 5, "s2"), ("light block", 5, "s9")])])
        self.assertIn('class="tm-text k2"', out)
        self.assertIn('class="tm-text k9"', out)

    def test_empty_data_draws_nothing(self) -> None:
        self.assertEqual(charts.donut([("a", 0, "s5")], "x"), "")
        self.assertEqual(charts.treemap([("A", "s9", [("a", 0, "s5")])]), "")
        self.assertEqual(charts.funnel([("a", 0, None, "s5")]), "")
        self.assertEqual(charts.columns([]), "")
        self.assertEqual(charts.line([]), "")

    def test_donut_segments_and_legend(self) -> None:
        out = charts.donut([("Applied", 3, "s5"), ("Offer", 1, "s10"), ("None", 0, "s1")], "applications")
        self.assertEqual(out.count("<circle class=\"s"), 2)  # zero values get no segment
        self.assertIn(">4</text>", out)  # total in the middle
        self.assertIn("75%", out)
        self.assertIn("25%", out)

    def test_columns_mark_target_hits(self) -> None:
        out = charts.columns([("w1", 2), ("w2", 5)], target=4)
        self.assertIn('class="s6"', out)  # below target
        self.assertIn('class="s9"', out)  # target reached
        self.assertIn("target 4", out)

    def test_treemap_skips_empty_groups(self) -> None:
        out = charts.treemap([("A", "s9", [("x", 2, "s5")]), ("B", "s7", [("y", 0, "s1")])])
        self.assertIn("A · 2", out)
        self.assertNotIn("B ·", out)

    def test_fit(self) -> None:
        self.assertEqual(charts._fit("short", 100), "short")
        self.assertTrue(charts._fit("a much longer label", 50).endswith("…"))
