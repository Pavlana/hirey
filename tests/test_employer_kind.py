"""applications.employer_kind: product / client / unknown (NULL) — storage, CLI, pipeline label, filter and order."""
import re
from contextlib import closing

from prep import db, web
from tests.helpers import IsolatedTestCase
from tests.test_web import WebTestCase


class EmployerKindDbTest(IsolatedTestCase):
    def test_validated_and_nullable(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            app_id = db.add_application(conn, "Acme Rockets", "Engineer", employer_kind="product")
            self.assertEqual(db.get_application(conn, app_id)["employer_kind"], "product")
            with self.assertRaises(ValueError):
                db.update_application(conn, app_id, employer_kind="agency")
            db.update_application(conn, app_id, employer_kind=None)
            self.assertIsNone(db.get_application(conn, app_id)["employer_kind"])

    def test_migration_adds_column_to_old_db(self) -> None:
        with closing(db.connect(self.prep_db)) as conn:
            db.add_application(conn, "Old Co", "Engineer")
            conn.execute("ALTER TABLE applications DROP COLUMN employer_kind")  # a database from before the column
            conn.commit()
        with closing(db.connect(self.prep_db)) as conn:
            self.assertIsNone(db.get_application(conn, 1)["employer_kind"])


class EmployerKindCliTest(IsolatedTestCase):
    def kind(self) -> str | None:
        with closing(db.connect(self.prep_db)) as conn:
            return db.get_application(conn, 1)["employer_kind"]

    def test_set_and_clear(self) -> None:
        self.run_cli("applied", "--company", "Nowhere Ltd", "--role", "SRE")
        code, out, _ = self.run_cli("update", "1", "--employer-kind", "client")
        self.assertEqual(code, 0)
        self.assertIn("(client)", out)
        self.assertEqual(self.kind(), "client")
        self.run_cli("update", "1", "--employer-kind", "product")
        self.assertEqual(self.kind(), "product")
        _, out, _ = self.run_cli("apps")
        self.assertIn("Nowhere Ltd — SRE [applied] (product)", out)
        self.run_cli("update", "1", "--employer-kind", "unknown")
        self.assertIsNone(self.kind())
        code, _, err = self.run_cli("update", "1", "--employer-kind", "agency")
        self.assertEqual(code, 2)
        self.assertIn("invalid choice", err)


class EmployerKindWebTest(WebTestCase):
    def setUp(self) -> None:
        super().setUp()
        with self.conn() as conn:
            # Same stage (Offer, empty in the fixtures); added client first so the order is not by id.
            self.client = db.add_application(conn, "Client Co Example", "Engineer", status="offer", employer_kind="client")
            self.unknown = db.add_application(conn, "Unknown Co Example", "Engineer", status="offer")
            self.product = db.add_application(conn, "Product Co Example", "Engineer", status="offer", employer_kind="product")

    def offer_rows(self, body: str) -> list[str]:
        group = re.search(r'<tbody data-group="offer">(.*?)</tbody>', body, re.S).group(1)
        return re.findall(r'data-app="(\d+)"', group)

    def test_product_first_then_unknown_then_client(self) -> None:
        body = self.get("/")[2]
        self.assertEqual(self.offer_rows(body), [str(self.product), str(self.unknown), str(self.client)])

    def test_labels(self) -> None:
        body = self.get("/")[2]
        self.assertIn('class="emp-kind emp-product"', body)
        self.assertIn('class="emp-kind emp-client"', body)
        row = re.search(rf'<tr id="app-{self.unknown}".*?</tr>', body, re.S).group(0)
        self.assertNotIn("emp-kind", row)
        panel = self.get(f"/?app={self.product}")[2]
        self.assertIn('<h2 id="panel-title">Product Co Example</h2>', panel)  # a word in section 1, not a badge
        self.assertIn("Product company", panel)
        self.assertNotIn("<select", panel.split('id="panel"')[1])  # no Employer control any more

    def test_filter(self) -> None:
        body = self.get("/?employer=product")[2]
        self.assertEqual(self.offer_rows(body), [str(self.product)])
        self.assertIn('data-employer="product" aria-current', body)
        self.assertEqual(self.offer_rows(self.get("/?employer=client")[2]), [str(self.client)])
        self.assertEqual(len(self.offer_rows(self.get("/?employer=bogus")[2])), 3)  # unknown value: all

    def test_filter_kept_in_urls(self) -> None:
        f = web.home_filters({"employer": ["client"], "stage": ["offer"]})
        self.assertEqual(web.home_url(f, app=3), "/?stage=offer&employer=client&app=3")
        self.assertEqual(web.home_url(f, employer=""), "/?stage=offer")
        self.assertEqual(web.home_url(f, view="offers"), "/?view=offers")  # pipeline filters only

    def test_no_panel_route_and_research_sets_an_unknown_kind(self) -> None:
        self.assertEqual(self.post("/applications/employer", {"app_id": str(self.unknown), "employer_kind": "client"})[0], 404)
        profile = {"company": "Unknown Co Example", "file_path": "x.md", "researched_at": "2026-03-01",
                   "employer_kind": "client"}
        with self.conn() as conn:
            db.save_company_profile(conn, db.company_slug("Unknown Co Example"), profile)
            self.assertEqual(db.get_application(conn, self.unknown)["employer_kind"], "client")
            db.save_company_profile(conn, db.company_slug("Product Co Example"),
                                    {**profile, "company": "Product Co Example"})
            self.assertEqual(db.get_application(conn, self.product)["employer_kind"], "product")  # a set kind is kept
        self.assertIn("Client delivery", self.get(f"/?app={self.unknown}")[2])
