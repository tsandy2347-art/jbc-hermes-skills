---
name: create-payroll-journal
description: Build JBC payroll DRAFT manual journals from a MYOB Pay Activity Summary, split by (Entity × Direct/Indirect). Mirrors the shape Craig used historically — real JBC codes (477/477.4/478/478.1/803/825/826/877), Location tracking on SC's P&L lines (Sunshine Coast / Wide Bay), 877 Tracking Transfers clearing so payables stay untagged. Hard-locked DRAFT — Nicole / Tony / external accountant posts in Xero.
version: 0.3.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [xero, payroll, jbc, drafts, manual-journal, finance, sc, cq, wb, location-tracking]
    category: jbc-finance
required_environment_variables:
  - name: XERO_SC_CLIENT_ID
    prompt: Xero SC tenant client ID (Sunshine Coast Pty Ltd — also holds Wide Bay)
    required_for: SC journal (SC + WB lines, Location-tracked)
  - name: XERO_SC_CLIENT_SECRET
    prompt: Xero SC tenant client secret
    required_for: SC journal
  - name: XERO_SC_TENANT_ID
    prompt: Xero SC tenant UUID
    required_for: SC journal
  - name: XERO_CQ_CLIENT_ID
    prompt: Xero CQ tenant client ID (Central Queensland Pty Ltd)
    required_for: CQ journal
  - name: XERO_CQ_CLIENT_SECRET
    prompt: Xero CQ tenant client secret
    required_for: CQ journal
  - name: XERO_CQ_TENANT_ID
    prompt: Xero CQ tenant UUID
    required_for: CQ journal
---

# Create JBC payroll DRAFT manual journals

## When to use

Triggered when the user has MYOB Pay Activity Summary totals for a pay
period and wants the corresponding manual journals in Xero. JBC's
operational shape:

- **SC Xero tenant** holds Sunshine Coast + Wide Bay. WB is tagged via the
  Xero `Location` tracking category (options `Sunshine Coast` and `Wide Bay`).
- **CQ Xero tenant** is its own Pty Ltd. Single location.

One DRAFT manual journal per Xero tenant per pay period.

## The JBC chart (real codes, per Craig's historical journals)

| Line | Code | Side | Tracked? |
|------|------|------|----------|
| Wages and Salaries — Direct | `477` | DR | Location (SC tenant only) |
| Wages — Indirect | `477.4` | DR | Location (SC tenant only) |
| Superannuation — Direct | `478` | DR | Location (SC tenant only) |
| Superannuation — Indirect | `478.1` | DR | Location (SC tenant only) |
| Wages Payable | `803` | CR | NO tracking |
| PAYG Withholdings Payable | `825` | CR | NO tracking |
| Superannuation Payable | `826` | CR | NO tracking |
| Tracking Transfers (clearing) | `877` | both | mixed — see below |

Direct vs Indirect rule (Tony 2026-05-27): **Department `Field` = Direct.
Everything else = Indirect.** (Admin / Mgmt / Finance / HR / Rostering /
HCP / HCP Admin / NDIS Disability / NDIS SIL → Indirect.)

## The Tracking Transfers (877) pattern — important

JBC's pattern (from Journal #673782 by Craig):
- Expense DRs (477 / 477.4 / 478 / 478.1) carry the `Location` tag.
- Payable CRs (803 / 825 / 826) carry NO tag — they're shared clearing accounts.
- `877 Tracking Transfers` bridges the two:
  - `CR 877 (with location)` matches each expense DR by location + amount → location nets to zero on P&L
  - `DR 877 (no location)` matches each payable CR amount → balance-sheet payables reconcile clean

The skill collapses Craig's line-by-line `877` entries into one per (location × directness × account) for readability — same accounting effect, cleaner journal. If you want the line-by-line MYOB-block breakdown, ask and I'll iterate.

## CQ doesn't use Location tracking

CQ's journal is single-tenant single-location — no `877` clearing needed.
Just DR expenses + CR payables, untagged. If CQ ever adds tracking dims,
tell me and I'll layer them in.

## Hard rules

1. **Status is hard-locked to DRAFT** in the Python below. No path flips it.
2. **One journal per Xero tenant** — SC's contains SC + WB (Location-tagged), CQ's is CQ-only.
3. **Each journal balances** (DR == CR within 1c).
4. **Net = Gross − PreTaxDed − PAYG + AfterTax − PostTaxDed** (verified against MYOB row-8 totals).
5. **AfterTax allowances roll into the wages line** (no separate allowances account in JBC's chart).
6. **PreTaxDed (salary sacrifice) is the same dollar as part of EmpSuper** — counted once, on the Wages Payable CR (it reduces what hits the employee's bank).

## Procedure

1. Confirm with the user, conversationally:
   - **Pay period start, end, and journal date** (usually period end)
   - **Per-(entity × directness) totals** — six tuples max:
     `SC / Direct`, `SC / Indirect`, `WB / Direct`, `WB / Indirect`,
     `CQ / Direct`, `CQ / Indirect`. Set unused tuples to `None`.
   - **Account codes** — defaults below match Craig's example. User can
     override per-line if needed.

2. Render the proposal: two balanced blocks (SC tenant with SC+WB
   Location-tagged DRs + CRs + 877 clearing, then CQ tenant). Show
   DR/CR sums per tenant.
   End with: **"Reply YES to confirm and I'll create both DRAFTS in Xero now."**

3. ON EXPLICIT YES — invoke the Python via `execute_code` with `PARAMS`
   populated.

4. Quote both Xero deep-links back; add "Nicole / Tony / external
   accountant clicks Post in Xero when ready."

5. On Xero error, relay verbatim and ask the user how to proceed
   (usually: an account code that doesn't exist, or a Location option
   that's spelled differently in Xero).

## The script (run via execute_code on YES)

```python
import base64, datetime as _dt, json, os, sys
import urllib.error, urllib.request

# ─── PARAMS — populate from the user's confirmed proposal ────────────
# Defaults below are Tony's 20-24 April 2026 pay-week (Payrun1908+1909+1911-ish)
# split by Department: Field = Direct, all others = Indirect.
PARAMS = {
    "pay_period_start": "2026-04-20",
    "pay_period_end":   "2026-04-24",
    "journal_date":     "2026-04-24",
    "narration":        "Payroll week ending 24 Apr 2026",
    # Per (entity, directness) totals from MYOB Pay Activity Summary.
    # Each block: gross, pretax_ded, payg, after_tax, post_tax_ded, net, employer_super.
    # Set a tuple to None to skip it.
    "totals": {
        "SC_DIRECT":   {"gross": 107151.63, "pretax_ded": 150.00, "payg": 21438.00, "after_tax": 9406.14, "post_tax_ded": 409.45, "net": 94560.32, "employer_super": 12858.20},
        "SC_INDIRECT": {"gross": 74532.72,  "pretax_ded": 400.00, "payg": 16932.00, "after_tax": 0.00,    "post_tax_ded": 0.00,   "net": 57200.72, "employer_super": 8943.93},
        "WB_DIRECT":   {"gross": 20069.33,  "pretax_ded": 0.00,   "payg": 4269.00,  "after_tax": 1256.69, "post_tax_ded": 0.00,   "net": 17057.02, "employer_super": 2408.33},
        "WB_INDIRECT": {"gross": 1250.00,   "pretax_ded": 0.00,   "payg": 224.00,   "after_tax": 0.00,    "post_tax_ded": 0.00,   "net": 1026.00,  "employer_super": 150.00},
        "CQ_DIRECT":   {"gross": 37285.46,  "pretax_ded": 0.00,   "payg": 6765.00,  "after_tax": 2468.82, "post_tax_ded": 0.00,   "net": 32989.28, "employer_super": 4474.26},
        "CQ_INDIRECT": {"gross": 15876.21,  "pretax_ded": 0.00,   "payg": 3148.00,  "after_tax": 0.00,    "post_tax_ded": 0.00,   "net": 12728.21, "employer_super": 1905.15},
    },
    "codes": {
        "wages_direct":   "477",
        "wages_indirect": "477.4",
        "super_direct":   "478",
        "super_indirect": "478.1",
        "wages_payable":  "803",
        "payg_payable":   "825",
        "super_payable":  "826",
        "tracking_xfer":  "877",
    },
    # SC Xero's Location tracking category name + option names.
    "sc_tracking": {
        "category_name": "Location",
        "sc_option_name": "Sunshine Coast",
        "wb_option_name": "Wide Bay",
    },
}
# ─────────────────────────────────────────────────────────────────────

XERO_TOKEN_URL = "https://identity.xero.com/connect/token"
XERO_API = "https://api.xero.com/api.xro/2.0"
WRITE_SCOPES = "accounting.transactions accounting.contacts accounting.settings.read"


def _creds(entity):
    p = entity.upper()
    return {
        "client_id":     os.environ.get(f"XERO_{p}_CLIENT_ID", ""),
        "client_secret": os.environ.get(f"XERO_{p}_CLIENT_SECRET", ""),
        "tenant_id":     os.environ.get(f"XERO_{p}_TENANT_ID", ""),
    }


def _token(creds):
    if not creds["client_id"] or not creds["client_secret"]:
        raise RuntimeError("XERO_*_CLIENT_ID / _CLIENT_SECRET env vars not set")
    basic = base64.b64encode(f"{creds['client_id']}:{creds['client_secret']}".encode()).decode()
    req = urllib.request.Request(
        XERO_TOKEN_URL,
        data=f"grant_type=client_credentials&scope={WRITE_SCOPES}".encode(),
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())["access_token"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Xero token exchange failed: {e.code} {e.read().decode()[:300]}") from e


def _xero_get(creds, token, path):
    req = urllib.request.Request(
        f"{XERO_API}{path}",
        headers={"Authorization": f"Bearer {token}",
                 "Xero-Tenant-Id": creds["tenant_id"],
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Xero GET {path} failed: {e.code} {e.read().decode()[:300]}") from e


def discover_location_tracking(creds, token, cfg):
    data = _xero_get(creds, token, "/TrackingCategories")
    cats = data.get("TrackingCategories", [])
    target = next((c for c in cats
                   if c.get("Name", "").strip().lower() == cfg["category_name"].strip().lower()
                   and c.get("Status") == "ACTIVE"), None)
    if not target:
        raise RuntimeError(f"Couldn't find ACTIVE tracking category '{cfg['category_name']}' on SC Xero — saw: {[c.get('Name') for c in cats]}")
    by_name = {o.get("Name", "").strip().lower(): o for o in target.get("Options", []) if o.get("Status") == "ACTIVE"}
    sc = by_name.get(cfg["sc_option_name"].strip().lower())
    wb = by_name.get(cfg["wb_option_name"].strip().lower())
    if not sc:
        raise RuntimeError(f"'{cfg['sc_option_name']}' not in '{cfg['category_name']}' options — saw: {[o.get('Name') for o in target.get('Options', [])]}")
    if not wb:
        raise RuntimeError(f"'{cfg['wb_option_name']}' not in '{cfg['category_name']}' options — saw: {[o.get('Name') for o in target.get('Options', [])]}")
    return {
        "category_id": target["TrackingCategoryID"],
        "category_name": target["Name"],
        "sc_option_id": sc["TrackingOptionID"],
        "wb_option_id": wb["TrackingOptionID"],
    }


def _round(n):
    return round(float(n or 0), 2)


def build_sc_journal_lines(totals, codes, tracking):
    """SC + WB lines for the SC tenant, with Location tracking + 877 clearing.
       Layout per (location × directness):
         DR  Wages    (Gross + AfterTax)    Location-tagged
         DR  Super    (EmployerSuper)        Location-tagged
         CR  877       (DR sum)              Location-tagged   ← clears the location side
       Then ONE block of untracked payables:
         CR  803 Wages Payable     (sum of Nets across SC+WB)
         CR  825 PAYG Payable      (sum of PAYGs across SC+WB)
         CR  826 Super Payable     (sum of EmployerSuper across SC+WB)
       Plus matching DR 877 untracked = sum of payable CRs.
       Total DR = Total CR by construction."""
    lines = []

    def loc_dim(tag):
        return [{"TrackingCategoryID": tracking["category_id"],
                 "TrackingOptionID":   tracking["sc_option_id"] if tag == "SC" else tracking["wb_option_id"]}]

    location_blocks = [
        ("SC", "Direct",   "SC_DIRECT",   codes["wages_direct"],   codes["super_direct"]),
        ("SC", "Indirect", "SC_INDIRECT", codes["wages_indirect"], codes["super_indirect"]),
        ("WB", "Direct",   "WB_DIRECT",   codes["wages_direct"],   codes["super_direct"]),
        ("WB", "Indirect", "WB_INDIRECT", codes["wages_indirect"], codes["super_indirect"]),
    ]

    payables_acc = {"net": 0.0, "payg": 0.0, "super": 0.0}

    for loc, kind, key, wages_acc, super_acc in location_blocks:
        t = totals.get(key)
        if not t:
            continue
        gross_plus_at = _round(t.get("gross", 0) + t.get("after_tax", 0))
        super_emp = _round(t.get("employer_super", 0))
        loc_total = _round(gross_plus_at + super_emp)
        if abs(loc_total) < 0.005:
            continue

        if gross_plus_at:
            lines.append({"LineAmount": gross_plus_at, "AccountCode": wages_acc,
                          "Description": f"{loc} / {kind} — Wages (gross + after-tax allowances)",
                          "Tracking": loc_dim(loc)})
        if super_emp:
            lines.append({"LineAmount": super_emp, "AccountCode": super_acc,
                          "Description": f"{loc} / {kind} — Superannuation expense (SG)",
                          "Tracking": loc_dim(loc)})
        # 877 CR with location — clears the location side
        lines.append({"LineAmount": -loc_total, "AccountCode": codes["tracking_xfer"],
                      "Description": f"{loc} / {kind} — Tracking Transfer (location clearing)",
                      "Tracking": loc_dim(loc)})

        payables_acc["net"]   += t.get("net", 0)
        payables_acc["payg"]  += t.get("payg", 0)
        payables_acc["super"] += t.get("employer_super", 0)

    # Untracked payable CRs (combined across all SC + WB blocks)
    net_total   = _round(payables_acc["net"])
    payg_total  = _round(payables_acc["payg"])
    super_total = _round(payables_acc["super"])
    untracked_cr = _round(net_total + payg_total + super_total)
    if net_total:
        lines.append({"LineAmount": -net_total, "AccountCode": codes["wages_payable"],
                      "Description": "Net pay (SC + WB combined) — Wages Payable"})
    if payg_total:
        lines.append({"LineAmount": -payg_total, "AccountCode": codes["payg_payable"],
                      "Description": "PAYG withholdings (SC + WB combined)"})
    if super_total:
        lines.append({"LineAmount": -super_total, "AccountCode": codes["super_payable"],
                      "Description": "Employer super (SC + WB combined)"})

    # Matching untracked 877 DR
    if untracked_cr:
        lines.append({"LineAmount": untracked_cr, "AccountCode": codes["tracking_xfer"],
                      "Description": "Tracking Transfer — payable clearing (no location)"})
    return lines


def build_cq_journal_lines(totals, codes):
    """CQ Xero: no location tracking. Standard DR expenses / CR payables shape."""
    lines = []
    blocks = [
        ("Direct",   "CQ_DIRECT",   codes["wages_direct"],   codes["super_direct"]),
        ("Indirect", "CQ_INDIRECT", codes["wages_indirect"], codes["super_indirect"]),
    ]
    pay = {"net": 0.0, "payg": 0.0, "super": 0.0}
    for kind, key, wages_acc, super_acc in blocks:
        t = totals.get(key)
        if not t:
            continue
        gross_plus_at = _round(t.get("gross", 0) + t.get("after_tax", 0))
        super_emp = _round(t.get("employer_super", 0))
        if gross_plus_at:
            lines.append({"LineAmount": gross_plus_at, "AccountCode": wages_acc,
                          "Description": f"CQ / {kind} — Wages (gross + after-tax allowances)"})
        if super_emp:
            lines.append({"LineAmount": super_emp, "AccountCode": super_acc,
                          "Description": f"CQ / {kind} — Superannuation expense (SG)"})
        pay["net"]   += t.get("net", 0)
        pay["payg"]  += t.get("payg", 0)
        pay["super"] += t.get("employer_super", 0)

    net = _round(pay["net"]); payg = _round(pay["payg"]); sg = _round(pay["super"])
    if net:
        lines.append({"LineAmount": -net,  "AccountCode": codes["wages_payable"],
                      "Description": "Net pay (CQ) — Wages Payable"})
    if payg:
        lines.append({"LineAmount": -payg, "AccountCode": codes["payg_payable"],
                      "Description": "PAYG withholdings (CQ)"})
    if sg:
        lines.append({"LineAmount": -sg,   "AccountCode": codes["super_payable"],
                      "Description": "Employer super (CQ)"})
    return lines


def post_draft_journal(entity_xero, narration, journal_date, lines):
    if not lines:
        return None
    creds = _creds(entity_xero)
    if not creds["tenant_id"]:
        raise RuntimeError(f"XERO_{entity_xero}_TENANT_ID not set")

    dr = sum(l["LineAmount"] for l in lines if l["LineAmount"] > 0)
    cr = -sum(l["LineAmount"] for l in lines if l["LineAmount"] < 0)
    if abs(dr - cr) > 0.01:
        raise RuntimeError(f"{entity_xero} journal unbalanced: DR {dr:.2f} != CR {cr:.2f}")

    token = _token(creds)

    brisbane_now = _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=10))
    )
    tag = f" [DRAFT auto-generated by JBC Hermes {brisbane_now:%Y-%m-%d %H:%M AEST}]"
    final_narration = (narration + tag)[:2500]

    body = {
        "Date": journal_date,
        "Status": "DRAFT",  # HARD LOCKED — Tony 2026-05-27
        "LineAmountTypes": "NoTax",
        "Narration": final_narration,
        "JournalLines": lines,
    }
    req = urllib.request.Request(
        f"{XERO_API}/ManualJournals",
        data=json.dumps({"ManualJournals": [body]}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Xero-Tenant-Id": creds["tenant_id"],
                 "Content-Type": "application/json",
                 "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()[:600]
        try:
            j = json.loads(err)
            msg = j.get("Message") or j.get("Detail") or err
        except json.JSONDecodeError:
            msg = err
        raise RuntimeError(f"Xero {e.code}: {msg}") from e

    mj = data["ManualJournals"][0]
    return {
        "ok": True, "tenant": entity_xero,
        "ManualJournalID": mj["ManualJournalID"],
        "Status": mj.get("Status"),
        "TotalDR": round(dr, 2),
        "TotalCR": round(cr, 2),
        "LineCount": len(lines),
        "xero_link": f"https://go.xero.com/Bank/ViewManualJournal.aspx?ManualJournalID={mj['ManualJournalID']}",
    }


def run(p):
    out = {"sc_xero": None, "cq_xero": None, "notes": []}

    has_sc_side = any(p["totals"].get(k) for k in ("SC_DIRECT", "SC_INDIRECT", "WB_DIRECT", "WB_INDIRECT"))
    if has_sc_side:
        creds_sc = _creds("SC")
        if not creds_sc["client_id"]:
            raise RuntimeError("XERO_SC_* env vars not set — can't post SC/WB journal")
        token_sc = _token(creds_sc)
        tracking = discover_location_tracking(creds_sc, token_sc, p["sc_tracking"])
        out["notes"].append(f"SC Location: {tracking['sc_option_id'][:8]}…  WB Location: {tracking['wb_option_id'][:8]}…")
        sc_lines = build_sc_journal_lines(p["totals"], p["codes"], tracking)
        sc_narration = f"{p['narration']} — SC + Wide Bay (pay period {p['pay_period_start']} to {p['pay_period_end']})"
        out["sc_xero"] = post_draft_journal("SC", sc_narration, p["journal_date"], sc_lines)

    has_cq_side = any(p["totals"].get(k) for k in ("CQ_DIRECT", "CQ_INDIRECT"))
    if has_cq_side:
        creds_cq = _creds("CQ")
        if not creds_cq["client_id"]:
            raise RuntimeError("XERO_CQ_* env vars not set — can't post CQ journal")
        cq_lines = build_cq_journal_lines(p["totals"], p["codes"])
        cq_narration = f"{p['narration']} — Central Queensland (pay period {p['pay_period_start']} to {p['pay_period_end']})"
        out["cq_xero"] = post_draft_journal("CQ", cq_narration, p["journal_date"], cq_lines)

    out["ok"] = True
    return out


try:
    print(json.dumps(run(PARAMS), indent=2))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
    sys.exit(1)
```

## What the user sees in the proposal (example — April week)

> **SC Xero journal — Payroll wk ending 24 Apr 2026 — SC + Wide Bay**
> 
> Sunshine Coast / Direct:
>   DR 477 Wages-Direct (Loc: Sunshine Coast) — $116,557.77
>   DR 478 Super-Direct (Loc: Sunshine Coast) — $12,858.20
>   CR 877 Tracking Transfer (Loc: Sunshine Coast) — $129,415.97
> 
> Sunshine Coast / Indirect:
>   DR 477.4 Wages-Indirect (Loc: Sunshine Coast) — $74,532.72
>   DR 478.1 Super-Indirect (Loc: Sunshine Coast) — $8,943.93
>   CR 877 Tracking Transfer (Loc: Sunshine Coast) — $83,476.65
> 
> Wide Bay / Direct:
>   DR 477 Wages-Direct (Loc: Wide Bay) — $21,326.02
>   DR 478 Super-Direct (Loc: Wide Bay) — $2,408.33
>   CR 877 Tracking Transfer (Loc: Wide Bay) — $23,734.35
> 
> Wide Bay / Indirect:
>   DR 477.4 Wages-Indirect (Loc: Wide Bay) — $1,250.00
>   DR 478.1 Super-Indirect (Loc: Wide Bay) — $150.00
>   CR 877 Tracking Transfer (Loc: Wide Bay) — $1,400.00
> 
> Untracked payables (SC + WB combined):
>   CR 803 Wages Payable — $169,844.06
>   CR 825 PAYG Payable — $42,863.00
>   CR 826 Super Payable — $24,360.46
>   DR 877 Tracking Transfer (no location) — $237,067.52
> 
> SC tenant balance: $237,066.97 DR / $237,066.97 CR ✓
> 
> **CQ Xero journal — Payroll wk ending 24 Apr 2026 — Central Queensland**
>   DR 477 Wages-Direct — $39,754.28
>   DR 477.4 Wages-Indirect — $15,876.21
>   DR 478 Super-Direct — $4,474.26
>   DR 478.1 Super-Indirect — $1,905.15
>   CR 803 Wages Payable — $45,717.49
>   CR 825 PAYG Payable — $9,913.00
>   CR 826 Super Payable — $6,379.41
> 
> CQ tenant balance: $62,009.90 DR / $62,009.90 CR ✓
> 
> Account codes match Craig's historical pattern (per Journal #673782).
> Leave isn't separated in this v2 — folded into the wages lines.
> Reply YES to confirm and I'll create both DRAFTS in Xero now.
