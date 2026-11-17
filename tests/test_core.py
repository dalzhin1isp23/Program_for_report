import unittest
from datetime import date
from pathlib import Path

from Program_for_report.payreport.allocate import allocate_contract
from Program_for_report.payreport.asof import payment_effect, work_state
from Program_for_report.payreport.loading import load
from Program_for_report.payreport.money import MoneyError, fmt, parse_kop
from Program_for_report.payreport.models import Payment, Work
from Program_for_report.payreport.report import build_report

ON = date(2026, 11, 10)
DATA = Path(__file__).resolve().parents[1] / "data"


def work(wid, price, status="planned", done=None, canc=None, cid="C"):
    return Work(wid, cid, wid, parse_kop(price), status, done, canc, 0)


def pay(pid, amount, work_id=None, paid=None, status="paid", refunded=None, cid="C"):
    return Payment(pid, cid, work_id, "", parse_kop(amount), status, None, paid, refunded, 0)


class Money(unittest.TestCase):
    def test_parse(self):
        for raw, want in [("1234.56", 123456), ("1 234,56", 123456),
                          ("1\u00a0234,5", 123450), ("0", 0), ("-0,01", -1)]:
            self.assertEqual(parse_kop(raw), want)

    def test_reject(self):
        for raw in ["", "abc", "1.234,56", "1.005", "1e3", None]:
            with self.assertRaises(MoneyError):
                parse_kop(raw)

    def test_fmt(self):
        self.assertEqual(fmt(123456789), "1 234 567,89")
        self.assertEqual(fmt(-1), "-0,01")


class AsOf(unittest.TestCase):
    def test_work(self):
        w = work("W", "100", "done", done=date(2026, 11, 11))
        self.assertEqual(work_state(w, ON), "planned")
        self.assertEqual(work_state(w, date(2026, 11, 11)), "done")

    def test_payment(self):
        p = pay("P", "50", paid=date(2026, 9, 2), status="refunded", refunded=date(2026, 9, 10))
        self.assertEqual(payment_effect(p, ON).net_kop, 0)
        self.assertEqual(payment_effect(p, date(2026, 9, 5)).net_kop, 5000)
        q = pay("Q", "150", status="queued")
        self.assertEqual(payment_effect(q, ON).net_kop, 0)
        self.assertEqual(payment_effect(q, ON).queued_kop, 15000)


class Allocation(unittest.TestCase):
    def test_waterfall_and_cap(self):
        ws = [work("A", "100", "done", date(2026, 10, 1)), work("B", "100")]
        ps = [pay("P1", "150", paid=date(2026, 9, 1)), pay("P2", "30", "B", date(2026, 9, 2))]
        a = allocate_contract(ws, ps, ON)
        self.assertEqual(a.works["A"].paid_kop, 10000)
        self.assertEqual(a.works["B"].paid_kop, 8000)
        self.assertEqual(sum(x for _, x in a.unallocated), 0)

    def test_linked_overprice(self):
        a = allocate_contract([work("A", "100")], [pay("P", "120", "A", date(2026, 9, 1))], ON)
        self.assertEqual(a.works["A"].paid_kop, 10000)
        self.assertEqual(a.unallocated, [("P", 2000)])

    def test_cancelled_keeps_money_visible(self):
        ws = [work("A", "100", "cancelled", canc=date(2026, 10, 12)), work("B", "100")]
        ps = [pay("P", "40", "A", date(2026, 9, 1))]
        a = allocate_contract(ws, ps, ON)
        self.assertEqual(a.works["A"].paid_kop, 4000)
        self.assertEqual(a.works["A"].remaining_kop, 0)
        self.assertEqual(a.works["B"].paid_kop, 0)

    def test_rounding_one_kopeck(self):
        ws = [work(x, "100") for x in ("A", "B", "C")]
        a = allocate_contract(ws, [pay("P", "0.01", paid=date(2026, 9, 1))], ON, "proportional")
        self.assertEqual(sum(w.paid_kop for w in a.works.values()), 1)

    def test_proportional_exact(self):
        ws = [work(x, "100") for x in ("A", "B", "C")]
        a = allocate_contract(ws, [pay("P", "100", paid=date(2026, 9, 1))], ON, "proportional")
        # 100 ₽ = 10 000 коп на три равные работы: 33,33 + 33,33 + 33,34
        self.assertEqual(sorted(w.paid_kop for w in a.works.values()), [3333, 3333, 3334])
        self.assertEqual(sum(w.paid_kop for w in a.works.values()), 10_000)
        self.assertEqual(a.works["A"].paid_kop, 3334)
        self.assertEqual(a.unallocated, [])


class RealData(unittest.TestCase):
    def setUp(self):
        self.data = load(DATA)

    def test_no_errors(self):
        self.assertFalse(self.data.has_errors,
                         [vars(i) for i in self.data.issues if i.severity == "error"])

    def test_10_nov(self):
        rep = build_report(self.data, ON)
        d1, d2 = rep.contracts
        self.assertEqual(d1.paid_kop, 31_500_000)
        self.assertEqual(d1.remaining_by_amount_kop, 28_500_000)
        self.assertEqual(d1.remaining_by_works_kop, 18_500_000)
        self.assertEqual(d1.queued_kop, 15_000_000)
        self.assertEqual(d1.refunded_kop, 5_000_000)
        self.assertEqual(d2.paid_kop, 7_500_000)
        self.assertEqual(d2.remaining_by_amount_kop, 1_500_000)
        w = {x.work_id: x for x in rep.works}
        self.assertEqual(w["W-14"].linked_kop, 1_500_000)
        self.assertEqual(w["W-14"].pool_kop, 8_500_000)
        self.assertEqual(w["W-15"].remaining_kop, 8_500_000)
        self.assertEqual(w["W-23"].state, "planned")
        self.assertEqual(w["W-23"].paid_kop, 2_500_000)

    def test_17_nov_overpay(self):
        d2 = build_report(self.data, date(2026, 11, 17)).contracts[1]
        self.assertEqual(d2.paid_kop, 9_500_000)
        self.assertEqual(d2.overpaid_kop, 500_000)
        self.assertEqual(d2.unallocated_kop, 500_000)

    def test_before_everything(self):
        rep = build_report(self.data, date(2026, 8, 1))
        self.assertEqual(sum(c.paid_kop for c in rep.contracts), 0)

    def test_strategies_same_contract_totals(self):
        a = build_report(self.data, ON, "fifo")
        b = build_report(self.data, ON, "proportional")
        for x, y in zip(a.contracts, b.contracts):
            self.assertEqual(x.paid_kop, y.paid_kop)
            self.assertEqual(x.remaining_by_amount_kop, y.remaining_by_amount_kop)

    def test_proportional_real_data(self):
        rep = build_report(self.data, ON, "proportional")
        w = {x.work_id: x for x in rep.works}
        self.assertEqual(w["W-11"].paid_kop, 6_185_567)   # 61 855,67 вместо 100 000 при fifo
        self.assertEqual(w["W-14"].paid_kop, 6_757_732)   # 15 000 привязанных + доля пула
        self.assertEqual(sum(x.paid_kop for x in rep.works), 39_000_000)


if __name__ == "__main__":
    unittest.main()