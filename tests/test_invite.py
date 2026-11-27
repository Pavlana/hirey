"""Recruiters' invitations: one paste in the whole-search Draft a reply form ("Invitation reply"), read by the
agent (`invite save`): the job named is recorded at Interested with source "LinkedIn (recruiter)", queues company
research and the CV fit, is screened against the criteria and joins the Top 5 marked LinkedIn; a job not named
yet records what the reply asks for. The draft never mentions pay. dev3 is never called. Made-up data only."""

import os
from unittest import mock

from prep import criteria, db, search
from prep.config import REPO_ROOT, load_settings
from tests.test_comms import PASTED
from tests.test_resend_cancel import ResendCancelBase
from tests.test_web import TODAY

EXAMPLE = REPO_ROOT / "criteria.example.md"
INVITE = ("Hi Sam, I'm recruiting for Northwind Robotics: a Senior AI Engineer to build LLM agents, "
          "London hybrid, team of eight engineers. Open to a chat?")
VAGUE = "Hi Sam, I have an exciting AI role with a fast-growing client. Interested in a chat?"
SCREEN = ('role_family = "yes"\nlevel = "yes"\nteam = "yes"\nlocation = "yes"\npay = "unknown"\n'
          'builds_agents = "yes"\nevals = "unknown"\ncontract = "permanent"\n')


class InviteBase(ResendCancelBase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"PREP_CRITERIA_FILE": str(EXAMPLE)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = load_settings()

    def request(self, text: str = INVITE, notes: str = "") -> int:
        with self.conn() as conn:
            return db.add_draft_request(conn, None, "invite", "linkedin", text, notes)

    def save(self, *argv: str, screen: str = SCREEN) -> tuple[int, str, str]:
        with mock.patch("sys.stdin") as stdin:
            stdin.read.return_value = screen
            return self.run_cli("invite", "save", *argv)


class StorageTest(InviteBase):
    def test_request_is_about_the_whole_search(self) -> None:
        msg = self.request()
        with self.conn() as conn:
            m = db.get_message(conn, msg)
            self.assertTrue(m["body"].startswith("Draft a LinkedIn invitation reply (/reply)."))
            with self.assertRaises(ValueError):
                db.add_draft_request(conn, 1, "invite", "linkedin", INVITE)  # not on an application
            with self.assertRaises(ValueError):
                db.add_draft_request(conn, None, "invite", "linkedin", "")  # needs the paste

    def test_named_job_is_recorded_and_queued(self) -> None:
        msg = self.request()
        with self.conn() as conn:
            inv, created = db.save_invitation(conn, msg, "Northwind Robotics", "Senior AI Engineer", "London")
            app = db.get_application(conn, inv["application_id"])
            kinds = {r["kind"] for r in db.open_requests(conn, app["id"])}
            moved = db.get_message(conn, msg)["application_id"]
        self.assertTrue(created)
        self.assertEqual((app["status"], app["source"], app["getjo_job_id"]), ("to_apply", db.INVITE_SOURCE, None))
        self.assertEqual(kinds, {"research", "cv_fit"})
        self.assertEqual(moved, app["id"])  # the recruiter's text now sits with the application
        self.assertEqual(inv["job_id"], f"linkedin:{inv['id']}")

    def test_answer_naming_the_job_shows_on_the_card(self) -> None:
        first = self.request(VAGUE)
        with self.conn() as conn:
            inv, _ = db.save_invitation(conn, first, missing=("company", "interviews"))
        answer = self.request("It's Northwind Robotics, Senior AI Engineer.")
        code, _, err = self.save("--to", str(answer), "--company", "Northwind Robotics", "--role", "Senior AI Engineer",
                                 "--invite", str(inv["id"]))
        self.assertEqual(code, 0, err)
        with self.conn() as conn:
            app = db.get_invitation(conn, inv["id"])["application_id"]
            kinds = {r["kind"] for r in db.open_requests(conn, app)}
        self.assertEqual(kinds, {"research", "cv_fit"})
        panel = self.get(f"/?app={app}")[2]
        self.assertIn("Named in the recruiter's answer", panel)
        self.assertIn("Held: interview style unknown", panel)
        self.assertIn("CV fit being checked", panel)
        self.assertIn("Drafting the reply…", panel)
        self.assertIn("Researching…", panel)

    def test_missing_then_answered(self) -> None:
        first = self.request(VAGUE)
        with self.conn() as conn:
            inv, created = db.save_invitation(conn, first, missing=("company", "description", "interviews"))
            self.assertFalse(created)
            self.assertIsNone(inv["application_id"])
            self.assertEqual(inv["missing"], ("company", "description", "interviews"))
            with self.assertRaises(ValueError):
                db.save_invitation(conn, first)  # neither named nor missing
            with self.assertRaises(ValueError):
                db.save_invitation(conn, first, missing=("salary",))  # pay is never asked for
        answer = self.request("It's Northwind Robotics, Senior AI Engineer; take-home then a pairing session.")
        with self.conn() as conn:
            same, created = db.save_invitation(conn, answer, "Northwind Robotics", "Senior AI Engineer",
                                               invite_id=inv["id"])
            both = [db.get_message(conn, m)["application_id"] for m in (first, answer)]
        self.assertTrue(created)
        self.assertEqual(same["id"], inv["id"])
        self.assertEqual(same["missing"], ())
        self.assertEqual(both, [same["application_id"]] * 2)

    def test_pay_never_in_the_fields_or_an_invitation_draft(self) -> None:
        msg = self.request(INVITE + " Up to £90k.")
        with self.conn() as conn:
            with self.assertRaises(ValueError):
                db.save_invitation(conn, msg, "Northwind", "Senior AI Engineer £90k")
            with self.assertRaises(ValueError):  # the recruiter raised pay; the reply still never does
                db.save_draft(conn, msg, "Hi Jo, thanks. Could you share the salary range?")
            db.save_draft(conn, msg, "Hi Jo,\nThanks, this sounds interesting.\nI'm free Tue or Wed after 2.\nSam")
        ok = self.request(notes="say I'd like to hear the salary band on the call")
        with self.conn() as conn:
            db.save_draft(conn, ok, "Hi Jo, happy to talk; I'd like to hear the salary band on the call. Sam")


class GateTest(InviteBase):
    def named(self) -> dict:
        msg = self.request()
        code, out, err = self.save("--to", str(msg), "--company", "Northwind Robotics", "--role",
                                   "Senior AI Engineer", "--location", "London")
        self.assertEqual(code, 0, err)
        with self.conn() as conn:
            return db.invitations(conn, named=True)[-1]

    def test_cli_prints_the_gate_and_holds_until_the_company_check(self) -> None:
        msg = self.request()
        code, out, err = self.save("--to", str(msg), "--company", "Northwind Robotics", "--role", "Senior AI Engineer",
                                   "--location", "London")
        self.assertEqual(code, 0, err)
        self.assertIn("recorded application", out)
        self.assertIn("Company research and CV fit", out)
        self.assertIn("state: held", out)  # interview style unknown: the no-LeetCode rule holds it

    def test_screen_is_required_and_validated_before_recording(self) -> None:
        msg = self.request()
        self.assertEqual(self.save("--to", str(msg), "--company", "N", "--role", "R", screen="")[0], 2)
        self.assertEqual(self.save("--to", str(msg), "--company", "N", "--role", "R", screen='pay = "95000"')[0], 2)
        with self.conn() as conn:
            self.assertEqual(db.invitations(conn), [])
            self.assertEqual([a for a in db.list_applications(conn) if a["source"] == db.INVITE_SOURCE], [])

    def test_failed_gate_is_shown_and_nothing_is_skipped(self) -> None:
        msg = self.request("Lead AI Engineer at Northwind Robotics, team of one.")
        code, out, _ = self.save("--to", str(msg), "--company", "Northwind Robotics", "--role", "Lead AI Engineer",
                                 screen=SCREEN.replace('team = "yes"', 'team = "no"'))
        self.assertEqual(code, 0)
        self.assertIn("failed: level:", out)  # a team of one is a red flag in the panel, never a gate
        self.assertNotIn("team size", out)
        with self.conn() as conn:
            inv = db.invitations(conn, named=True)[-1]
            self.assertEqual(db.get_application(conn, inv["application_id"])["status"], "to_apply")

    def test_passes_and_joins_the_top_5_marked_linkedin(self) -> None:
        inv = self.named()
        self.run_cli("company", "set", "Northwind Robotics", "--interviews", "yes")
        with self.conn() as conn:
            db.save_company_check(conn, "Northwind Robotics", {"verdict": "strong", "sector_ok": "yes"}, "agent")
            db.save_cv_fit(conn, inv["application_id"], 4, [], str(self.tmp / "fit.md"))
        crit = criteria.load(self.settings)
        with self.conn() as conn:
            row = search.invite_eval(conn, inv, crit, TODAY)
            self.assertEqual(row["state"], "passed")
            self.assertTrue(row["why"].startswith("8/10 CV fit · strong company"))
            top = search.board(conn, [], crit, TODAY, 5)
        self.assertEqual([p["getjo_job_id"] for p in top["picks"]], [inv["job_id"]])
        body = self.get("/?view=offers")[2]
        self.assertIn(">LinkedIn</span>", body)
        self.assertIn(f'data-key="a:{inv["application_id"]}"', body)
        with self.conn() as conn:
            msg = db.thread(conn, inv["application_id"])[0]["id"]
            reply = db.save_draft(conn, msg, "Hi Jo,\nThanks, this sounds interesting.\nFree Tue after 2.\nSam")
        panel = self.get(f"/?app={inv['application_id']}")[2]
        card = panel[panel.index('id="card-invite"'):panel.index("</section>", panel.index('id="card-invite"'))]
        self.assertIn("Passes all gates · 8/10 CV fit · strong company · builds agents", card)
        self.assertNotIn("4/5", card)  # the fit once, out of 10
        self.assertNotIn("pay:", card)  # what needs a look is in sections 1 and 2, not repeated here
        self.assertIn("What is the pay range?", panel)  # a question for the call
        self.assertNotIn("sector:", card)
        self.assertNotIn("tailoring plan", card)  # Tailor CV is the one way to start it
        self.assertIn("Top 5 this week · #1", card)
        self.assertIn(f'id="draft-body-panel-{reply}"', card)  # the reply lands in the panel, editable

    def test_acted_on_pick_stays_with_its_outcome(self) -> None:
        inv = self.named()
        self.run_cli("company", "set", "Northwind Robotics", "--interviews", "yes")
        crit = criteria.load(self.settings)
        with self.conn() as conn:
            search.board(conn, [], crit, TODAY, 5)
            db.move_application(conn, inv["application_id"], "applied", TODAY)
            top = search.board(conn, [], crit, TODAY, 5)
            stats = search.pick_stats(conn)
        self.assertEqual([(p["getjo_job_id"], p["outcome"]) for p in top["picks"]], [(inv["job_id"], "applied")])
        self.assertEqual(top["picks"][0]["job"]["app_id"], inv["application_id"])  # still links to the application
        self.assertEqual((stats["picked"], stats["applied"]), (1, 1))


class FormTest(InviteBase):
    def test_paste_box_is_one_step_and_draft_forms_offer_no_invitation(self) -> None:
        body = self.get("/?app=1")[2]
        self.assertIn('action="/applications/invite"', body)
        self.assertIn(">Paste recruiter message</label>", body)
        self.assertNotIn('value="invite"', body)  # no type choice anywhere

    def test_paste_stores_an_invitation(self) -> None:
        status, reply = self.post_json("/applications/invite", {"pasted": INVITE})
        self.assertEqual(status, 200, reply)
        with self.conn() as conn:
            msg = db.thread(conn)[-1]
        self.assertEqual((msg["comms"], msg["channel"], msg["application_id"]), ("invite", "linkedin", None))
        self.assertEqual(self.post_json("/applications/invite", {"pasted": " "})[0], 400)
        self.assertEqual(self.post("/applications/draft", {"comms": "invite", "channel": "linkedin",
                                                           "pasted": INVITE})[0], 404)  # no such route any more

    def test_invitation_draft_is_editable_in_the_box(self) -> None:
        msg = self.request(VAGUE)
        with self.conn() as conn:
            db.save_invitation(conn, msg, missing=("company", "interviews"))
            reply = db.save_draft(conn, msg, "Hi Alex,\nWhich company is it, and what is the interview format?\nSam")
        body = self.get("/")[2]
        self.assertIn(f'<textarea class="draft-body draft-edit" id="draft-body-{reply}"', body)
        self.assertIn(f'data-copy="draft-body-{reply}">Copy</button>', body)

    def test_reply_show_prints_both_cases_and_waiting_invitations(self) -> None:
        first = self.request(VAGUE)
        with self.conn() as conn:
            db.save_invitation(conn, first, missing=("company", "interviews"))
        msg = self.request("It's Northwind Robotics.")
        out = self.run_cli("reply", "show", str(msg))[1]
        self.assertIn("This is a recruiter's invitation", out)
        self.assertIn(str(EXAMPLE), out)
        self.assertIn("asked for company, interviews", out)
        self.assertIn("--missing", out)
        self.assertIn("never asks about pay", out)
