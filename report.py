"""Отчёт по оплате работ и договоров на дату.

    python report.py --date 2026-11-10 [--data data] [--format table|csv]

Правила расчёта — в DECISIONS.md, в комментариях ссылки «п. N».
Только стандартная библиотека; деньги — Decimal из строк, без float.
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")

STATUS_RU = {"done": "принята", "planned": "запланирована", "cancelled": "отменена"}
KIND_RU = {
    "error": "Ошибка данных",
    "overpayment": "Переплата",
    "discrepancy": "Расхождение",
    "check": "Проверить",
    "after_date": "После даты отчёта",
}
KIND_ORDER = list(KIND_RU)

FILES = {
    "contracts.csv": ["contract_id", "contractor", "amount", "signed_at"],
    "works.csv": ["work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"],
    "payments.csv": ["payment_id", "contract_id", "work_id", "purpose", "amount", "status",
                     "created_at", "paid_at", "refunded_at"],
}


# ---------- значения ----------

MONEY_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?", re.ASCII)
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


def parse_money(text: str) -> Decimal:
    """«1234.56» → Decimal до копейки, ROUND_HALF_UP (п. 13). Другие форматы — ValueError."""
    text = (text or "").strip()
    if not MONEY_RE.fullmatch(text):
        raise ValueError(f"не сумма: «{text}»")
    return Decimal(text).quantize(CENT, rounding=ROUND_HALF_UP)


def parse_date(text: str) -> date:
    text = (text or "").strip()
    if not DATE_RE.fullmatch(text):
        raise ValueError(f"не дата ГГГГ-ММ-ДД: «{text}»")
    return date.fromisoformat(text)


def fmt_money(value: Decimal, sep: str = " ") -> str:
    """1234567.5 → «1 234 567,50»."""
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value.quantize(CENT, rounding=ROUND_HALF_UP)):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return f"{sign}{sep.join(groups)},{frac}"


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def norm(text: str) -> str:
    return (text or "").casefold().replace("ё", "е")


# ---------- модель ----------

@dataclass
class Issue:
    kind: str          # ключ KIND_RU
    contract_id: str
    text: str


@dataclass
class WorkLine:
    contract_id: str
    work_id: str
    title: str
    status: str        # на дату: done | planned | cancelled
    price: Decimal
    completed_at: date | None = None
    paid: Decimal = ZERO
    remaining: Decimal = ZERO


@dataclass
class ContractLine:
    contract_id: str
    contractor: str
    amount: Decimal
    works_total: Decimal = ZERO   # сумма действующих работ
    paid_gross: Decimal = ZERO    # ушло
    refunded: Decimal = ZERO      # вернулось
    paid_net: Decimal = ZERO      # выплачено нетто
    allocated: Decimal = ZERO     # распределено по работам
    unallocated: Decimal = ZERO   # не распределено (аванс)
    queued: Decimal = ZERO        # в очереди
    remaining: Decimal = ZERO     # осталось оплатить
    overpaid: Decimal = ZERO      # переплата


@dataclass
class Report:
    report_date: date
    works: dict[str, WorkLine] = field(default_factory=dict)
    contracts: dict[str, ContractLine] = field(default_factory=dict)
    attention: list[Issue] = field(default_factory=list)


# ---------- загрузка ----------

def load_data(data_dir: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Три CSV как списки словарей строк. Нет файла или колонки — исключение (п. 11)."""
    tables = []
    for filename, columns in FILES.items():
        with open(os.path.join(data_dir, filename), encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c in columns if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"{filename}: нет колонок {', '.join(missing)}")
            tables.append([{k: (v or "").strip() for k, v in row.items() if k} for row in reader])
    return tables[0], tables[1], tables[2]


# ---------- расчёт ----------

class _Builder:
    def __init__(self, report_date: date):
        self.R = report_date
        self.rep = Report(report_date)

    def issue(self, kind: str, cid: str, text: str) -> None:
        self.rep.attention.append(Issue(kind, cid, text))

    def error(self, cid: str, text: str) -> None:
        self.issue("error", cid, text)

    def money(self, row: dict, col: str, what: str, cid: str, positive: bool) -> Decimal | None:
        """Сумма или None с ошибкой. positive: > 0 (платёж), иначе ≥ 0 (п. 11)."""
        try:
            value = parse_money(row[col])
        except ValueError:
            self.error(cid, f"{what}: сумма «{row[col]}» не распознана — строка не учтена")
            return None
        if value < 0 or (positive and value == 0):
            self.error(cid, f"{what}: сумма {fmt_money(value)} {'≤' if positive else '<'} 0 — строка не учтена")
            return None
        return value

    def date_or_none(self, row: dict, col: str, what: str, cid: str, quiet: bool = False) -> date | None:
        """Пусто → None; мусор → None с ошибкой (п. 11). quiet — ошибку покажет тот, чей статус от даты зависит."""
        if not row[col]:
            return None
        try:
            return parse_date(row[col])
        except ValueError:
            if not quiet:
                self.error(cid, f"{what}: {col} «{row[col]}» не в формате ГГГГ-ММ-ДД — считаю, что даты нет")
            return None

    # --- договоры (п. 11, 12)
    def add_contracts(self, rows: list[dict]) -> set[str]:
        """Возвращает все известные номера договоров (в т.ч. скрытые и с ошибкой) — чтобы их строки не считать «неизвестными»."""
        known = set()
        for row in rows:
            cid = row["contract_id"]
            if cid in known:
                self.error(cid, f"договор {cid} повторяется в contracts.csv — взята первая строка")
                continue
            known.add(cid)
            signed = self.date_or_none(row, "signed_at", f"договор {cid}", cid)
            if signed and signed > self.R:
                continue   # п. 12: на дату договора ещё нет
            amount = self.money(row, "amount", f"договор {cid}", cid, positive=False)
            if amount is None:
                self.rep.attention[-1].text += "; работы и платежи по договору тоже не учтены"
                continue
            self.rep.contracts[cid] = ContractLine(cid, row["contractor"], amount)
        return known

    def contract_ok(self, cid: str, what: str, known: set[str]) -> bool:
        if cid in self.rep.contracts:
            return True
        if cid not in known:
            self.error(cid, f"{what}: договор «{cid}» не найден — строка не учтена")
        return False

    # --- работы (п. 3, 4, 11)
    def add_works(self, rows: list[dict], known: set[str]) -> None:
        seen = set()
        for row in rows:
            wid, cid = row["work_id"], row["contract_id"]
            if wid in seen:
                self.error(cid, f"работа {wid} повторяется в works.csv — взята первая строка")
                continue
            seen.add(wid)
            if not self.contract_ok(cid, f"работа {wid}", known):
                continue
            price = self.money(row, "price", f"работа {wid}", cid, positive=False)
            if price is None:
                continue
            status = row["status"]
            completed = self.date_or_none(row, "completed_at", f"работа {wid}", cid, quiet=status == "done")
            cancelled = self.date_or_none(row, "cancelled_at", f"работа {wid}", cid, quiet=status == "cancelled")
            status = self.work_status(row, completed, cancelled)
            self.rep.works[wid] = WorkLine(cid, wid, row["title"], status, price, completed)

    def work_status(self, row: dict, completed: date | None, cancelled: date | None) -> str:
        wid, cid, status, R = row["work_id"], row["contract_id"], row["status"], self.R
        if status == "done":
            if completed is None:
                self.error(cid, f"работа {wid}: статус done, {no_date(row, 'completed_at')} — считаю принятой на любую дату")
                return "done"
            if completed > R:
                self.issue("after_date", cid, f"{wid} «{row['title']}» принята {fmt_date(completed)} — на дату не учтено")
                return "planned"
            return "done"
        if status == "cancelled":
            if cancelled is None:
                self.error(cid, f"работа {wid}: статус cancelled, {no_date(row, 'cancelled_at')} — "
                                f"считаю отменённой на любую дату")
                return "cancelled"
            if cancelled <= R:
                return "cancelled"
            self.issue("after_date", cid, f"{wid} «{row['title']}» отменена {fmt_date(cancelled)} — на дату не учтено")
            return "done" if completed and completed <= R else "planned"
        if status != "planned":
            self.error(cid, f"работа {wid}: неизвестный статус «{status}» — считаю запланированной")
        return "planned"

    # --- платежи (п. 2, 7, 11)
    def add_payments(self, rows: list[dict], known: set[str]) -> tuple[dict[str, Decimal], dict[str, list[str]], dict[str, Decimal]]:
        """Возвращает (нетто целевых по работе, номера целевых платежей по работе, нераспределённое по договору)."""
        targeted: dict[str, Decimal] = {}
        targeted_by: dict[str, list[str]] = {}
        pool = {cid: ZERO for cid in self.rep.contracts}
        seen = set()
        for row in rows:
            pid, cid, wid = row["payment_id"], row["contract_id"], row["work_id"]
            if pid in seen:
                self.error(cid, f"платёж {pid} повторяется в payments.csv — взята первая строка")
                continue
            seen.add(pid)
            if not self.contract_ok(cid, f"платёж {pid}", known):
                continue
            amount = self.money(row, "amount", f"платёж {pid}", cid, positive=True)
            if amount is None:
                continue
            state = self.payment_state(row, amount)
            if state is None:
                continue
            is_paid, is_refunded = state

            if wid and (wid not in self.rep.works or self.rep.works[wid].contract_id != cid):
                where = (f"относится к договору {self.rep.works[wid].contract_id}"
                         if wid in self.rep.works else "не найдена")
                self.error(cid, f"платёж {pid}: работа {wid} {where} — считаю платёж непривязанным")
                wid = ""
            if wid and not is_refunded and not purpose_mentions_work(row["purpose"], wid, self.rep.works[wid].title):
                self.issue("check", cid, f"{pid} {fmt_money(amount)} ₽{'' if is_paid else ' (в очереди)'} привязан к "
                                         f"{wid} «{self.rep.works[wid].title}», а назначение — «{row['purpose']}». "
                                         f"Учтён как оплата работы. Подтвердить: оплата работы или отдельный расход?")

            line = self.rep.contracts[cid]
            if not is_paid:
                line.queued += amount
                continue
            line.paid_gross += amount
            net = amount
            if is_refunded:
                line.refunded += amount
                net = ZERO
            line.paid_net += net
            if wid:
                targeted[wid] = targeted.get(wid, ZERO) + net
                targeted_by.setdefault(wid, []).append(pid)
            else:
                pool[cid] += net
        return targeted, targeted_by, pool

    def payment_state(self, row: dict, amount: Decimal) -> tuple[bool, bool] | None:
        """(ушли ли деньги на дату, вернулись ли) или None, если платежа на дату нет или он не учитывается."""
        pid, cid, status, R = row["payment_id"], row["contract_id"], row["status"], self.R
        what = f"платёж {pid}"
        if status not in ("paid", "queued", "refunded"):
            self.error(cid, f"{what}: неизвестный статус «{status}» — платёж не учтён")
            return None
        created = self.date_or_none(row, "created_at", what, cid, quiet=True)
        paid = self.date_or_none(row, "paid_at", what, cid, quiet=True) if status != "queued" else None
        refunded = self.date_or_none(row, "refunded_at", what, cid, quiet=True) if status == "refunded" else None
        if status != "queued" and paid is None:
            if created is None:
                self.error(cid, f"{what}: статус {status}, {no_date(row, 'paid_at')}, {no_date(row, 'created_at')} — "
                                f"платёж не учтён")
                return None
            self.error(cid, f"{what}: статус {status}, {no_date(row, 'paid_at')} — считаю оплаченным в день "
                            f"заведения {fmt_date(created)}")
            paid = created
        if status == "refunded" and refunded is None:
            self.error(cid, f"{what}: статус refunded, {no_date(row, 'refunded_at')} — считаю возвращённым в день оплаты")
            refunded = paid
        elif refunded and refunded < paid:
            self.error(cid, f"{what}: возврат {fmt_date(refunded)} раньше оплаты {fmt_date(paid)} — "
                            f"считаю возвращённым в день оплаты")
            refunded = paid
        if status == "queued" and created is None:
            self.error(cid, f"{what}: статус queued, {no_date(row, 'created_at')} — считаю заведённым на любую дату")

        start = min(d for d in (created, paid) if d) if (created or paid) else None
        events = [(start, "заведён"), (paid, "оплачен"), (refunded, "возвращён")]
        if start == paid:
            events = events[1:]   # заведён и оплачен в один день — достаточно «оплачен»
        later = [f"{w} {fmt_date(d)}" for d, w in events if d and d > R]
        if later:
            self.issue("after_date", cid, f"{pid} «{row['purpose']}» {fmt_money(amount)} ₽: {', '.join(later)} — "
                                          f"на дату не учтено")
        if start and start > R:
            return None
        is_paid = paid is not None and paid <= R
        return is_paid, is_paid and refunded is not None and refunded <= R

    # --- распределение и итоги (п. 4–6, 8–10)
    def allocate(self, targeted: dict[str, Decimal], targeted_by: dict[str, list[str]], pool: dict[str, Decimal]) -> None:
        for cid, line in self.rep.contracts.items():
            own = [w for w in self.rep.works.values() if w.contract_id == cid]
            for w in own:
                got = targeted.get(w.work_id, ZERO)
                if w.status == "cancelled":
                    if got:
                        pool[cid] += got
                        self.issue("check", cid, f"{', '.join(targeted_by[w.work_id])}: оплата {fmt_money(got)} ₽ "
                                                 f"на отменённую работу {w.work_id} — перенесена в нераспределённый аванс")
                    continue
                w.paid = min(got, w.price)
                if got > w.price:
                    pool[cid] += got - w.price
                    self.issue("overpayment", cid, f"{w.work_id} «{w.title}»: целевых платежей {fmt_money(got)} ₽ при "
                                                   f"цене {fmt_money(w.price)} ₽ — излишек {fmt_money(got - w.price)} ₽ "
                                                   f"перенесён в нераспределённый аванс")
            accepted = sorted((w for w in own if w.status == "done"), key=lambda w: (w.completed_at or date.min, w.work_id))
            for w in accepted:
                take = min(pool[cid], w.price - w.paid)
                if take > 0:
                    w.paid += take
                    pool[cid] -= take
            for w in own:
                w.remaining = ZERO if w.status == "cancelled" else w.price - w.paid

            line.works_total = sum((w.price for w in own if w.status != "cancelled"), ZERO)
            line.allocated = sum((w.paid for w in own), ZERO)
            line.unallocated = line.paid_net - line.allocated
            line.remaining = max(line.works_total - line.paid_net, ZERO)
            line.overpaid = max(line.paid_net - line.works_total, ZERO)

            if line.amount != line.works_total:
                cancelled = sum((w.price for w in own if w.status == "cancelled"), ZERO)
                note = f" (отменено работ на {fmt_money(cancelled)} ₽)" if cancelled else ""
                question = ("договор уменьшен допсоглашением или будет ещё работа?" if line.works_total < line.amount
                            else "договор увеличен или цены работ неверны?")
                self.issue("discrepancy", cid, f"{cid}: сумма договора {fmt_money(line.amount)} ₽, действующих работ "
                                               f"{fmt_money(line.works_total)} ₽{note}. «Осталось» считаю от суммы работ. "
                                               f"Подтвердить: {question}")
            if line.overpaid:
                self.issue("overpayment", cid, f"{cid}: выплачено {fmt_money(line.paid_net)} ₽ при сумме работ "
                                               f"{fmt_money(line.works_total)} ₽ — переплата {fmt_money(line.overpaid)} ₽")
            if line.queued and line.paid_net + line.queued > line.works_total:
                over = line.paid_net + line.queued - line.works_total
                self.issue("overpayment", cid, f"{cid}: если оплатить очередь ({fmt_money(line.queued)} ₽), переплата "
                                               f"составит {fmt_money(over)} ₽ — проверить до оплаты")


def build_report(contracts: list[dict], works: list[dict], payments: list[dict], report_date: date) -> Report:
    b = _Builder(report_date)
    known = b.add_contracts(contracts)
    b.add_works(works, known)
    b.allocate(*b.add_payments(payments, known))
    b.rep.attention.sort(key=lambda i: KIND_ORDER.index(i.kind))
    return b.rep


def no_date(row: dict, col: str) -> str:
    raw = row[col]
    return f"{col} «{raw}» не в формате ГГГГ-ММ-ДД" if raw else f"нет {col}"


def purpose_mentions_work(purpose: str, work_id: str, title: str) -> bool:
    """В назначении есть номер работы или её название — целыми словами, без учёта регистра и ё (п. 7)."""
    text = norm(purpose)
    if re.search(rf"(?<![\w-]){re.escape(norm(work_id))}(?![\w-])", text):
        return True
    title = norm(title).strip()
    return bool(title) and re.search(rf"(?<!\w){re.escape(title)}(?!\w)", text) is not None


# ---------- вывод ----------

WORK_COLS = ["договор", "работа", "название", "статус на дату", "цена", "выплачено", "осталось"]
CONTRACT_FIELDS = [  # (атрибут, подпись)
    ("amount", "сумма договора"),
    ("works_total", "сумма работ (без отменённых)"),
    ("paid_gross", "ушло подрядчику"),
    ("refunded", "вернул подрядчик"),
    ("paid_net", "выплачено всего (нетто)"),
    ("allocated", "распределено по работам"),
    ("unallocated", "не распределено (аванс)"),
    ("queued", "в очереди на оплату"),
    ("remaining", "осталось оплатить"),
    ("overpaid", "переплата"),
]


def work_rows(rep: Report, sep: str) -> list[list[str]]:
    return [[w.contract_id, w.work_id, w.title, STATUS_RU[w.status],
             fmt_money(w.price, sep), fmt_money(w.paid, sep), fmt_money(w.remaining, sep)]
            for w in rep.works.values()]


def render_csv(rep: Report) -> str:
    out = io.StringIO()
    sections = [
        (WORK_COLS, work_rows(rep, "")),
        (["договор", "подрядчик", *(label for _, label in CONTRACT_FIELDS)],
         [[c.contract_id, c.contractor, *(fmt_money(getattr(c, a), "") for a, _ in CONTRACT_FIELDS)]
          for c in rep.contracts.values()]),
        (["тип", "договор", "что"], [[KIND_RU[i.kind], i.contract_id, i.text] for i in rep.attention]),
    ]
    writer = csv.writer(out, delimiter=";", lineterminator="\n")
    for n, (cols, rows) in enumerate(sections):
        if n:
            out.write("\n")
        writer.writerow(cols)
        writer.writerows(rows)
    return out.getvalue()


def table(cols: list[str], rows: list[list[str]], right: set[int]) -> str:
    widths = [max(len(r[i]) for r in [cols, *rows]) for i in range(len(cols))]

    def line(r):
        return "  ".join(v.rjust(widths[i]) if i in right else v.ljust(widths[i]) for i, v in enumerate(r)).rstrip()

    return "\n".join([line(cols), "  ".join("-" * w for w in widths), *(line(r) for r in rows)])


def render_table(rep: Report) -> str:
    parts = [f"Отчёт на {fmt_date(rep.report_date)} (события этого дня включены)", "",
             "РАБОТЫ", table(WORK_COLS, work_rows(rep, " "), {4, 5, 6}), "", "ДОГОВОРЫ"]
    for c in rep.contracts.values():
        parts.append(f"{c.contract_id} — {c.contractor}")
        for attr, label in CONTRACT_FIELDS:
            indent = "    " if attr in ("allocated", "unallocated") else "  "
            parts.append(f"{indent}{label:<{32 - len(indent)}}{fmt_money(getattr(c, attr)):>16}")
        parts.append("")
    parts.append("Требует внимания:" + ("" if rep.attention else " ничего"))
    parts += [f"  [{KIND_RU[i.kind]}] {i.text}" for i in rep.attention]
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Отчёт по оплате работ и договоров на дату")
    parser.add_argument("--date", required=True, help="дата отчёта ГГГГ-ММ-ДД (включительно)")
    parser.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
                        help="папка с contracts.csv, works.csv, payments.csv")
    parser.add_argument("--format", choices=("table", "csv"), default="table")
    args = parser.parse_args(argv)
    try:
        report_date = parse_date(args.date)
    except ValueError as e:
        print(f"Ошибка: --date {e}", file=sys.stderr)
        return 2
    try:
        data = load_data(args.data)
    except (OSError, ValueError, csv.Error) as e:
        print(f"Ошибка чтения данных: {e}", file=sys.stderr)
        return 2
    rep = build_report(*data, report_date)
    sys.stdout.write(render_csv(rep) if args.format == "csv" else render_table(rep))
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main())
