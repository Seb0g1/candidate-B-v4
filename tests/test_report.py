"""Тесты отчёта. Номера в названиях классов = пункты DECISIONS.md."""
import codecs
import csv
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from datetime import date
from decimal import Decimal as D

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import report  # noqa: E402

DATA = os.path.join(ROOT, "data")


def contract(cid="C", amount="100.00", signed="2026-01-01", contractor="Подрядчик"):
    return {"contract_id": cid, "contractor": contractor, "amount": amount, "signed_at": signed}


def work(wid, cid="C", price="50.00", status="planned", completed="", cancelled="", title=None):
    return {"work_id": wid, "contract_id": cid, "title": f"Работа {wid}" if title is None else title, "price": price,
            "status": status, "completed_at": completed, "cancelled_at": cancelled}


def pay(pid, amount, status="paid", cid="C", wid="", purpose="Оплата", created="2026-02-01",
        paid="", refunded=""):
    if status in ("paid", "refunded") and paid == "":
        paid = created
    return {"payment_id": pid, "contract_id": cid, "work_id": wid, "purpose": purpose, "amount": amount,
            "status": status, "created_at": created, "paid_at": paid, "refunded_at": refunded}


def build(contracts, works, payments, on="2026-06-01"):
    return report.build_report(contracts, works, payments, date.fromisoformat(on))


def c_line(rep, cid="C"):
    return next(c for c in rep.contracts if c.contract_id == cid)


def w_line(rep, wid):
    return next(w for w in rep.works if w.work_id == wid)


def kinds(rep, kind):
    return [i for i in rep.attention if i.kind == kind]


def texts(rep):
    return "\n".join(i.text for i in rep.attention)


class T13Money(unittest.TestCase):
    def test_parse_money_string_to_decimal_half_up(self):
        self.assertEqual(report.parse_money("100.005"), D("100.01"))
        self.assertEqual(report.parse_money("100.004"), D("100.00"))
        self.assertEqual(report.parse_money(" 300000.00 "), D("300000.00"))

    def test_parse_money_rejects_garbage(self):
        for bad in ("", "abc", "1,5", "NaN", "Infinity"):
            with self.assertRaises(ValueError, msg=bad):
                report.parse_money(bad)

    def test_no_float_rounding_errors(self):
        works = [work("W1", price="0.10", status="done", completed="2026-02-01"),
                 work("W2", price="0.20", status="done", completed="2026-02-02")]
        rep = build([contract(amount="0.30")], works,
                    [pay("P1", "0.10"), pay("P2", "0.20")])
        c = c_line(rep)
        self.assertEqual(c.works_total, D("0.30"))
        self.assertEqual(c.paid_net, D("0.30"))
        self.assertEqual(c.remaining, D("0.00"))
        self.assertEqual(kinds(rep, "discrepancy"), [])


class T01AsOfDate(unittest.TestCase):
    def test_report_date_is_inclusive(self):
        rep = build([contract()], [work("W1", status="done", completed="2026-06-01")],
                    [pay("P1", "10.00", created="2026-06-01", paid="2026-06-01")], on="2026-06-01")
        self.assertEqual(w_line(rep, "W1").status, "done")
        self.assertEqual(c_line(rep).paid_net, D("10.00"))


class T02PaymentState(unittest.TestCase):
    def setUp(self):
        self.c = [contract()]
        self.w = [work("W1")]

    def test_created_after_date_does_not_exist(self):
        rep = build(self.c, self.w, [pay("P1", "10.00", status="queued", created="2026-07-01")])
        self.assertEqual(c_line(rep).queued, D("0"))
        self.assertEqual(c_line(rep).paid_net, D("0"))

    def test_paid_after_date_is_queued_on_date(self):
        rep = build(self.c, self.w, [pay("P1", "10.00", created="2026-05-01", paid="2026-07-01")])
        c = c_line(rep)
        self.assertEqual(c.paid_net, D("0"))
        self.assertEqual(c.queued, D("10.00"))

    def test_queued_counts_in_queue(self):
        rep = build(self.c, self.w, [pay("P1", "10.00", status="queued", created="2026-05-01")])
        self.assertEqual(c_line(rep).queued, D("10.00"))

    def test_refunded_before_date_nets_to_zero(self):
        rep = build(self.c, self.w, [pay("P1", "10.00", status="refunded", created="2026-05-01",
                                         paid="2026-05-02", refunded="2026-05-10")])
        c = c_line(rep)
        self.assertEqual(c.paid_gross, D("10.00"))
        self.assertEqual(c.refunded, D("10.00"))
        self.assertEqual(c.paid_net, D("0.00"))

    def test_refunded_after_date_still_paid_on_date(self):
        rep = build(self.c, self.w, [pay("P1", "10.00", status="refunded", created="2026-05-01",
                                         paid="2026-05-02", refunded="2026-07-10")])
        c = c_line(rep)
        self.assertEqual(c.paid_net, D("10.00"))
        self.assertEqual(c.refunded, D("0"))


class T03WorkState(unittest.TestCase):
    def test_accepted_after_date_is_planned_and_reported(self):
        rep = build([contract()], [work("W1", status="done", completed="2026-07-01")], [])
        self.assertEqual(w_line(rep, "W1").status, "planned")
        self.assertTrue(any("W1" in i.text for i in kinds(rep, "after_date")))

    def test_cancelled_after_date_is_planned_on_date(self):
        rep = build([contract(amount="50.00")], [work("W1", status="cancelled", cancelled="2026-07-01")], [])
        w = w_line(rep, "W1")
        self.assertEqual(w.status, "planned")
        self.assertEqual(w.remaining, D("50.00"))
        self.assertEqual(c_line(rep).works_total, D("50.00"))


class T04Cancelled(unittest.TestCase):
    def test_cancelled_work_not_in_obligations(self):
        works = [work("W1", price="50.00", status="cancelled", cancelled="2026-03-01"),
                 work("W2", price="50.00")]
        rep = build([contract(amount="100.00")], works, [])
        self.assertEqual(w_line(rep, "W1").status, "cancelled")
        self.assertEqual(w_line(rep, "W1").remaining, D("0"))
        c = c_line(rep)
        self.assertEqual(c.works_total, D("50.00"))
        self.assertEqual(c.remaining, D("50.00"))

    def test_targeted_payment_on_cancelled_work_goes_to_advance(self):
        works = [work("W1", price="50.00", status="cancelled", cancelled="2026-03-01"),
                 work("W2", price="50.00")]
        rep = build([contract(amount="50.00")], works, [pay("P1", "20.00", wid="W1", purpose="W1")])
        self.assertEqual(w_line(rep, "W1").paid, D("0"))
        c = c_line(rep)
        self.assertEqual(c.unallocated, D("20.00"))
        self.assertIn("P1", texts(rep))


class T05AmountVsWorks(unittest.TestCase):
    def test_discrepancy_flagged_and_remaining_from_works(self):
        rep = build([contract(amount="100.00")], [work("W1", price="60.00")], [pay("P1", "10.00")])
        c = c_line(rep)
        self.assertEqual(c.amount, D("100.00"))
        self.assertEqual(c.works_total, D("60.00"))
        self.assertEqual(c.remaining, D("50.00"))
        self.assertEqual(len(kinds(rep, "discrepancy")), 1)

    def test_no_discrepancy_when_equal(self):
        rep = build([contract(amount="50.00")], [work("W1")], [])
        self.assertEqual(kinds(rep, "discrepancy"), [])


class T06Allocation(unittest.TestCase):
    def test_fifo_by_acceptance_date_then_id(self):
        works = [work("W3", status="done", completed="2026-03-01"),
                 work("W2", status="done", completed="2026-02-15"),
                 work("W1", status="done", completed="2026-03-01")]
        rep = build([contract(amount="150.00")], works, [pay("P1", "120.00")])
        self.assertEqual(w_line(rep, "W2").paid, D("50.00"))
        self.assertEqual(w_line(rep, "W1").paid, D("50.00"))
        self.assertEqual(w_line(rep, "W3").paid, D("20.00"))
        self.assertEqual(w_line(rep, "W3").remaining, D("30.00"))
        c = c_line(rep)
        self.assertEqual(c.allocated, D("120.00"))
        self.assertEqual(c.unallocated, D("0.00"))

    def test_planned_works_get_nothing_rest_is_advance(self):
        works = [work("W1", status="done", completed="2026-03-01"), work("W2")]
        rep = build([contract()], works, [pay("P1", "80.00")])
        self.assertEqual(w_line(rep, "W1").paid, D("50.00"))
        self.assertEqual(w_line(rep, "W2").paid, D("0"))
        c = c_line(rep)
        self.assertEqual(c.allocated, D("50.00"))
        self.assertEqual(c.unallocated, D("30.00"))
        self.assertEqual(c.remaining, D("20.00"))

    def test_targeted_payment_first_then_pool_tops_up(self):
        works = [work("W1", status="done", completed="2026-03-01")]
        rep = build([contract(amount="50.00")], works,
                    [pay("P1", "15.00", wid="W1", purpose="за W1"), pay("P2", "100.00")])
        self.assertEqual(w_line(rep, "W1").paid, D("50.00"))
        self.assertEqual(c_line(rep).unallocated, D("65.00"))

    def test_targeted_payment_on_planned_work_counts(self):
        rep = build([contract(amount="50.00")], [work("W1")], [pay("P1", "10.00", wid="W1", purpose="W1")])
        self.assertEqual(w_line(rep, "W1").paid, D("10.00"))
        self.assertEqual(w_line(rep, "W1").remaining, D("40.00"))

    def test_invariant_allocated_plus_advance_equals_paid(self):
        rep = report.build_report(*report.load_data(DATA), date(2026, 11, 10))
        for c in rep.contracts:
            self.assertEqual(c.allocated + c.unallocated, c.paid_net, c.contract_id)
            self.assertEqual(sum(w.paid for w in rep.works if w.contract_id == c.contract_id), c.allocated)


class T07Purpose(unittest.TestCase):
    def test_purpose_not_mentioning_work_is_flagged(self):
        rep = build([contract(amount="50.00")], [work("W1", title="Съёмка: осень")],
                    [pay("P1", "10.00", wid="W1", purpose="Материалы для съёмки")])
        self.assertEqual(w_line(rep, "W1").paid, D("10.00"))
        self.assertEqual(len(kinds(rep, "check")), 1)
        self.assertIn("P1", kinds(rep, "check")[0].text)

    def test_purpose_with_title_or_id_not_flagged(self):
        rep = build([contract(amount="100.00")],
                    [work("W1", title="Съёмка: осень"), work("W2", title="Лето")],
                    [pay("P1", "10.00", wid="W1", purpose="Оплата: съемка: Осень"),
                     pay("P2", "10.00", wid="W2", purpose="по w2")])
        self.assertEqual(kinds(rep, "check"), [])


class T08WorkOverpayment(unittest.TestCase):
    def test_capped_at_price_excess_to_advance(self):
        rep = build([contract(amount="50.00")], [work("W1", status="done", completed="2026-03-01")],
                    [pay("P1", "70.00", wid="W1", purpose="W1")])
        w = w_line(rep, "W1")
        self.assertEqual(w.paid, D("50.00"))
        self.assertEqual(w.remaining, D("0.00"))
        self.assertEqual(c_line(rep).unallocated, D("20.00"))
        self.assertTrue(any("W1" in i.text for i in kinds(rep, "overpayment")))


class T09ContractOverpayment(unittest.TestCase):
    def test_overpaid_contract(self):
        rep = build([contract(amount="50.00")], [work("W1")], [pay("P1", "55.00")])
        c = c_line(rep)
        self.assertEqual(c.remaining, D("0"))
        self.assertEqual(c.overpaid, D("5.00"))
        self.assertTrue(any(i.contract_id == "C" for i in kinds(rep, "overpayment")))

    def test_queue_would_overpay_warning(self):
        rep = build([contract(amount="50.00")], [work("W1")],
                    [pay("P1", "40.00"), pay("P2", "20.00", status="queued")])
        c = c_line(rep)
        self.assertEqual(c.overpaid, D("0"))
        self.assertEqual(len(kinds(rep, "overpayment")), 1)
        self.assertIn("очеред", kinds(rep, "overpayment")[0].text)


class T10Queue(unittest.TestCase):
    def test_queue_not_subtracted_from_remaining(self):
        rep = build([contract(amount="50.00")], [work("W1")],
                    [pay("P1", "10.00"), pay("P2", "30.00", status="queued")])
        c = c_line(rep)
        self.assertEqual(c.queued, D("30.00"))
        self.assertEqual(c.remaining, D("40.00"))


class T11DataErrors(unittest.TestCase):
    def errors(self, contracts, works, payments):
        rep = build(contracts, works, payments)
        return rep, kinds(rep, "error")

    def test_done_without_date(self):
        _, errs = self.errors([contract(amount="50.00")], [work("W1", status="done")], [])
        self.assertEqual(len(errs), 1)

    def test_unknown_contract_skipped(self):
        rep, errs = self.errors([contract(amount="50.00")], [work("W1"), work("W9", cid="X")],
                                [pay("P1", "10.00", cid="X")])
        self.assertEqual(len(errs), 2)
        self.assertEqual([w.work_id for w in rep.works], ["W1"])
        self.assertEqual(c_line(rep).paid_net, D("0"))

    def test_payment_on_foreign_work_treated_as_unlinked(self):
        rep, errs = self.errors([contract(amount="50.00"), contract("C2", amount="50.00")],
                                [work("W1"), work("W2", cid="C2")],
                                [pay("P1", "10.00", wid="W2", purpose="W2")])
        self.assertEqual(len(errs), 1)
        self.assertEqual(c_line(rep).unallocated, D("10.00"))
        self.assertEqual(w_line(rep, "W2").paid, D("0"))

    def test_duplicate_ids(self):
        _, errs = self.errors([contract(amount="50.00")], [work("W1")],
                              [pay("P1", "10.00"), pay("P1", "10.00")])
        self.assertEqual(len(errs), 1)

    def test_bad_amounts(self):
        rep, errs = self.errors([contract(amount="50.00")], [work("W1")],
                                [pay("P1", "abc"), pay("P2", "0.00"), pay("P3", "-5.00")])
        self.assertEqual(len(errs), 3)
        self.assertEqual(c_line(rep).paid_net, D("0"))

    def test_refunded_without_date_and_refund_before_paid(self):
        _, errs = self.errors([contract(amount="50.00")], [work("W1")],
                              [pay("P1", "10.00", status="refunded", paid="2026-02-01"),
                               pay("P2", "10.00", status="refunded", paid="2026-02-05", refunded="2026-02-01")])
        self.assertEqual(len(errs), 2)

    def test_paid_without_paid_at_uses_created(self):
        p = pay("P1", "10.00", created="2026-02-01")
        p["paid_at"] = ""
        rep, errs = self.errors([contract(amount="50.00")], [work("W1")], [p])
        self.assertEqual(len(errs), 1)
        self.assertEqual(c_line(rep).paid_net, D("10.00"))

    def test_clean_data_has_no_errors(self):
        rep = report.build_report(*report.load_data(DATA), date(2026, 11, 10))
        self.assertEqual(kinds(rep, "error"), [])


class T12SignedAfterDate(unittest.TestCase):
    def test_contract_signed_later_hidden(self):
        rep = build([contract(signed="2026-07-01")], [work("W1")], [])
        self.assertEqual(rep.contracts, [])
        self.assertEqual(rep.works, [])

    def test_payment_before_signing_flagged(self):
        rep = build([contract(amount="50.00", signed="2026-03-01")], [work("W1")],
                    [pay("P1", "10.00", created="2026-02-01")])
        self.assertEqual(len(kinds(rep, "error")), 1)


class TRealData20261110(unittest.TestCase):
    """Итог по data/ на 10.11.2026, посчитан вручную (см. README, «Сверка»)."""

    @classmethod
    def setUpClass(cls):
        cls.rep = report.build_report(*report.load_data(DATA), date(2026, 11, 10))

    def test_works(self):
        expected = {
            "W-11": ("done", "100000.00", "100000.00", "0.00"),
            "W-12": ("done", "100000.00", "100000.00", "0.00"),
            "W-13": ("cancelled", "100000.00", "0", "0"),
            "W-14": ("done", "100000.00", "100000.00", "0.00"),
            "W-15": ("planned", "100000.00", "0", "100000.00"),
            "W-16": ("planned", "100000.00", "0", "100000.00"),
            "W-21": ("done", "20000.00", "20000.00", "0.00"),
            "W-22": ("done", "30000.00", "30000.00", "0.00"),
            "W-23": ("planned", "40000.00", "0", "40000.00"),
        }
        self.assertEqual([w.work_id for w in self.rep.works], list(expected))
        for wid, (status, price, paid, rem) in expected.items():
            w = w_line(self.rep, wid)
            self.assertEqual((w.status, w.price, w.paid, w.remaining),
                             (status, D(price), D(paid), D(rem)), wid)

    def test_contracts(self):
        d1 = c_line(self.rep, "D-1")
        self.assertEqual(
            (d1.amount, d1.works_total, d1.paid_gross, d1.refunded, d1.paid_net, d1.allocated,
             d1.unallocated, d1.queued, d1.remaining, d1.overpaid),
            tuple(D(x) for x in ("600000", "500000", "365000", "50000", "315000", "300000",
                                 "15000", "150000", "185000", "0")))
        d2 = c_line(self.rep, "D-2")
        self.assertEqual(
            (d2.amount, d2.works_total, d2.paid_net, d2.allocated, d2.unallocated, d2.queued,
             d2.remaining, d2.overpaid),
            tuple(D(x) for x in ("90000", "90000", "75000", "50000", "25000", "0", "15000", "0")))

    def test_attention(self):
        t = texts(self.rep)
        self.assertEqual([i.contract_id for i in kinds(self.rep, "discrepancy")], ["D-1"])
        self.assertIn("P-3", "\n".join(i.text for i in kinds(self.rep, "check")))
        after = "\n".join(i.text for i in kinds(self.rep, "after_date"))
        self.assertIn("P-8", after)
        self.assertIn("W-23", after)
        self.assertEqual(kinds(self.rep, "overpayment"), [])
        self.assertNotIn("P-1 ", t)


class TRealDataLater(unittest.TestCase):
    def test_d2_overpaid_on_12_11(self):
        rep = report.build_report(*report.load_data(DATA), date(2026, 11, 12))
        d2 = c_line(rep, "D-2")
        self.assertEqual(d2.paid_net, D("95000.00"))
        self.assertEqual(d2.overpaid, D("5000.00"))
        self.assertEqual(d2.remaining, D("0"))
        self.assertEqual(w_line(rep, "W-23").paid, D("40000.00"))
        self.assertEqual(d2.unallocated, D("5000.00"))
        self.assertTrue(any(i.contract_id == "D-2" for i in kinds(rep, "overpayment")))

    def test_p2_on_its_paid_day(self):
        rep = report.build_report(*report.load_data(DATA), date(2026, 9, 5))
        d1 = c_line(rep, "D-1")
        self.assertEqual(d1.paid_net, D("350000.00"))
        self.assertEqual(w_line(rep, "W-13").status, "planned")
        self.assertEqual(d1.works_total, D("600000.00"))
        self.assertEqual(kinds(rep, "discrepancy"), [])
        self.assertEqual([c.contract_id for c in rep.contracts], ["D-1"])


class TCli(unittest.TestCase):
    def run_cli(self, *args):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = report.main(["--date", "2026-11-10", "--data", DATA, *args])
        return code, buf.getvalue()

    def test_table(self):
        code, out = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Требует внимания", out)
        self.assertIn("185 000,00", out)
        self.assertIn("W-23", out)

    def test_csv_sections_parse(self):
        code, out = self.run_cli("--format", "csv")
        self.assertEqual(code, 0)
        sections = [s for s in out.strip().split("\n\n")]
        self.assertEqual(len(sections), 3)
        works = list(csv.reader(io.StringIO(sections[0]), delimiter=";"))
        self.assertEqual(len(works), 1 + 9)
        contracts = list(csv.DictReader(io.StringIO(sections[1]), delimiter=";"))
        d1 = next(r for r in contracts if r["договор"] == "D-1")
        self.assertEqual(d1["осталось оплатить"], "185000,00")
        self.assertEqual(d1["не распределено (аванс)"], "15000,00")

    def test_bad_date(self):
        buf = io.StringIO()
        with redirect_stdout(buf), self.assertRaises(SystemExit):
            from contextlib import redirect_stderr
            with redirect_stderr(io.StringIO()):
                report.main(["--date", "10.11.2026"])

    def test_missing_data_dir(self):
        from contextlib import redirect_stderr
        err = io.StringIO()
        with redirect_stderr(err):
            code = report.main(["--date", "2026-11-10", "--data", os.path.join(ROOT, "nope")])
        self.assertEqual(code, 2)


class TReviewFixes(unittest.TestCase):
    """Регрессии по итогам code review."""

    def test_huge_amount_is_data_error_not_crash(self):
        self.assertRaises(ValueError, report.parse_money, "1e30")
        rep = build([contract(amount="50.00")], [work("W1")], [pay("P1", "1e30")])
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_negative_price_and_amount_rejected(self):
        rep = build([contract(amount="50.00"), contract("C2", amount="-1.00")],
                    [work("W1", status="done", completed="2026-03-01"), work("W2", price="-50.00")],
                    [pay("P1", "10.00")])
        self.assertEqual([w.work_id for w in rep.works], ["W1"])
        c = c_line(rep)
        self.assertEqual((c.allocated, c.unallocated), (D("10.00"), D("0.00")))
        self.assertEqual(len(kinds(rep, "error")), 2)

    def test_zero_price_allowed(self):
        rep = build([contract(amount="0.00")], [work("W1", price="0.00")], [])
        self.assertEqual(kinds(rep, "error"), [])

    def test_after_date_notice_respects_status(self):
        rep = build([contract(amount="50.00")], [work("W1", completed="2026-12-01")], [])
        self.assertEqual(kinds(rep, "after_date"), [])
        rep = build([contract(amount="50.00")],
                    [work("W1", status="done", completed="2026-03-01", cancelled="2026-12-01")], [])
        self.assertEqual(kinds(rep, "after_date"), [])

    def test_purpose_empty_title_still_checked(self):
        rep = build([contract(amount="50.00")], [work("W1", title="")],
                    [pay("P1", "10.00", wid="W1", purpose="Материалы")])
        self.assertEqual(len(kinds(rep, "check")), 1)

    def test_purpose_id_is_whole_token(self):
        works = [work("W-1"), work("W-12")]
        rep = build([contract(amount="100.00")], works,
                    [pay("P1", "10.00", wid="W-1", purpose="оплата W-12")])
        self.assertEqual(len(kinds(rep, "check")), 1)
        rep = build([contract(amount="100.00")], works,
                    [pay("P1", "10.00", wid="W-1", purpose="оплата W-1, этап 1")])
        self.assertEqual(kinds(rep, "check"), [])

    def test_hidden_contract_produces_no_errors(self):
        rep = build([contract(amount="50.00"), contract("C2", signed="2026-12-01")],
                    [work("W1"), work("W2", cid="C2", status="done")],
                    [pay("P1", "10.00", cid="C2", created="2026-02-01")])
        self.assertEqual(rep.attention, [])
        self.assertEqual([c.contract_id for c in rep.contracts], ["C"])

    def test_duplicate_after_invalid_row_is_used(self):
        rep = build([contract(amount="50.00")], [work("W1", price="abc"), work("W1", price="50.00")],
                    [pay("P1", "abc"), pay("P1", "10.00")])
        self.assertEqual(w_line(rep, "W1").price, D("50.00"))
        self.assertEqual(c_line(rep).paid_net, D("10.00"))
        self.assertEqual(len(kinds(rep, "error")), 2)

    def test_bad_contract_amount_single_error(self):
        rep = build([contract(amount="600 000")], [work("W1"), work("W2")], [pay("P1", "10.00")])
        self.assertEqual(rep.contracts, [])
        errs = kinds(rep, "error")
        self.assertEqual(len(errs), 1)
        self.assertNotIn("не найден", errs[0].text)

    def test_discrepancy_text_when_works_exceed_contract(self):
        rep = build([contract(amount="40.00")], [work("W1")], [])
        text = kinds(rep, "discrepancy")[0].text
        self.assertIn("больше", text)
        self.assertNotIn("уменьшен", text)

    def test_csv_out_file_has_bom(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "r.csv")
            code = report.main(["--date", "2026-11-10", "--data", DATA, "--format", "csv", "--out", path])
            self.assertEqual(code, 0)
            with open(path, "rb") as f:
                self.assertTrue(f.read().startswith(codecs.BOM_UTF8))


class TReviewFixes2(unittest.TestCase):
    """Регрессии по второму code review."""

    def test_queued_payment_on_unknown_work_flagged(self):
        rep = build([contract(amount="50.00")], [work("W1")],
                    [pay("P1", "10.00", status="queued", wid="ZZZ", purpose="ZZZ")])
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_queued_targeted_payment_purpose_checked(self):
        rep = build([contract(amount="50.00")], [work("W1", title="Логотип")],
                    [pay("P1", "10.00", status="queued", wid="W1", purpose="Материалы")])
        self.assertEqual(len(kinds(rep, "check")), 1)

    def test_refund_before_paid_normalized_to_paid_day(self):
        p = pay("P1", "10.00", status="refunded", created="2026-01-15", paid="2026-03-01", refunded="2026-02-10")
        rep = build([contract(amount="50.00")], [work("W1")], [p], on="2026-03-05")
        c = c_line(rep)
        self.assertEqual((c.paid_gross, c.refunded, c.paid_net, c.queued),
                         (D("10.00"), D("10.00"), D("0.00"), D("0")))

    def test_hidden_contract_duplicates_silent(self):
        rep = build([contract(amount="50.00"), contract("C2", signed="2026-12-01"), contract("C2", signed="2026-12-01")],
                    [work("W1"), work("W2", cid="C2"), work("W2", cid="C2")],
                    [pay("P9", "1.00", cid="C2"), pay("P9", "1.00", cid="C2")])
        self.assertEqual(rep.attention, [])

    def test_csv_formula_injection_neutralized(self):
        rep = build([contract(amount="50.00", contractor="=1+1")], [work("W1", title="@SUM(A1)")], [])
        out = report.render_csv(rep)
        rows = list(csv.reader(io.StringIO(out), delimiter=";"))
        cells = [c for r in rows for c in r]
        self.assertIn("'=1+1", cells)
        self.assertIn("'@SUM(A1)", cells)
        self.assertFalse(any(c[:1] in "=+-@" and c for c in cells))

    def test_queue_warning_also_when_already_overpaid(self):
        rep = build([contract(amount="50.00")], [work("W1")],
                    [pay("P1", "55.00"), pay("P2", "100.00", status="queued")])
        over = kinds(rep, "overpayment")
        self.assertEqual(len(over), 2)
        self.assertTrue(any("очеред" in i.text for i in over))

    def test_bad_amount_then_valid_duplicate_message_consistent(self):
        rep = build([contract(amount="abc"), contract(amount="50.00")], [work("W1")], [pay("P1", "10.00")])
        self.assertEqual(c_line(rep).paid_net, D("10.00"))
        errs = kinds(rep, "error")
        self.assertEqual(len(errs), 1)
        self.assertNotIn("тоже не учтены", errs[0].text)

    def test_title_matched_as_whole_words(self):
        rep = build([contract(amount="50.00")], [work("W1", title="лет")],
                    [pay("P1", "10.00", wid="W1", purpose="Оплата за летом материалы")])
        self.assertEqual(len(kinds(rep, "check")), 1)

    def test_strict_date_and_money_formats(self):
        for bad in ("20261110", "2026-W46-2", "2026-11-10T00:00"):
            with self.assertRaises(ValueError, msg=bad):
                report.parse_date(bad)
        for bad in ("1_000", "١٠٠", "1e3", "+5"):
            with self.assertRaises(ValueError, msg=bad):
                report.parse_money(bad)
        self.assertEqual(report.parse_money("-5.5"), D("-5.50"))
        self.assertEqual(report.parse_money("100"), D("100.00"))

    def test_bad_cli_date_format_rejected(self):
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            report.main(["--date", "20261110", "--data", DATA])

    def test_payment_without_any_date_single_error(self):
        p = pay("P1", "10.00")
        p["created_at"] = p["paid_at"] = ""
        rep = build([contract(amount="50.00")], [work("W1")], [p])
        self.assertEqual(len(kinds(rep, "error")), 1)

    def test_out_to_missing_dir_returns_2(self):
        from contextlib import redirect_stderr
        with redirect_stderr(io.StringIO()):
            code = report.main(["--date", "2026-11-10", "--data", DATA, "--format", "csv",
                                "--out", os.path.join(ROOT, "nope", "r.csv")])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main()
