"""Тесты отчёта. Класс TDecisionN — пункт N из DECISIONS.md; TRealData — итог по data/ на 10.11.2026."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import report  # noqa: E402

DATA = os.path.join(os.path.dirname(__file__), "..", "data")
R = date(2026, 11, 10)


def D(s):
    return Decimal(s)


def contract(cid="D-1", amount="300.00", signed="2026-01-01", contractor="Подрядчик"):
    return {"contract_id": cid, "contractor": contractor, "amount": amount, "signed_at": signed}


def work(wid, price="100.00", status="planned", completed="", cancelled="", cid="D-1", title=None):
    return {"work_id": wid, "contract_id": cid, "title": title or f"Работа {wid}", "price": price,
            "status": status, "completed_at": completed, "cancelled_at": cancelled}


def pay(pid, amount, status="paid", paid="2026-02-01", created="2026-02-01", refunded="", wid="",
        cid="D-1", purpose="Оплата"):
    return {"payment_id": pid, "contract_id": cid, "work_id": wid, "purpose": purpose, "amount": amount,
            "status": status, "created_at": created, "paid_at": paid, "refunded_at": refunded}


def build(works=(), payments=(), contracts=None, on=R):
    return report.build_report(contracts or [contract()], list(works), list(payments), on)


def kinds(rep, kind):
    return [i.text for i in rep.attention if i.kind == kind]


# ---------- итог по data/ ----------

class TRealData(unittest.TestCase):
    """Итог на 10.11.2026 — сверен вручную (README, «Сверка»)."""

    @classmethod
    def setUpClass(cls):
        cls.rep = report.build_report(*report.load_data(DATA), R)

    def test_works(self):
        expected = {  # статус, выплачено, осталось
            "W-11": ("done", "100000.00", "0.00"),
            "W-12": ("done", "100000.00", "0.00"),
            "W-13": ("cancelled", "0.00", "0.00"),
            "W-14": ("done", "100000.00", "0.00"),
            "W-15": ("planned", "0.00", "100000.00"),
            "W-16": ("planned", "0.00", "100000.00"),
            "W-21": ("done", "20000.00", "0.00"),
            "W-22": ("done", "30000.00", "0.00"),
            "W-23": ("planned", "0.00", "40000.00"),
        }
        got = {wid: (w.status, str(w.paid), str(w.remaining)) for wid, w in self.rep.works.items()}
        self.assertEqual(got, expected)

    def test_contract_d1(self):
        c = self.rep.contracts["D-1"]
        self.assertEqual(
            (c.amount, c.works_total, c.paid_gross, c.refunded, c.paid_net, c.allocated, c.unallocated,
             c.queued, c.remaining, c.overpaid),
            (D("600000"), D("500000"), D("365000"), D("50000"), D("315000"), D("300000"), D("15000"),
             D("150000"), D("185000"), D("0")))

    def test_contract_d2(self):
        c = self.rep.contracts["D-2"]
        self.assertEqual(
            (c.amount, c.works_total, c.paid_gross, c.refunded, c.paid_net, c.allocated, c.unallocated,
             c.queued, c.remaining, c.overpaid),
            (D("90000"), D("90000"), D("75000"), D("0"), D("75000"), D("50000"), D("25000"),
             D("0"), D("15000"), D("0")))

    def test_invariants(self):
        for cid, c in self.rep.contracts.items():
            own = [w for w in self.rep.works.values() if w.contract_id == cid]
            self.assertEqual(c.allocated + c.unallocated, c.paid_net)
            self.assertEqual(sum(w.paid for w in own), c.allocated)
            self.assertEqual(c.paid_gross - c.refunded, c.paid_net)

    def test_attention(self):
        self.assertEqual(kinds(self.rep, "error"), [])
        self.assertEqual(len(kinds(self.rep, "discrepancy")), 1)
        self.assertIn("D-1", kinds(self.rep, "discrepancy")[0])
        self.assertEqual(len(kinds(self.rep, "check")), 1)
        self.assertIn("P-3", kinds(self.rep, "check")[0])
        after = " ".join(kinds(self.rep, "after_date"))
        self.assertIn("W-23", after)
        self.assertIn("P-8", after)
        self.assertEqual(kinds(self.rep, "overpayment"), [])

    def test_later_date_shows_d2_overpayment(self):
        rep = report.build_report(*report.load_data(DATA), date(2026, 11, 12))
        c = rep.contracts["D-2"]
        self.assertEqual((c.paid_net, c.remaining, c.overpaid), (D("95000"), D("0"), D("5000")))
        self.assertEqual(rep.works["W-23"].status, "done")
        self.assertTrue(any("D-2" in t for t in kinds(rep, "overpayment")))


# ---------- по пунктам DECISIONS.md ----------

class TDecision1(unittest.TestCase):
    def test_event_on_report_date_is_included(self):
        rep = build([work("W-1", status="done", completed="2026-11-10")],
                    [pay("P-1", "50.00", paid="2026-11-10", created="2026-11-10")])
        self.assertEqual(rep.works["W-1"].status, "done")
        self.assertEqual(rep.contracts["D-1"].paid_net, D("50"))

    def test_events_after_date_are_listed_not_counted(self):
        rep = build([work("W-1", status="done", completed="2026-11-11")],
                    [pay("P-1", "50.00", paid="2026-11-12", created="2026-11-12")])
        self.assertEqual(rep.contracts["D-1"].paid_net, D("0"))
        after = " ".join(kinds(rep, "after_date"))
        self.assertIn("W-1", after)
        self.assertIn("P-1", after)


class TDecision2(unittest.TestCase):
    def test_paid(self):
        c = build(payments=[pay("P-1", "70.00")]).contracts["D-1"]
        self.assertEqual((c.paid_gross, c.paid_net, c.queued), (D("70"), D("70"), D("0")))

    def test_created_after_date_does_not_exist(self):
        c = build(payments=[pay("P-1", "70.00", status="queued", paid="", created="2026-11-11")]).contracts["D-1"]
        self.assertEqual((c.paid_net, c.queued), (D("0"), D("0")))

    def test_queued(self):
        c = build(payments=[pay("P-1", "70.00", status="queued", paid="")]).contracts["D-1"]
        self.assertEqual((c.paid_net, c.queued), (D("0"), D("70")))

    def test_paid_after_date_is_queued_on_date(self):
        c = build(payments=[pay("P-1", "70.00", paid="2026-11-11", created="2026-11-01")]).contracts["D-1"]
        self.assertEqual((c.paid_net, c.queued), (D("0"), D("70")))

    def test_refunded_before_date_nets_to_zero(self):
        c = build(payments=[pay("P-1", "70.00", status="refunded", refunded="2026-03-01")]).contracts["D-1"]
        self.assertEqual((c.paid_gross, c.refunded, c.paid_net), (D("70"), D("70"), D("0")))

    def test_refunded_after_date_is_still_paid(self):
        c = build(payments=[pay("P-1", "70.00", status="refunded", refunded="2026-11-11")]).contracts["D-1"]
        self.assertEqual((c.refunded, c.paid_net), (D("0"), D("70")))


class TDecision3(unittest.TestCase):
    def test_accepted_after_date_is_planned(self):
        rep = build([work("W-1", status="done", completed="2026-11-11")], [pay("P-1", "100.00")])
        w = rep.works["W-1"]
        self.assertEqual((w.status, w.paid, w.remaining), ("planned", D("0"), D("100")))
        self.assertEqual(rep.contracts["D-1"].unallocated, D("100"))

    def test_cancelled_after_date_but_accepted_before_is_done(self):
        w = build([work("W-1", status="cancelled", completed="2026-10-01", cancelled="2026-11-11")]).works["W-1"]
        self.assertEqual(w.status, "done")

    def test_cancelled_after_date_never_accepted_is_planned(self):
        w = build([work("W-1", status="cancelled", cancelled="2026-11-11")]).works["W-1"]
        self.assertEqual(w.status, "planned")


class TDecision4(unittest.TestCase):
    def test_cancelled_excluded_from_works_total(self):
        rep = build([work("W-1"), work("W-2", status="cancelled", cancelled="2026-05-01")])
        w = rep.works["W-2"]
        self.assertEqual((w.status, w.paid, w.remaining), ("cancelled", D("0"), D("0")))
        self.assertEqual(rep.contracts["D-1"].works_total, D("100"))

    def test_cancelled_gets_no_advance(self):
        rep = build([work("W-1", status="cancelled", completed="2026-03-01", cancelled="2026-05-01")],
                    [pay("P-1", "100.00")])
        self.assertEqual(rep.works["W-1"].paid, D("0"))
        self.assertEqual(rep.contracts["D-1"].unallocated, D("100"))

    def test_targeted_payment_to_cancelled_goes_to_advance(self):
        rep = build([work("W-1", status="cancelled", cancelled="2026-05-01")],
                    [pay("P-1", "40.00", wid="W-1", purpose="W-1")])
        self.assertEqual(rep.works["W-1"].paid, D("0"))
        self.assertEqual(rep.contracts["D-1"].unallocated, D("40"))
        self.assertTrue(any("W-1" in t for t in kinds(rep, "check")))


class TDecision5(unittest.TestCase):
    def test_remaining_from_works_total_and_discrepancy_flagged(self):
        rep = build([work("W-1", price="200.00")], contracts=[contract(amount="300.00")])
        c = rep.contracts["D-1"]
        self.assertEqual((c.amount, c.works_total, c.remaining), (D("300"), D("200"), D("200")))
        self.assertEqual(len(kinds(rep, "discrepancy")), 1)

    def test_no_discrepancy_when_equal(self):
        rep = build([work("W-1", price="300.00")])
        self.assertEqual(kinds(rep, "discrepancy"), [])


class TDecision6(unittest.TestCase):
    def test_fifo_over_accepted_works_by_completion_date(self):
        rep = build([work("W-1", status="done", completed="2026-06-01"),
                     work("W-2", status="done", completed="2026-05-01"),
                     work("W-3")],
                    [pay("P-1", "150.00")])
        self.assertEqual(rep.works["W-2"].paid, D("100"))
        self.assertEqual(rep.works["W-1"].paid, D("50"))
        self.assertEqual(rep.works["W-1"].remaining, D("50"))
        self.assertEqual(rep.works["W-3"].paid, D("0"))
        c = rep.contracts["D-1"]
        self.assertEqual((c.allocated, c.unallocated), (D("150"), D("0")))

    def test_same_completion_date_ordered_by_work_id(self):
        rep = build([work("W-2", status="done", completed="2026-05-01"),
                     work("W-1", status="done", completed="2026-05-01")],
                    [pay("P-1", "60.00")])
        self.assertEqual((rep.works["W-1"].paid, rep.works["W-2"].paid), (D("60"), D("0")))

    def test_leftover_is_unallocated_advance(self):
        rep = build([work("W-1", status="done", completed="2026-05-01"), work("W-2")], [pay("P-1", "130.00")])
        c = rep.contracts["D-1"]
        self.assertEqual((c.allocated, c.unallocated, c.remaining), (D("100"), D("30"), D("70")))

    def test_advance_fills_only_what_targeted_did_not(self):
        rep = build([work("W-1", status="done", completed="2026-05-01"),
                     work("W-2", status="done", completed="2026-06-01")],
                    [pay("P-1", "30.00", wid="W-1", purpose="W-1"), pay("P-2", "100.00")])
        self.assertEqual((rep.works["W-1"].paid, rep.works["W-2"].paid), (D("100"), D("30")))

    def test_targeted_payment_to_planned_work_stays_on_work(self):
        rep = build([work("W-1")], [pay("P-1", "30.00", wid="W-1", purpose="W-1")])
        self.assertEqual((rep.works["W-1"].paid, rep.works["W-1"].remaining), (D("30"), D("70")))
        self.assertEqual(rep.contracts["D-1"].unallocated, D("0"))


class TDecision7(unittest.TestCase):
    def test_mismatched_purpose_counts_for_work_but_flagged(self):
        rep = build([work("W-1", title="Съёмка: осень")],
                    [pay("P-1", "15.00", wid="W-1", purpose="Материалы для съёмки")])
        self.assertEqual(rep.works["W-1"].paid, D("15"))
        self.assertEqual(len(kinds(rep, "check")), 1)

    def test_purpose_with_work_id_or_title_not_flagged(self):
        rep = build([work("W-1", title="Съёмка: осень")],
                    [pay("P-1", "10.00", wid="W-1", purpose="Оплата по w-1"),
                     pay("P-2", "10.00", wid="W-1", purpose="За «съемка: осень»")])
        self.assertEqual(kinds(rep, "check"), [])

    def test_work_id_matched_as_whole_word(self):
        rep = build([work("W-1")], [pay("P-1", "10.00", wid="W-1", purpose="Оплата W-12")])
        self.assertEqual(len(kinds(rep, "check")), 1)

    def test_queued_payment_also_checked(self):
        rep = build([work("W-1")], [pay("P-1", "10.00", status="queued", paid="", wid="W-1", purpose="Материалы")])
        self.assertEqual(len(kinds(rep, "check")), 1)


class TDecision8(unittest.TestCase):
    def test_targeted_overpayment_capped_and_excess_to_advance(self):
        rep = build([work("W-1"), work("W-2")], [pay("P-1", "130.00", wid="W-1", purpose="W-1")])
        w = rep.works["W-1"]
        self.assertEqual((w.paid, w.remaining), (D("100"), D("0")))
        self.assertEqual(rep.contracts["D-1"].unallocated, D("30"))
        self.assertTrue(any("W-1" in t for t in kinds(rep, "overpayment")))


class TDecision9(unittest.TestCase):
    def test_contract_overpayment(self):
        rep = build([work("W-1", status="done", completed="2026-05-01")], [pay("P-1", "120.00")],
                    contracts=[contract(amount="100.00")])
        c = rep.contracts["D-1"]
        self.assertEqual((c.remaining, c.overpaid, c.unallocated), (D("0"), D("20"), D("20")))
        self.assertTrue(any("D-1" in t for t in kinds(rep, "overpayment")))

    def test_queue_would_overpay_warns(self):
        rep = build([work("W-1")], [pay("P-1", "80.00"), pay("P-2", "50.00", status="queued", paid="")],
                    contracts=[contract(amount="100.00")])
        self.assertEqual(rep.contracts["D-1"].overpaid, D("0"))
        self.assertTrue(any("очеред" in t for t in kinds(rep, "overpayment")))


class TDecision10(unittest.TestCase):
    def test_queue_not_subtracted_from_remaining(self):
        rep = build([work("W-1")], [pay("P-1", "30.00"), pay("P-2", "50.00", status="queued", paid="")],
                    contracts=[contract(amount="100.00")])
        c = rep.contracts["D-1"]
        self.assertEqual((c.paid_net, c.queued, c.remaining), (D("30"), D("50"), D("70")))


class TDecision11(unittest.TestCase):
    def test_bad_amount_row_skipped(self):
        rep = build([work("W-1")], [pay("P-1", "1,5"), pay("P-2", "-5.00"), pay("P-3", "0.00")])
        self.assertEqual(rep.contracts["D-1"].paid_net, D("0"))
        self.assertEqual(len(kinds(rep, "error")), 3)

    def test_bad_price_work_skipped(self):
        rep = build([work("W-1", price="abc")])
        self.assertNotIn("W-1", rep.works)
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_unknown_contract(self):
        rep = build([work("W-9", cid="D-9")], [pay("P-9", "10.00", cid="D-9")])
        self.assertNotIn("W-9", rep.works)
        self.assertNotIn("D-9", rep.contracts)
        self.assertEqual(len(kinds(rep, "error")), 2)

    def test_duplicate_ids_take_first(self):
        rep = build([work("W-1", price="100.00"), work("W-1", price="999.00")],
                    [pay("P-1", "10.00"), pay("P-1", "20.00")])
        self.assertEqual(rep.works["W-1"].price, D("100"))
        self.assertEqual(rep.contracts["D-1"].paid_net, D("10"))
        self.assertEqual(len(kinds(rep, "error")), 2)

    def test_payment_to_foreign_or_unknown_work_is_unbound(self):
        rep = build([work("W-1"), work("W-2", cid="D-2")],
                    [pay("P-1", "10.00", wid="W-2", purpose="W-2"), pay("P-2", "10.00", wid="W-404")],
                    contracts=[contract(), contract("D-2")])
        self.assertEqual(rep.works["W-2"].paid, D("0"))
        self.assertEqual(rep.contracts["D-1"].unallocated, D("20"))
        self.assertEqual(len(kinds(rep, "error")), 2)

    def test_done_without_date_is_done(self):
        rep = build([work("W-1", status="done")])
        self.assertEqual(rep.works["W-1"].status, "done")
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_cancelled_without_date_is_cancelled(self):
        rep = build([work("W-1", status="cancelled")])
        self.assertEqual(rep.works["W-1"].status, "cancelled")
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_paid_without_paid_at_uses_created(self):
        rep = build(payments=[pay("P-1", "10.00", paid="", created="2026-03-01")])
        self.assertEqual(rep.contracts["D-1"].paid_net, D("10"))
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_refunded_without_refunded_at_refunded_on_paid_date(self):
        rep = build(payments=[pay("P-1", "10.00", status="refunded")])
        self.assertEqual(rep.contracts["D-1"].paid_net, D("0"))
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_refund_before_payment_flagged(self):
        rep = build(payments=[pay("P-1", "10.00", status="refunded", paid="2026-03-01", refunded="2026-02-01")])
        c = rep.contracts["D-1"]
        self.assertEqual((c.paid_gross, c.paid_net), (D("10"), D("0")))
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_unknown_statuses(self):
        rep = build([work("W-1", status="wip")], [pay("P-1", "10.00", status="lost")])
        self.assertEqual(rep.works["W-1"].status, "planned")
        self.assertEqual(rep.contracts["D-1"].paid_net, D("0"))
        self.assertEqual(len(kinds(rep, "error")), 2)

    def test_bad_date_treated_as_missing(self):
        rep = build([work("W-1", status="done", completed="10.11.2026")])
        self.assertEqual(rep.works["W-1"].status, "done")
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_bad_contract_amount(self):
        rep = build([work("W-1")], contracts=[contract(amount="много")])
        self.assertEqual(rep.contracts, {})
        self.assertEqual(rep.works, {})
        self.assertGreaterEqual(len(kinds(rep, "error")), 1)


class TDecision12(unittest.TestCase):
    def test_contract_signed_after_date_hidden(self):
        rep = build([work("W-1")], [pay("P-1", "10.00")], contracts=[contract(signed="2026-11-11")])
        self.assertEqual((rep.contracts, rep.works), ({}, {}))
        self.assertEqual(kinds(rep, "error"), [])


class TDecision13(unittest.TestCase):
    def test_parse_money_exact_half_up(self):
        self.assertEqual(report.parse_money("0.105"), D("0.11"))
        self.assertEqual(report.parse_money("0.1"), D("0.10"))
        self.assertEqual(report.parse_money(" 300000.00 "), D("300000.00"))
        for bad in ("", "1,5", "1e3", "abc", "1.2.3", "١٢"):
            with self.assertRaises(ValueError, msg=bad):
                report.parse_money(bad)

    def test_kopecks_sum_exactly(self):
        c = build([work("W-1", price="0.30")], [pay(f"P-{i}", "0.10") for i in range(3)],
                  contracts=[contract(amount="0.30")]).contracts["D-1"]
        self.assertEqual((c.paid_net, c.remaining), (D("0.30"), D("0.00")))

    def test_fmt_money(self):
        self.assertEqual(report.fmt_money(D("1234567.5")), "1 234 567,50")
        self.assertEqual(report.fmt_money(D("0"), sep=""), "0,00")

    def test_csv_sections_and_format(self):
        rep = report.build_report(*report.load_data(DATA), R)
        text = report.render_csv(rep)
        sections = text.strip("\n").split("\n\n")
        self.assertEqual(len(sections), 3)
        self.assertTrue(sections[0].startswith("договор;работа;"))
        self.assertIn("D-1;W-11;Съёмка: весенняя коллекция;принята;100000,00;100000,00;0,00", sections[0])
        self.assertIn("315000,00", sections[1])

    def test_table_contains_totals(self):
        rep = report.build_report(*report.load_data(DATA), R)
        text = report.render_table(rep)
        for s in ("10.11.2026", "Требует внимания", "185 000,00", "15 000,00", "W-23"):
            self.assertIn(s, text)


# ---------- CLI ----------

class TCli(unittest.TestCase):
    def run_main(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = report.main(list(argv))
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_table_default(self):
        code, out, _ = self.run_main("--date", "2026-11-10", "--data", DATA)
        self.assertEqual(code, 0)
        self.assertIn("185 000,00", out)

    def test_csv(self):
        code, out, _ = self.run_main("--date", "2026-11-10", "--data", DATA, "--format", "csv")
        self.assertEqual(code, 0)
        self.assertIn("договор;работа;", out)

    def test_bad_date(self):
        code, _, _ = self.run_main("--date", "10.11.2026", "--data", DATA)
        self.assertEqual(code, 2)

    def test_missing_data_dir(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = self.run_main("--date", "2026-11-10", "--data", d)
        self.assertEqual(code, 2)
        self.assertIn("contracts.csv", err)

    def test_missing_column(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("contracts.csv", "works.csv", "payments.csv"):
                with open(os.path.join(DATA, name), encoding="utf-8") as src:
                    text = src.read()
                if name == "payments.csv":
                    text = text.replace("refunded_at", "refund")
                with open(os.path.join(d, name), "w", encoding="utf-8") as dst:
                    dst.write(text)
            code, _, err = self.run_main("--date", "2026-11-10", "--data", d)
        self.assertEqual(code, 2)
        self.assertIn("refunded_at", err)


if __name__ == "__main__":
    unittest.main()
