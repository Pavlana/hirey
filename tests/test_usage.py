"""Claude Code token usage (prep/usage.py) and the Metrics page's Usage tab, on made-up transcripts
written into a temp folder: never the real ~/.claude/projects, never a real dev3."""

import json
import os
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest import mock

from prep import db, prices, usage, web
from prep.config import ConfigError, load_settings
from prep.seed_example import build_usage_example
from tests.helpers import IsolatedTestCase
from tests.test_web import HOST, PAY, TODAY, WebTestCase

SECRET = "SECRET-CONTENT-MARKER-4242"
WT = usage.WORKTREES


def line(msg_id: str | None, ts: str = "2026-01-19T10:00:00Z", cwd: str = "/example/Dev/inter_prep",
         session: str = "s1", **counts: int) -> str:
    usage_ = {"input_tokens": counts.get("i", 10), "output_tokens": counts.get("o", 20),
              "cache_read_input_tokens": counts.get("r", 1000), "cache_creation_input_tokens": counts.get("w", 100)}
    message = {"role": "assistant", "model": "claude-example-1", "content": [{"type": "text", "text": SECRET}],
               "usage": usage_}
    if msg_id:
        message["id"] = msg_id
    return json.dumps({"timestamp": ts, "cwd": cwd, "sessionId": session, "type": "assistant", "message": message})


class ScanTest(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.tmp / "claude-projects"
        (self.root / "-example").mkdir(parents=True)
        self.file = self.root / "-example" / "s1.jsonl"
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)

    def write(self, *lines: str, mode: str = "a", end: str = "\n") -> None:
        with self.file.open(mode, encoding="utf-8") as f:
            f.write("\n".join(lines) + end)

    def rows(self) -> dict:
        return {r["message_id"]: dict(r) for r in self.conn.execute("SELECT * FROM usage_messages")}

    def test_only_counts_are_stored_and_ids_are_merged(self) -> None:
        user = json.dumps({"timestamp": "2026-01-19T09:59:00Z", "type": "user", "message": {"content": SECRET}})
        self.write(user, line("m1", o=5), line("m1", o=300), "{not json", line(None), line("m2", i=0, o=0, r=0, w=0))
        self.assertEqual(usage.scan(self.conn, self.root), {"files": 1, "read": 1})
        rows = self.rows()
        self.assertEqual(list(rows), ["m1"])  # no id, all-zero and non-usage lines are skipped
        self.assertEqual((rows["m1"]["output"], rows["m1"]["cache_read"]), (300, 1000))  # largest value kept
        self.assertEqual(rows["m1"]["day"], "2026-01-19")
        dump = "\n".join(self.conn.iterdump())
        self.assertNotIn(SECRET, dump)
        self.assertNotIn("content", dump.replace("CREATE", ""))

    def test_cache_lifetime_speed_and_geo_are_stored(self) -> None:
        raw = json.loads(line("m1", w=300))
        raw["message"]["usage"].update({"cache_creation": {"ephemeral_1h_input_tokens": 200,
                                                           "ephemeral_5m_input_tokens": 100},
                                        "speed": "fast", "inference_geo": "us"})
        self.write(json.dumps(raw), line("m2"))
        usage.scan(self.conn, self.root)
        rows = self.rows()
        self.assertEqual((rows["m1"]["cache_write_1h"], rows["m1"]["speed"], rows["m1"]["geo"]), (200, "fast", "us"))
        self.assertEqual((rows["m2"]["cache_write_1h"], rows["m2"]["speed"]), (None, None))  # no split: unknown

    def test_new_cache_column_reads_every_transcript_again(self) -> None:
        self.write(line("m1"))
        usage.scan(self.conn, self.root)
        self.assertEqual(usage.scan(self.conn, self.root)["read"], 0)
        self.conn.executescript("ALTER TABLE usage_messages DROP COLUMN cache_write_1h;")
        db._add_missing_columns(self.conn)
        self.assertEqual(usage.scan(self.conn, self.root)["read"], 1)  # usage_files was emptied once

    def test_incremental_and_partial_last_line(self) -> None:
        self.write(line("m1"))
        usage.scan(self.conn, self.root)
        self.assertEqual(usage.scan(self.conn, self.root)["read"], 0)  # nothing new: the file is not opened
        self.write(line("m2"), line("m3")[:40], end="")  # m3 is still being written
        usage.scan(self.conn, self.root)
        self.assertEqual(sorted(self.rows()), ["m1", "m2"])
        offset = self.conn.execute("SELECT offset FROM usage_files").fetchone()[0]
        self.assertEqual(self.file.read_bytes()[offset - 1:offset], b"\n")
        self.write(line("m3")[40:])
        usage.scan(self.conn, self.root)
        self.assertEqual(sorted(self.rows()), ["m1", "m2", "m3"])

    def test_a_shorter_file_is_read_again_without_double_counting(self) -> None:
        self.write(line("m1", o=50), line("m2"))
        usage.scan(self.conn, self.root)
        self.write(line("m1", o=50), mode="w")
        usage.scan(self.conn, self.root)
        self.assertEqual(self.rows()["m1"]["output"], 50)
        self.assertEqual(len(self.rows()), 2)

    def test_subfolders_and_missing_root(self) -> None:
        sub = self.root / "-example" / "s1" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text(line("m9") + "\n")
        self.assertEqual(usage.scan(self.conn, self.root)["files"], 1)
        self.assertIn("m9", self.rows())
        self.assertEqual(usage.scan(self.conn, self.tmp / "nope"), {"files": 0, "read": 0})

    def test_cli_scan(self) -> None:
        self.write(line("m1"))
        with mock.patch.dict(os.environ, {"PREP_CLAUDE_PROJECTS_DIR": str(self.root)}):
            code, out, _ = self.run_cli("usage", "scan")
        self.assertEqual(code, 0)
        self.assertIn("Read 1 of 1 transcript files; 1 responses stored.", out)


PROJECTS = [{"project_id": "p-prep", "name": "exampleprep", "path": "/example/Dev/inter_prep"},
            {"project_id": "p-jobs", "name": "examplejobs", "path": "/example/Dev/getjo"}]


class PlaceTest(IsolatedTestCase):
    def test_places(self) -> None:
        cases = {
            f"{WT}/example-Dev-inter_prep/a1b2c3d4/worktree": ("inter_prep", "a1b2c3d4"),
            f"{WT}/example-Dev-inter_prep/a1b2c3d4/worktree/prep": ("inter_prep", "a1b2c3d4"),  # a subfolder
            f"{WT}/example-Dev-getjo/f6f6f6f6/worktree": ("getjo", "f6f6f6f6"),
            "/example/Dev/inter_prep": ("inter_prep", None),
            "/example/Dev/inter_prep/tests": ("inter_prep", None),
            "/example/Dev/getjo/.dev3/worktrees/9a9a9a9a/x": ("getjo", "9a9a9a9a"),
            "/somewhere/else": ("else", None),
            "": ("other", None),
        }
        for cwd, expected in cases.items():
            with self.subTest(cwd=cwd):
                self.assertEqual(usage.place(cwd, PROJECTS), expected)
        # Without dev3's project list a worktree folder still reads as its last part.
        self.assertEqual(usage.place(f"{WT}/Users-x-Dev-inter_prep/a1b2c3d4/worktree", []), ("inter_prep", "a1b2c3d4"))

    def test_short(self) -> None:
        self.assertEqual([usage.short(n) for n in (999, 1500, 2_000_000, 161_400_000, 1_060_000_000)],
                         ["999", "1.5k", "2M", "161M", "1.1B"])


class Dev3Test(IsolatedTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.conn = db.connect(self.prep_db)
        self.addCleanup(self.conn.close)
        usage._LAST_TRY[0] = 0.0
        self.calls: list[list[str]] = []
        dev3 = self.tmp / "dev3"
        dev3.write_text("")
        self.settings = load_settings()
        self.settings = self.settings.__class__(**{**self.settings.__dict__, "dev3_bin": dev3})

    def fake(self, _dev3: str, args: list[str]) -> object:
        self.calls.append(args)
        if args[:2] == ["projects", "list"]:
            return [{"id": "p-prep", "name": "exampleprep", "path": "/example/Dev/inter_prep"}]
        if args[:2] == ["tasks", "list"]:
            return {"tasks": [
                {"id": "a1b2c3d4-0000", "seq": 7, "title": "Example assistant", "taskType": "standard", "status": "in-progress"},
                {"id": "b2c3d4e5-0000", "seq": 8, "title": "Example build", "taskType": "standard", "status": "review-by-user"},
                {"id": "ffffffff-0000", "seq": 9, "title": "Not in usage", "taskType": "standard", "status": "todo"},
            ]}
        if args[:2] == ["task", "show"]:
            merged = args[3].startswith("b2c3d4e5")
            return {"pullRequests": [{"number": 1, "state": "MERGED" if merged else "OPEN"}]}
        return None

    def test_refresh_stores_wanted_tasks_with_ids_only_on_the_command_line(self) -> None:
        with mock.patch.object(usage, "_dev3_json", self.fake):
            self.assertTrue(usage.refresh_tasks(self.conn, self.settings, {"a1b2c3d4", "b2c3d4e5"}))
        tasks = {r["seq"]: dict(r) for r in self.conn.execute("SELECT * FROM usage_tasks")}
        self.assertEqual(sorted(tasks), [7, 8])  # a task without usage is not stored
        self.assertEqual((tasks[7]["pr_merged"], tasks[8]["pr_merged"]), (0, 1))
        self.assertIn(["tasks", "list", "--limit", "1000", "--project", "p-prep"], self.calls)
        self.assertIn(["task", "show", "--task", "b2c3d4e5-0000", "--project", "p-prep"], self.calls)
        # Fresh cache: no second call.
        self.calls.clear()
        with mock.patch.object(usage, "_dev3_json", self.fake):
            self.assertFalse(usage.refresh_tasks(self.conn, self.settings, {"a1b2c3d4"}))
        self.assertEqual(self.calls, [])

    def test_unknown_task_waits_between_failed_tries(self) -> None:
        with mock.patch.object(usage, "_dev3_json", return_value=None) as run:
            self.assertFalse(usage.refresh_tasks(self.conn, self.settings, {"a1b2c3d4"}))
            self.assertFalse(usage.refresh_tasks(self.conn, self.settings, {"a1b2c3d4"}))
        self.assertEqual(run.call_count, 1)  # the second try waits RETRY_SECONDS

    def test_no_dev3(self) -> None:
        settings = self.settings.__class__(**{**self.settings.__dict__, "dev3_bin": self.tmp / "missing"})
        with mock.patch.object(usage, "_dev3_json") as run:
            self.assertFalse(usage.refresh_tasks(self.conn, settings, {"a1b2c3d4"}))
        run.assert_not_called()

    def test_dev3_json_failure_is_none(self) -> None:
        with mock.patch.object(usage.subprocess, "run", side_effect=OSError("boom")):
            self.assertIsNone(usage._dev3_json("dev3", ["projects", "list"]))


class ExampleUsage(WebTestCase):
    """The committed made-up usage (tests/fixtures/example.json "usage") written as transcripts."""

    def setUp(self) -> None:
        super().setUp()
        spec = self.fixtures["usage"]
        build_usage_example(self.tmp / "claude-projects", self.prep_db, spec, date.fromisoformat(TODAY))
        env = {"PREP_ASSISTANT_TASK": "7", "PREP_PREP_ASSISTANT_TASK": "9",
               "PREP_ASSISTANT_PROJECT": spec["assistant_project"],
               "PREP_DEV3_BIN": str(self.tmp / "no-dev3")}  # never a real dev3, and no assistant ping
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = load_settings()

    def report(self, days: int = 14, project: str | None = "inter_prep", **changes) -> dict:
        settings = self.settings.__class__(**{**self.settings.__dict__, **changes})
        with closing(db.connect(self.prep_db)) as conn:
            usage.sync(conn, settings)
            return usage.report(conn, settings, TODAY, days, project)


class ReportTest(ExampleUsage):
    def test_roles_top_and_totals(self) -> None:
        r = self.report()
        self.assertEqual(r["projects"], ["getjo", "inter_prep"])
        self.assertEqual({role for role, _, _ in r["roles"]},
                         {"Applications Assistant", "Learn Assistant", "Coordinator", "Build tasks", "Main checkouts"})
        costs = [c for _, _, c in r["roles"]]
        self.assertEqual(costs, sorted(costs, reverse=True))  # roles sorted by dollars
        self.assertEqual(r["top"][0]["ref"], "seq:7")
        self.assertEqual(r["top"][0]["title"], "Applications Assistant")
        self.assertEqual([t["cost"] for t in r["top"]], sorted((t["cost"] for t in r["top"]), reverse=True))
        self.assertNotIn("seq:getjo/12", [t["ref"] for t in r["top"]])  # another project: filtered out
        self.assertEqual(sum(n for _, n, _ in r["roles"]), r["total"])
        self.assertAlmostEqual(sum(c for _, _, c in r["roles"]), sum(t["cost"] for t in r["top"]))
        self.assertEqual(sum(v for d in r["days"] for v in d[1:]), r["total"])
        self.assertEqual(len(r["days"]), 14)
        self.assertGreater(r["cache_read_share"], .8)

    def test_turns_count_each_response_once(self) -> None:
        r = self.report(days=30, project=None)  # the example spans 30 days
        files = list((self.tmp / "claude-projects").rglob("*.jsonl"))
        responses = {json.loads(l)["message"]["id"] for f in files for l in f.read_text().splitlines()
                     if "usage" in json.loads(l)["message"]}
        self.assertEqual(r["turns"], len(responses))  # each response is on two lines in the files
        self.assertIn("seq:getjo/12", [t["ref"] for t in r["top"]])

    def test_context_and_where_to_save(self) -> None:
        r = self.report()
        ctx = {c["role"]: c for c in r["context"]}
        self.assertEqual(list(ctx), ["Applications Assistant", "Learn Assistant", "Coordinator"])
        self.assertTrue(ctx["Applications Assistant"]["over"])
        self.assertFalse(ctx["Learn Assistant"]["over"])
        self.assertEqual(len(ctx["Coordinator"]["trend"]), 14)
        save = "\n".join(r["save"])
        self.assertIn("Applications Assistant (seq:7) is at", save)
        self.assertIn("time to /compact", save)
        self.assertIn("seq:41 Tailored CV as PDF is still open after its PR merged", save)
        self.assertNotIn("seq:52 Learn: tick off sources is still open", save)  # completed
        self.assertIn("A session of seq:52 wrote", save)  # 40% cache writes
        self.assertFalse(self.report(context_warn_tokens=400_000)["context"][0]["over"])

    def test_days_and_coordinator_setting(self) -> None:
        week = self.report(days=7)
        self.assertLess(week["total"], self.report()["total"])
        self.assertEqual(len(week["days"]), 7)
        r = self.report(coordinator_task=41)  # a seq named in the settings is a coordinator too
        self.assertIn("seq:41", [c["ref"] for c in r["context"] if c["role"] == "Coordinator"])

    def test_standing_tasks_are_not_asked_to_complete(self) -> None:
        # seq:41 has a merged PR and is still open; as a standing task it stays open by design.
        for setting in ("assistant_task", "prep_assistant_task", "coordinator_task"):
            with self.subTest(setting=setting):
                save = "\n".join(self.report(**{setting: 41})["save"])
                self.assertNotIn("still open after its PR merged", save)

    def test_money_windows_and_split(self) -> None:
        r = self.report()
        m = r["money"]
        self.assertEqual(len(m["days"]), usage.MONEY_DAYS)  # fixed 30 days, whatever the filter
        self.assertEqual(m, self.report(days=7)["money"])
        self.assertAlmostEqual(sum(sum(c) for _, c in m["days"]), m["month"]["cost"])
        self.assertAlmostEqual(sum(sum(c) for _, c in m["days"][-7:]), m["week"]["cost"])
        self.assertAlmostEqual(sum(m["days"][-1][1]), m["today"]["cost"])
        self.assertLess(m["today"]["cost"], m["week"]["cost"])
        self.assertLess(m["week"]["cost"], m["month"]["cost"])
        self.assertEqual([k for k, _, _ in m["kinds"]], list(usage.COST_KINDS))
        self.assertAlmostEqual(sum(c for _, c, _ in m["kinds"]), m["month"]["cost"])
        # seq:27 ran on a model with no price, 19 days ago: in the 30-day window only.
        self.assertEqual(m["unknown_models"], [("claude-example-1", m["month"]["unpriced"])])
        self.assertGreater(m["month"]["unpriced"], 0)
        self.assertEqual((m["week"]["unpriced"], r["unpriced"]), (0, 0))
        self.assertIn("seq:27", [t["ref"] for t in self.report(days=30)["top"]])
        t27 = next(t for t in self.report(days=30)["top"] if t["ref"] == "seq:27")
        self.assertEqual((t27["cost"], t27["unpriced"]), (0.0, t27["tokens"]))

    def test_settings_price_overrides_and_prices_the_unknown(self) -> None:
        r = self.report(days=30, prices=(("claude-example", (1.0, 2.0, 0.5, 1.5, 3.0)),))
        self.assertEqual(r["money"]["unknown_models"], [])
        self.assertEqual(r["money"]["month"]["unpriced"], 0)
        t27 = next(t for t in r["top"] if t["ref"] == "seq:27")
        self.assertGreater(t27["cost"], 0)
        free = self.report(prices=(("claude-", (0.0, 0.0, 0.0, 0.0, 0.0)),))  # a prefix covers every model
        self.assertEqual(free["money"]["month"]["cost"], 0.0)


class CostMathTest(IsolatedTestCase):
    """usage.price and usage.cost with made-up prices: never the real list in assertions on sums."""

    PRICES = (("claude-made-up", (1.0, 2.0, 0.5, 1.5, 3.0)),)

    def settings(self, prices: tuple = PRICES):
        s = load_settings(self.tmp / "missing.toml")
        return s.__class__(**{**s.__dict__, "prices": prices})

    def test_cost_split(self) -> None:
        p = (1.0, 2.0, 0.5, 1.5, 3.0)
        # 1M input, 2M output, 4M cache read, 3M cache write of which 1M is 1-hour.
        cr, cw, o, i = usage.cost((1_000_000, 2_000_000, 4_000_000, 3_000_000, 1_000_000), p)
        self.assertEqual((cr, o, i), (2.0, 4.0, 1.0))
        self.assertEqual(cw, 2 * 1.5 + 1 * 3.0)
        self.assertEqual(usage.cost((0, 0, 0, 0, 0), p), (0.0, 0.0, 0.0, 0.0))
        self.assertAlmostEqual(sum(usage.cost((7, 0, 0, 0, 0), p)), 7e-6)  # tiny counts stay exact

    def test_settings_prefix_wins_over_the_built_in_list(self) -> None:
        s = self.settings((("claude-opus-5-5", (1.0, 1.0, 1.0, 1.0, 1.0)),))
        self.assertEqual(usage.price(s, "claude-opus-5-5"), (1.0, 1.0, 1.0, 1.0, 1.0))
        self.assertEqual(usage.price(self.settings(), "claude-made-up-2"), self.PRICES[0][1])

    def test_unknown_model_has_no_price(self) -> None:
        s = self.settings(())
        for model in ("claude-example-1", "claude-3-opus-20240229", "<synthetic>", "", None,
                      "claude-opus-5-5-preview", "claude-opus-5-55"):
            with self.subTest(model=model):
                self.assertIsNone(usage.price(s, model))

    def test_built_in_ids(self) -> None:
        s = self.settings(())
        base = usage.price(s, "claude-opus-4-1")
        self.assertIsNotNone(base)
        for model in ("claude-opus-4-1-20250805", "claude-opus-4-1[1m]", "claude-opus-4-1@20250805"):
            with self.subTest(model=model):
                self.assertEqual(usage.price(s, model), base)
        for key, row in prices.DEFAULTS.items():
            with self.subTest(key=key):
                self.assertEqual(len(row), 5)
                self.assertTrue(all(v > 0 for v in row))
                self.assertEqual(row[4], row[0] * 2)       # 1-hour write is 2x input
                self.assertAlmostEqual(row[3], row[0] * 1.25)  # 5-minute write is 1.25x input

    def test_modifiers(self) -> None:
        s = self.settings((("claude-made-up", (1.0, 2.0, 0.5, 1.5, 3.0)),))
        us = usage.price(s, "claude-made-up", geo="us")
        self.assertEqual([round(v, 6) for v in us], [1.1, 2.2, 0.55, 1.65, 3.3])
        self.assertEqual(usage.price(s, "claude-made-up", speed="fast"), (1.0, 2.0, 0.5, 1.5, 3.0))  # no fast row
        with mock.patch.dict(prices.FAST, {"claude-made-up": (4.0, 8.0)}), \
                mock.patch.dict(prices.DEFAULTS, {"claude-made-up": (1.0, 2.0, 0.5, 1.5, 3.0)}):
            fast = usage.price(self.settings(()), "claude-made-up", speed="fast")
            self.assertEqual(fast, (4.0, 8.0, 2.0, 6.0, 12.0))  # cache prices scale with input
        with mock.patch.dict(prices.LONG_PROMPT_PRICES, {"claude-made-up": (5.0, 5.0, 5.0, 5.0, 5.0)}), \
                mock.patch.dict(prices.DEFAULTS, {"claude-made-up": (1.0, 2.0, 0.5, 1.5, 3.0)}):
            self.assertEqual(usage.price(self.settings(()), "claude-made-up", long_prompt=True), (5.0,) * 5)
            self.assertEqual(usage.price(self.settings(()), "claude-made-up"), (1.0, 2.0, 0.5, 1.5, 3.0))

    def test_long_prompt_is_per_response(self) -> None:
        conn = db.connect(self.prep_db)
        self.addCleanup(conn.close)
        with mock.patch.dict(prices.LONG_PROMPT_PRICES, {"claude-made-up": (10.0, 10.0, 10.0, 10.0, 10.0)}), \
                mock.patch.dict(prices.DEFAULTS, {"claude-made-up": (1.0, 1.0, 1.0, 1.0, 1.0)}):
            for msg_id, read in (("short", prices.LONG_PROMPT - 100), ("long", prices.LONG_PROMPT)):
                conn.execute("INSERT INTO usage_messages (message_id, ts, day, cwd, session, model, input, output,"
                             " cache_read, cache_write) VALUES (?, '2026-01-19T10:00:00Z', '2026-01-19', '/example/x', 's', 'claude-made-up',"
                             " 50, 0, ?, 50)", (msg_id, read))
            conn.commit()
            r = usage.report(conn, self.settings(()), "2026-01-19", 7)
        short = (prices.LONG_PROMPT - 100 + 100) / 1e6 * 1.0
        long_ = (prices.LONG_PROMPT + 100) / 1e6 * 10.0
        self.assertAlmostEqual(r["money"]["today"]["cost"], short + long_)


class UsageTabTest(ExampleUsage):
    def test_usage_tab(self) -> None:
        body = self.get("/metrics")[2]
        for text in ("Tokens per day", "Top tasks by cost", "Who spends what", "Context size per turn",
                     "Cost per day by role, last 30 days", "Where the money goes", web.ESTIMATE_NOTE,
                     "Last 7 days", "Last 30 days", "price unknown", "claude-example-1",
                     "Where to save", "Cache-read share", 'class="chart columns stacked"', 'class="chart share-bar"',
                     "time to /compact", "Tailored CV as PDF", '<span class="mono">seq:7</span>'):
            self.assertIn(text, body)
        self.assertNotIn(SECRET, body)
        self.assertNotIn("example reply", body)  # the made-up content never reaches the page
        self.assertNotIn(" style=", body)
        self.assertIn('href="/metrics?tab=usage&amp;project=getjo"', body)
        self.assertIn('class="view-tab is-active" href="/metrics?tab=usage&amp;project=inter_prep"', body)

    def test_filters_and_settings_prices(self) -> None:
        body = self.get("/metrics?project=all&days=30")[2]
        self.assertIn("Tokens, last 30 days", body)
        self.assertIn("seq:getjo/12", body)
        self.assertIn('<span class="muted">price unknown</span>', body)  # seq:27, no price for its model
        settings_file = self.tmp / "prices.toml"
        settings_file.write_text('[prices."claude-example"]\ninput = 1\noutput = 2\ncache_read = 0.1\n'
                                 'cache_write = 1.25\n')
        with mock.patch.dict(os.environ, {"INTERPREP_SETTINGS": str(settings_file)}):
            self.settings = load_settings()
            body = self.get("/metrics?project=all&days=30")[2]
        self.assertNotIn("price unknown", body)

    def test_usd(self) -> None:
        for x, text in ((0, "$0"), (-1, "$0"), (.004, "<$0.01"), (.0099, "<$0.01"), (.01, "$0.01"), (.0149, "$0.01"),
                        (3.274, "$3.27"), (3.275 + 1e-9, "$3.28"), (41.8, "$41.80"), (999.994, "$999.99"),
                        (999.996, "$1,000"), (1000, "$1,000"), (1234.5, "$1,234"), (1235.5, "$1,236")):
            with self.subTest(x=x):
                self.assertEqual(web._usd(x), text)

    def test_escapes_titles(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            conn.execute("UPDATE usage_tasks SET title = '<img src=x onerror=alert(1)>' WHERE seq = 7")
            conn.commit()
        body = self.get("/metrics")[2]
        self.assertNotIn("<img src=x", body)
        self.assertIn("&lt;img src=x", body)

    def test_pay_never_rendered(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.add_application(conn, "Example Pay", "Engineer", status="applied", pay=PAY)
        self.assertNotIn(PAY, self.get("/metrics")[2])


class UsageSettingsTest(IsolatedTestCase):
    def write(self, text: str) -> Path:
        path = self.tmp / "s.toml"
        path.write_text(text)
        return path

    def test_defaults(self) -> None:
        with mock.patch.dict(os.environ, {"PREP_CLAUDE_PROJECTS_DIR": ""}):
            s = load_settings(self.write(""))
        self.assertEqual(s.claude_projects_dir, Path.home() / ".claude" / "projects")
        self.assertEqual((s.context_warn_tokens, s.coordinator_task, s.prices), (300_000, None, ()))

    def test_keys(self) -> None:
        os.environ.pop("PREP_COORDINATOR_TASK")  # an empty value would clear the file's (CLEARABLE)
        s = load_settings(self.write('coordinator_task = 3\ncontext_warn_tokens = 250000\n'
                                     '[prices."claude-a"]\ninput = 3\noutput = 15\ncache_write = 3.75\n'
                                     '[prices."claude-b"]\ncache_write = 1\ncache_write_1h = 2\n'
                                     '[prices."claude-a-long"]\ninput = 1\ncache_read = 0.3\n'))
        self.assertEqual((s.coordinator_task, s.context_warn_tokens), (3, 250_000))
        self.assertEqual(s.prices, (("claude-a-long", (1.0, 0.0, 0.3, 0.0, 0.0)),
                                    ("claude-a", (3.0, 15.0, 0.0, 3.75, 3.75)),  # 1-hour write defaults to cache_write
                                    ("claude-b", (0.0, 0.0, 0.0, 1.0, 2.0))))

    def test_bad_values(self) -> None:
        for text in ('[prices."m"]\ninput = "x"\n', '[prices."m"]\ninput = -1\n', 'prices = 3\n',
                     'context_warn_tokens = 5\n', 'claude_projects_dir = "rel/path"\n'):
            with self.subTest(text=text), mock.patch.dict(os.environ, {"PREP_CLAUDE_PROJECTS_DIR": ""}):
                with self.assertRaises(ConfigError):
                    load_settings(self.write(text))
