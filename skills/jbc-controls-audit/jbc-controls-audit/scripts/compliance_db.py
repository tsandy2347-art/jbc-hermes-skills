"""Read-only loader for the jbc-compliance Postgres DB.

Feeds the paid-invoice-consistency detector with everything it needs to
cross-check a Xero bill against the compliance ticket that created it:

  - Ticket -> Xero bill linkage (from TicketEvent.eventType=XERO_UPLOADED,
    where `data->>'billId'` is the Xero InvoiceID — this is the exact-link
    join surface, set by triggerXeroForApprovedTicket in the hub).
  - Extracted invoice payload (from the matching INVOICE_PROCESSED event):
    supplier name/abn, invoice number/date, total amount.
  - Supplier identity + status + per-type compliance dateDue (for the
    "compliance lapsed at invoice date" sub-check).
  - Tag presence ('approved', 'business-invoice', 'returned-by-finance')
    and whether a RETURNED_TO_CP event landed AFTER the XERO_UPLOADED
    (the "paid but returned" sub-check).
  - Entity name -> SC/CQ code mapping (Entity model is `@@map("Region")`).

Connects via COMPLIANCE_DATABASE_URL. Returns None when the env var is
absent so the detector becomes a silent no-op rather than failing the
whole audit run.
"""

from __future__ import annotations

import datetime as _dt
import os
from dataclasses import dataclass, field
from typing import Any


ENTITY_NAME_TO_CODE = {
    "Sunshine Coast": "SC",
    "Central Queensland": "CQ",
}


@dataclass
class SupplierRow:
    id: str
    name: str
    abn: str | None
    status: str
    entity_code: str


@dataclass
class ComplianceRow:
    supplier_id: str
    type: str
    date_due: _dt.datetime | None
    not_applicable: bool
    docs_on_file: bool
    approval_status: str


@dataclass
class TicketLink:
    ticket_id: str
    ticket_number: int
    entity_code: str
    supplier_id: str | None
    supplier_name_extracted: str | None
    supplier_abn_extracted: str | None
    invoice_number: str | None
    invoice_date: _dt.datetime | None
    extracted_total: float | None
    xero_bill_id: str
    xero_bill_number: str | None
    xero_uploaded_at: _dt.datetime
    is_business_invoice: bool
    has_approved_tag: bool
    returned_after_xero: bool


@dataclass
class ComplianceSnapshot:
    entity_id_to_code: dict[str, str] = field(default_factory=dict)
    tickets_by_billid: dict[tuple[str, str], TicketLink] = field(default_factory=dict)
    all_links: list[TicketLink] = field(default_factory=list)
    suppliers: dict[str, SupplierRow] = field(default_factory=dict)
    compliance_by_supplier: dict[str, list[ComplianceRow]] = field(default_factory=dict)


def configured() -> bool:
    return bool(os.environ.get("COMPLIANCE_DATABASE_URL"))


def _connect():
    url = os.environ.get("COMPLIANCE_DATABASE_URL")
    if not url:
        return None
    try:
        import psycopg  # v3
        return psycopg.connect(url)
    except ImportError:
        import psycopg2  # v2 fallback
        return psycopg2.connect(url)


def load_snapshot() -> ComplianceSnapshot | None:
    conn = _connect()
    if conn is None:
        return None
    try:
        return _load(conn)
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass


def _load(conn) -> ComplianceSnapshot:
    snap = ComplianceSnapshot()

    with conn.cursor() as cur:
        cur.execute('SELECT id, name FROM "Region"')
        for eid, name in cur.fetchall():
            code = ENTITY_NAME_TO_CODE.get(name)
            if code:
                snap.entity_id_to_code[eid] = code

    if not snap.entity_id_to_code:
        return snap

    entity_ids = list(snap.entity_id_to_code.keys())

    with conn.cursor() as cur:
        cur.execute(
            'SELECT id, name, abn, status, "supplierRegionId" FROM "Supplier" '
            'WHERE "supplierRegionId" = ANY(%s)',
            (entity_ids,),
        )
        for sid, name, abn, status, eid in cur.fetchall():
            snap.suppliers[sid] = SupplierRow(
                id=sid,
                name=name,
                abn=abn,
                status=status,
                entity_code=snap.entity_id_to_code[eid],
            )

    if snap.suppliers:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT "supplierId", type, "dateDue", "notApplicable", '
                '       "docsOnFile", "approvalStatus" '
                '  FROM "SupplierCompliance" WHERE "supplierId" = ANY(%s)',
                (list(snap.suppliers.keys()),),
            )
            for sid, ctype, due, na, docs, appr in cur.fetchall():
                snap.compliance_by_supplier.setdefault(sid, []).append(
                    ComplianceRow(
                        supplier_id=sid,
                        type=ctype,
                        date_due=due,
                        not_applicable=bool(na),
                        docs_on_file=bool(docs),
                        approval_status=appr,
                    )
                )

    sql = """
        SELECT
          t.id,
          t.number,
          t."entityId",
          t."supplierId",
          xu."createdAt" AS xero_uploaded_at,
          xu.data        AS xero_data,
          ip.data        AS extracted_data,
          (SELECT EXISTS (
              SELECT 1 FROM "TicketTagAssignment" tta
              JOIN "TicketTag" tag ON tag.id = tta."tagId"
              WHERE tta."ticketId" = t.id AND tag.name = 'approved'
          )) AS has_approved_tag,
          (SELECT EXISTS (
              SELECT 1 FROM "TicketTagAssignment" tta
              JOIN "TicketTag" tag ON tag.id = tta."tagId"
              WHERE tta."ticketId" = t.id AND tag.name = 'business-invoice'
          )) AS is_business_invoice,
          (SELECT EXISTS (
              SELECT 1 FROM "TicketEvent" rcp
              WHERE rcp."ticketId" = t.id
                AND rcp."eventType" = 'RETURNED_TO_CP'
                AND rcp."createdAt" > xu."createdAt"
          )) AS returned_after_xero
        FROM "Ticket" t
        JOIN LATERAL (
          SELECT e.id, e."createdAt", e.data
            FROM "TicketEvent" e
           WHERE e."ticketId" = t.id
             AND e."eventType" = 'XERO_UPLOADED'
           ORDER BY e."createdAt" DESC
           LIMIT 1
        ) xu ON TRUE
        LEFT JOIN LATERAL (
          SELECT e.data
            FROM "TicketEvent" e
           WHERE e."ticketId" = t.id
             AND e."eventType" = 'INVOICE_PROCESSED'
           ORDER BY e."createdAt" DESC
           LIMIT 1
        ) ip ON TRUE
        WHERE t."entityId" = ANY(%s)
    """
    with conn.cursor() as cur:
        cur.execute(sql, (entity_ids,))
        for row in cur.fetchall():
            (tid, num, eid, sup_id, xu_at, xu_data, ip_data,
             has_appr, is_biz, returned) = row
            code = snap.entity_id_to_code.get(eid)
            if not code:
                continue
            bill_id = (xu_data or {}).get("billId") if isinstance(xu_data, dict) else None
            if not bill_id:
                continue
            bill_number = (xu_data or {}).get("billNumber") if isinstance(xu_data, dict) else None

            extracted_total: float | None = None
            invoice_number: str | None = None
            invoice_date: _dt.datetime | None = None
            supplier_name_x: str | None = None
            supplier_abn_x: str | None = None
            if ip_data and isinstance(ip_data, dict):
                full = ip_data.get("full") or {}
                raw_total = ip_data.get("totalAmount") or full.get("total_amount")
                try:
                    extracted_total = float(raw_total) if raw_total is not None else None
                except (TypeError, ValueError):
                    extracted_total = None
                invoice_number = ip_data.get("invoiceNumber") or full.get("invoice_number")
                raw_date = full.get("invoice_date")
                if raw_date:
                    try:
                        invoice_date = _dt.datetime.fromisoformat(
                            str(raw_date).replace("Z", "+00:00")
                        )
                    except ValueError:
                        invoice_date = None
                supplier_name_x = ip_data.get("supplierName") or full.get("supplier_name")
                supplier_abn_x = ip_data.get("supplierAbn") or full.get("supplier_abn")

            link = TicketLink(
                ticket_id=tid,
                ticket_number=num,
                entity_code=code,
                supplier_id=sup_id,
                supplier_name_extracted=supplier_name_x,
                supplier_abn_extracted=supplier_abn_x,
                invoice_number=invoice_number,
                invoice_date=invoice_date,
                extracted_total=extracted_total,
                xero_bill_id=str(bill_id),
                xero_bill_number=bill_number,
                xero_uploaded_at=xu_at,
                is_business_invoice=bool(is_biz),
                has_approved_tag=bool(has_appr),
                returned_after_xero=bool(returned),
            )
            snap.all_links.append(link)
            snap.tickets_by_billid[(code, link.xero_bill_id)] = link

    return snap
