"""Отчёт по оплате работ и договоров на дату.

    python report.py --date 2026-11-10 [--data data] [--format table|csv]

Правила расчёта описаны в DECISIONS.md (номера пунктов в комментариях: «п. N»).
Только стандартная библиотека, деньги — Decimal.
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
ZERO = Decimal("0")

STATUS_RU = {"done": "принята", "planned": "запланирована", "cancelled": "отменена"}
KIND_ORDER = ("error", "overpayment", "discrepancy", "check", "after_date")
KIND_RU = {
    "error": "Ошибка данных",
    "overpayment": "Переплата",
    "discrepancy": "Расхождение",
    "check": "Проверить",
    "after_date": "После даты отчёта",
}


# ---------- разбор входа ----------

MONEY_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Потолок одной суммы: триллион рублей. Итоги из миллионов таких строк остаются в 28 знаках
# точности Decimal, поэтому сложение и вывод точные и не падают.
MAX_MONEY = Decimal("1000000000000")


def parse_money(text: str) -> Decimal:
    """Строка «1234.56» → Decimal с копейками, ROUND_HALF_UP (п. 13). Без float, только ASCII-цифры."""
    if not isinstance(text, str) or not MONEY_RE.fullmatch(text.strip()):
        raise ValueError(f"не сумма: {text!r}")
    value = Decimal(text.strip())
    if abs(value) > MAX_MONEY:
        raise ValueError(f"слишком большая сумма: {text!r}")
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def parse_date(text: str) -> date | None:
    """Строго ГГГГ-ММ-ДД; пусто → None."""
    text = (text or "").strip()
    if not text:
        return None
    if not DATE_RE.fullmatch(text):
        raise ValueError(f"не дата ГГГГ-ММ-ДД: {text!r}")
    return date.fromisoformat(text)


def quiet_date(text: str) -> date | None:
    """Как parse_date, но нераспознанная дата → None без ошибки (её отметит тот, кто ею пользуется)."""
    try:
        return parse_date(text)
    except ValueError:
        return None


def cli_date(text: str) -> date:
    try:
        value = parse_date(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from None
    if value is None:
        raise argparse.ArgumentTypeError("пустая дата")
    return value


def fmt_date(d: date | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "—"


def fmt_money(value: Decimal, sep: str = " ") -> str:
    """300000.5 → «300 000,50» (таблица) или «300000,50» (csv)."""
    value = value.quantize(CENT, rounding=ROUND_HALF_UP)
    sign = "-" if value < 0 else ""
    whole, frac = f"{abs(value):.2f}".split(".")
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    return f"{sign}{sep.join(groups)},{frac}"


def norm(text: str) -> str:
    return (text or "").casefold().replace("ё", "е")


# ---------- модель ----------

@dataclass
class Issue:
    kind: str          # error | overpayment | discrepancy | check | after_date
    contract_id: str
    text: str


@dataclass
class WorkLine:
    contract_id: str
    work_id: str
    title: str
    status: str        # на дату отчёта: done | planned | cancelled
    price: Decimal
    paid: Decimal = ZERO
    remaining: Decimal = ZERO
    completed_at: date | None = None


@dataclass
class ContractLine:
    contract_id: str
    contractor: str
    amount: Decimal
    works_total: Decimal = ZERO   # сумма действующих (не отменённых) работ
    paid_gross: Decimal = ZERO    # ушло на дату
    refunded: Decimal = ZERO      # вернулось на дату
    paid_net: Decimal = ZERO      # нетто = ушло − вернулось
    allocated: Decimal = ZERO     # распределено по работам
    unallocated: Decimal = ZERO   # не распределено (аванс)
    queued: Decimal = ZERO        # в очереди на оплату
    remaining: Decimal = ZERO     # осталось оплатить
    overpaid: Decimal = ZERO


@dataclass
class Report:
    report_date: date
    works: list[WorkLine] = field(default_factory=list)
    contracts: list[ContractLine] = field(default_factory=list)
    attention: list[Issue] = field(default_factory=list)


# ---------- загрузка ----------

FILES = {
    "contracts": ("contracts.csv", ["contract_id", "contractor", "amount", "signed_at"]),
    "works": ("works.csv", ["work_id", "contract_id", "title", "price", "status", "completed_at", "cancelled_at"]),
    "payments": ("payments.csv", ["payment_id", "contract_id", "work_id", "purpose", "amount", "status",
                                  "created_at", "paid_at", "refunded_at"]),
}


def load_data(data_dir: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Читает три CSV как есть (строки). Нет файла или колонки → исключение (п. 11)."""
    result = []
    for name, (filename, columns) in FILES.items():
        path = os.path.join(data_dir, filename)
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            missing = [c for c in columns if c not in (reader.fieldnames or [])]
            if missing:
                raise ValueError(f"{filename}: нет колонок {', '.join(missing)}")
            result.append([{k: (v or "").strip() for k, v in row.items() if k} for row in reader])
    return result[0], result[1], result[2]


# ---------- расчёт ----------

def build_report(contracts: list[dict], works: list[dict], payments: list[dict], report_date: date) -> Report:
    rep = Report(report_date)
    issues = rep.attention
    R = report_date

    def err(cid: str, text: str) -> None:
        issues.append(Issue("error", cid, text))

    def safe_date(row: dict, col: str, rid: str, cid: str) -> date | None:
        try:
            return parse_date(row.get(col, ""))
        except ValueError:
            err(cid, f"{rid}: дата {col} «{row.get(col)}» не в формате ГГГГ-ММ-ДД — поле не учтено")
            return None

    def money_or_none(row: dict, col: str, rid: str, cid: str, positive: bool) -> Decimal | None:
        """positive=True: сумма > 0 (платёж); иначе ≥ 0 (цена, сумма договора)."""
        try:
            value = parse_money(row.get(col, ""))
        except ValueError:
            err(cid, f"{rid}: сумма «{row.get(col)}» не распознана — строка не учтена")
            return None
        if value < 0 or (positive and value == 0):
            err(cid, f"{rid}: сумма {fmt_money(value)} {'≤' if positive else '<'} 0 — строка не учтена")
            return None
        return value

    # --- договоры. Ошибки копим по договору и показываем только для видимых на дату (п. 12)
    all_contracts: dict[str, dict] = {}
    first_signed: dict[str, date | None] = {}   # дата подписания из первой строки договора
    contract_errors: dict[str, list[Issue]] = {}
    for row in contracts:
        cid = row["contract_id"]
        start = len(issues)
        if cid in all_contracts:
            err(cid, f"договор {cid} повторяется в contracts.csv — взята первая корректная строка")
        else:
            first_signed.setdefault(cid, quiet_date(row.get("signed_at", "")))
            amount = money_or_none(row, "amount", f"договор {cid}", cid, positive=False)
            if amount is not None:
                all_contracts[cid] = {"row": row, "amount": amount,
                                      "signed": safe_date(row, "signed_at", cid, cid)}
        contract_errors.setdefault(cid, []).extend(issues[start:])
        del issues[start:]

    lines: dict[str, ContractLine] = {}
    for cid in contract_errors:
        c = all_contracts.get(cid)
        if not is_visible(c["signed"] if c else first_signed[cid], R):
            continue
        errors = contract_errors[cid]
        if c:
            lines[cid] = ContractLine(cid, c["row"]["contractor"], c["amount"])
        elif errors:   # их работы и платежи молча пропускаем
            errors[0].text += "; работы и платежи по договору тоже не учтены"
        issues.extend(errors)

    # строки договоров, которых на дату нет или которые не разобрались, дальше не участвуют
    works = [r for r in works if r["contract_id"] in lines or r["contract_id"] not in contract_errors]
    payments = [r for r in payments if r["contract_id"] in lines or r["contract_id"] not in contract_errors]

    def contract_usable(cid: str, what: str) -> bool:
        if cid in lines:
            return True
        err(cid, f"{what}: договор {cid or '(пусто)'} не найден — строка не учтена")
        return False

    # --- работы
    work_lines: dict[str, WorkLine] = {}
    work_contract: dict[str, str] = {}   # корректные работы
    unpriced: dict[str, str] = {}        # работы с нераспознанной ценой → договор (п. 11)
    for row in works:
        wid, cid = row["work_id"], row["contract_id"]
        if wid in work_contract:
            err(cid, f"работа {wid} повторяется в works.csv — взята первая корректная строка")
            continue
        if not contract_usable(cid, f"работа {wid}"):
            continue
        price = money_or_none(row, "price", f"работа {wid}", cid, positive=False)
        if price is None:
            unpriced.setdefault(wid, cid)
            continue
        unpriced.pop(wid, None)
        work_contract[wid] = cid
        # дату, от которой зависит статус, проверяет work_status_at — одна ошибка на одно поле
        completed = (quiet_date(row["completed_at"]) if row["status"] == "done"
                     else safe_date(row, "completed_at", wid, cid))
        cancelled = (quiet_date(row["cancelled_at"]) if row["status"] == "cancelled"
                     else safe_date(row, "cancelled_at", wid, cid))
        status_at = work_status_at(row, completed, cancelled, cid, R, err)
        final = {"done": (completed, "принята"), "cancelled": (cancelled, "отменена")}.get(row["status"])
        if final and final[0] and final[0] > R:
            issues.append(Issue("after_date", cid, f"{wid} «{row['title']}» {final[1]} {fmt_date(final[0])} — "
                                                   f"после даты отчёта, на дату не учтено"))
        work_lines[wid] = WorkLine(cid, wid, row["title"], status_at, price, completed_at=completed)

    # --- платежи
    targeted: dict[str, Decimal] = {}        # work_id → нетто целевых платежей
    targeted_by: dict[str, list[str]] = {}   # work_id → номера платежей
    pool: dict[str, Decimal] = {cid: ZERO for cid in lines}   # нераспределённые деньги договора
    seen_payments: dict[str, str] = {}   # payment_id → договор
    for row in payments:
        pid, cid, wid = row["payment_id"], row["contract_id"], row.get("work_id", "")
        if pid in seen_payments:
            err(cid, f"платёж {pid} повторяется в payments.csv — взята первая корректная строка")
            continue
        if not contract_usable(cid, f"платёж {pid}"):
            continue
        amount = money_or_none(row, "amount", f"платёж {pid}", cid, positive=True)
        if amount is None:
            continue
        state = payment_state_at(row, pid, cid, R, err, safe_date, all_contracts[cid]["signed"])
        if state is None:
            continue
        seen_payments[pid] = cid
        exists, created, paid_date, refund_date = state

        # события позже даты отчёта (п. 9): не считаем, но показываем
        later = [(d, w) for d, w in ((paid_date, "оплачен"), (refund_date, "возвращён")) if d and d > R]
        if not exists and later == []:
            later = [(created, "заведён")]
        if later:
            desc = ", ".join(f"{w} {fmt_date(d)}" for d, w in later)
            issues.append(Issue("after_date", cid, f"{pid} «{row['purpose']}» {fmt_money(amount)} ₽: {desc} — "
                                                   f"после даты отчёта, на дату не учтено"))
        if not exists:
            continue

        line = lines[cid]
        is_paid = paid_date is not None and paid_date <= R
        is_refunded = is_paid and refund_date is not None and refund_date <= R

        # привязка к работе (п. 6–7, 11) — проверяем и платежи в очереди, пока их можно поправить
        if wid and unpriced.get(wid) == cid:
            wid = ""   # ошибка цены уже показана, деньги идут в общий пул договора
        elif wid and work_contract.get(wid) != cid:
            where = f"относится к договору {work_contract[wid]}" if wid in work_contract else "не найдена"
            err(cid, f"платёж {pid}: работа {wid} {where} — платёж считаю непривязанным к работе")
            wid = ""
        if wid and not is_refunded and not purpose_mentions_work(row["purpose"], wid, work_lines[wid].title):
            w = work_lines[wid]
            issues.append(Issue("check", cid, f"{pid} {fmt_money(amount)} ₽"
                                              f"{'' if is_paid else ' (в очереди)'} привязан к {wid} «{w.title}», "
                                              f"но назначение «{row['purpose']}» — учтён как оплата работы. "
                                              f"Подтвердить: это оплата работы или отдельный расход?"))

        if not is_paid:
            line.queued += amount       # п. 2: заведён, но не ушёл
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

    # --- распределение по работам (п. 4, 6, 8)
    by_contract: dict[str, list[WorkLine]] = {cid: [] for cid in lines}
    for w in work_lines.values():
        by_contract[w.contract_id].append(w)
    for cid, line in lines.items():
        own = by_contract[cid]
        for w in own:
            got = targeted.get(w.work_id, ZERO)
            if w.status == "cancelled":
                w.paid = ZERO
                if got:
                    pool[cid] += got
                    issues.append(Issue("check", cid, f"{', '.join(targeted_by[w.work_id])}: целевая оплата "
                                                      f"{fmt_money(got)} ₽ на отменённую работу {w.work_id} — "
                                                      f"перенесена в нераспределённый аванс"))
                continue
            w.paid = min(got, w.price)
            if got > w.price:
                pool[cid] += got - w.price
                issues.append(Issue("overpayment", cid, f"{w.work_id} «{w.title}»: целевых платежей "
                                                        f"{fmt_money(got)} ₽ при цене {fmt_money(w.price)} ₽ — "
                                                        f"излишек {fmt_money(got - w.price)} ₽ перенесён в аванс"))
        accepted = sorted((w for w in own if w.status == "done"),
                          key=lambda w: (w.completed_at or date.min, w.work_id))
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
        line.remaining = max(line.works_total - line.paid_net, ZERO)   # п. 10
        no_price = sorted(wid for wid, c in unpriced.items() if c == cid)
        if not no_price:
            line.overpaid = max(line.paid_net - line.works_total, ZERO)   # п. 9

        if no_price:   # сумма работ неполная: сравнивать её с договором и выплатами нельзя
            err(cid, f"{cid}: у работ {', '.join(no_price)} цена с ошибкой (см. выше) — сумма работ и «осталось» "
                     f"по договору неполные, переплату и расхождение с договором не проверяю; "
                     f"платежи на эти работы учтены как непривязанные")
        elif line.amount != line.works_total:                          # п. 5
            cancelled = sum((w.price for w in own if w.status == "cancelled"), ZERO)
            extra = f" (отменено работ на {fmt_money(cancelled)} ₽)" if cancelled else ""
            question = ("договор уменьшен (допсоглашение) или будет ещё работа?"
                        if line.works_total < line.amount else
                        "работ больше суммы договора — договор увеличен или цены работ неверны?")
            issues.append(Issue("discrepancy", cid, f"{cid}: сумма договора {fmt_money(line.amount)} ₽, а действующих "
                                                    f"работ {fmt_money(line.works_total)} ₽{extra}. «Осталось» "
                                                    f"считаю от суммы работ. Подтвердить: {question}"))
        if line.overpaid and not no_price:
            issues.append(Issue("overpayment", cid, f"{cid}: выплачено {fmt_money(line.paid_net)} ₽ при сумме работ "
                                                    f"{fmt_money(line.works_total)} ₽ — переплата "
                                                    f"{fmt_money(line.overpaid)} ₽"))
        if not no_price and line.queued and line.paid_net + line.queued > line.works_total:
            over = line.paid_net + line.queued - line.works_total
            issues.append(Issue("overpayment", cid, f"{cid}: если оплатить очередь ({fmt_money(line.queued)} ₽), "
                                                    f"будет переплата {fmt_money(over)} ₽ — проверить до оплаты"))

    rep.contracts = list(lines.values())
    rep.works = list(work_lines.values())
    rep.attention = sorted(issues, key=lambda i: KIND_ORDER.index(i.kind))
    return rep


def is_visible(signed: date | None, R: date) -> bool:
    return signed is None or signed <= R


def purpose_mentions_work(purpose: str, work_id: str, title: str) -> bool:
    """В назначении есть номер работы (целым словом) или её непустое название (п. 7)."""
    text = norm(purpose)
    if re.search(rf"(?<![\w-]){re.escape(norm(work_id))}(?![\w-])", text):
        return True
    title = norm(title).strip()
    return bool(title) and re.search(rf"(?<!\w){re.escape(title)}(?!\w)", text) is not None


def missing_date(row: dict, col: str) -> str:
    raw = row.get(col, "")
    return f"дата {col} «{raw}» не в формате ГГГГ-ММ-ДД" if raw else f"нет даты {col}"


def work_status_at(row, completed, cancelled, cid, R, err) -> str:
    """Статус работы на дату (п. 3, 11). Дату своего статуса проверяет здесь, а не в safe_date."""
    status, wid = row["status"], row["work_id"]
    if status == "done":
        if completed is None:
            err(cid, f"работа {wid}: статус done, {missing_date(row, 'completed_at')} — считаю принятой на любую дату")
            return "done"
        return "done" if completed <= R else "planned"
    if status == "cancelled":
        if cancelled is None:
            err(cid, f"работа {wid}: статус cancelled, {missing_date(row, 'cancelled_at')} — "
                     f"считаю отменённой на любую дату")
            return "cancelled"
        if cancelled <= R:
            return "cancelled"
        # отменена после даты отчёта: на дату была принята, если приёмка была раньше
        return "done" if completed and completed <= R else "planned"
    if status != "planned":
        err(cid, f"работа {wid}: неизвестный статус «{status}» — считаю запланированной")
    elif completed or cancelled:
        err(cid, f"работа {wid}: статус planned, но заполнена дата приёмки/отмены — верю статусу")
    return "planned"


def payment_state_at(row, pid, cid, R, err, safe_date, signed):
    """(существует на дату, дата заведения, дата оплаты, дата возврата) или None, если платёж не учитывается (п. 2, 11)."""
    status = row["status"]
    created = safe_date(row, "created_at", pid, cid)
    paid = safe_date(row, "paid_at", pid, cid)
    refunded = safe_date(row, "refunded_at", pid, cid)
    if status not in ("paid", "queued", "refunded"):
        err(cid, f"платёж {pid}: неизвестный статус «{status}» — платёж не учтён")
        return None
    if status == "queued":
        paid = refunded = None   # верю статусу; лишние даты отмечаю ниже, если платёж учтён
    if paid is None and created is None:
        what = "заведения (статус queued)" if status == "queued" else "ни заведения, ни оплаты"
        err(cid, f"платёж {pid}: нет корректной даты {what} — платёж не учтён")
        return None
    if status == "queued":
        if row.get("paid_at") or row.get("refunded_at"):
            err(cid, f"платёж {pid}: статус queued, но заполнена дата оплаты/возврата — верю статусу")
    else:
        if paid is None:
            paid = created
            err(cid, f"платёж {pid}: статус {status} без даты оплаты — беру дату заведения {fmt_date(created)}")
        if status == "refunded":
            if refunded is None:
                refunded = paid
                err(cid, f"платёж {pid}: статус refunded без даты возврата — считаю возвращённым в день оплаты")
            elif refunded < paid:
                err(cid, f"платёж {pid}: возврат {fmt_date(refunded)} раньше оплаты {fmt_date(paid)} — "
                         f"считаю возвращённым в день оплаты")
                refunded = paid
        elif refunded:
            err(cid, f"платёж {pid}: статус paid, но заполнена дата возврата — верю статусу")
            refunded = None
    if created and paid and paid < created:
        err(cid, f"платёж {pid}: оплачен {fmt_date(paid)} раньше, чем заведён {fmt_date(created)}")
    start = min(d for d in (created, paid) if d)
    if signed and start < signed:
        err(cid, f"платёж {pid} от {fmt_date(start)} раньше подписания договора {fmt_date(signed)}")
    return start <= R, created, paid, refunded


# ---------- вывод ----------

WORK_COLS = ["договор", "работа", "название", "статус на дату", "цена", "выплачено", "осталось"]
CONTRACT_COLS = ["договор", "подрядчик", "сумма договора", "сумма работ", "ушло", "возвращено",
                 "выплачено всего (нетто)", "распределено по работам", "не распределено (аванс)",
                 "в очереди на оплату", "осталось оплатить", "переплата"]
ATTENTION_COLS = ["тип", "договор", "что"]


CONTRACT_MONEY = ["amount", "works_total", "paid_gross", "refunded", "paid_net", "allocated", "unallocated",
                  "queued", "remaining", "overpaid"]   # в порядке CONTRACT_COLS[2:]


def work_rows(rep: Report, sep: str) -> list[list[str]]:
    return [[w.contract_id, w.work_id, w.title, STATUS_RU[w.status], fmt_money(w.price, sep),
             fmt_money(w.paid, sep), fmt_money(w.remaining, sep)] for w in rep.works]


def csv_safe(cell: str) -> str:
    """Текст, который Excel принял бы за формулу, экранируем апострофом."""
    return "'" + cell if cell[:1] in ("=", "+", "-", "@", "\t", "\r") else cell


def render_csv(rep: Report) -> str:
    out = io.StringIO()
    contracts = [[c.contract_id, c.contractor, *(fmt_money(getattr(c, a), "") for a in CONTRACT_MONEY)]
                 for c in rep.contracts]
    attention = [[KIND_RU[i.kind], i.contract_id, i.text] for i in rep.attention]
    for cols, data in ((WORK_COLS, work_rows(rep, "")), (CONTRACT_COLS, contracts), (ATTENTION_COLS, attention)):
        if out.tell():
            out.write("\n")
        writer = csv.writer(out, delimiter=";", lineterminator="\n")
        writer.writerow(cols)
        writer.writerows([csv_safe(str(v)) for v in r] for r in data)
    return out.getvalue()


def table(cols: list[str], data: list[list[str]], right: set[int]) -> str:
    widths = [max(len(str(r[i])) for r in [cols, *data]) for i in range(len(cols))]
    fmt = lambda r: "  ".join(str(v).rjust(widths[i]) if i in right else str(v).ljust(widths[i])  # noqa: E731
                              for i, v in enumerate(r)).rstrip()
    line = "  ".join("-" * w for w in widths)
    return "\n".join([fmt(cols), line, *(fmt(r) for r in data)])


def render_table(rep: Report) -> str:
    parts = [f"Отчёт на {fmt_date(rep.report_date)} (события этого дня включены)", "",
             "РАБОТЫ", table(WORK_COLS, work_rows(rep, " "), {4, 5, 6}), "", "ДОГОВОРЫ"]
    labels = ["сумма договора", "сумма действующих работ", "ушло подрядчику", "вернул подрядчик",
              "выплачено всего (нетто)", "  распределено по работам", "  не распределено (аванс)",
              "в очереди на оплату", "осталось оплатить", "переплата"]
    for c in rep.contracts:
        parts.append(f"{c.contract_id} — {c.contractor}")
        for label, attr in zip(labels, CONTRACT_MONEY):
            parts.append(f"  {label:<28}{fmt_money(getattr(c, attr)):>16}")
        parts.append("")
    parts.append("Требует внимания:" if rep.attention else "Требует внимания: ничего")
    for i in rep.attention:
        parts.append(f"  [{KIND_RU[i.kind]}] {i.text}")
    return "\n".join(parts) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Отчёт по оплате работ и договоров на дату")
    parser.add_argument("--date", required=True, type=cli_date, help="дата отчёта, ГГГГ-ММ-ДД")
    parser.add_argument("--data", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"),
                        help="папка с contracts.csv, works.csv, payments.csv")
    parser.add_argument("--format", choices=("table", "csv"), default="table")
    parser.add_argument("--out", help="записать в файл (UTF-8 с BOM — Excel откроет кириллицу) вместо консоли")
    args = parser.parse_args(argv)
    try:
        data = load_data(args.data)
    except (OSError, ValueError, csv.Error) as e:
        print(f"Ошибка чтения данных: {e}", file=sys.stderr)
        return 2
    rep = build_report(*data, args.date)
    text = render_csv(rep) if args.format == "csv" else render_table(rep)
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8-sig", newline="") as f:
                f.write(text)
        except OSError as e:
            print(f"Не удалось записать {args.out}: {e}", file=sys.stderr)
            return 2
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    sys.exit(main())
