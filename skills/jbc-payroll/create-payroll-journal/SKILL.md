---
name: create-payroll-journal
description: Build two balanced manual journals (one in SC Xero with Wide Bay tracking-tagged lines, one in CQ Xero) from a MYOB Pay Activity Summary's per-entity totals. Posts both as DRAFT — Nicole / Tony / the external accountant clicks Post in Xero. The agent never posts.
version: 0.1.0
platforms: [linux, macos]
metadata:
  hermes:
    tags: [xero, payroll, jbc, drafts, manual-journal, finance, sc, cq, wb]
    category: jbc-finance
required_environment_variables:
  - name: XERO_SC_CLIENT_ID
    prompt: Xero SC tenant client ID (Sunshine Coast Pty Ltd — also holds Wide Bay as a Location tracking option)
    required_for: SC + WB journal
  - name: XERO_SC_CLIENT_SECRET
    prompt: Xero SC tenant client secret
    required_for: SC + WB journal
  - name: XERO_SC_TENANT_ID
    prompt: Xero SC tenant UUID
    required_for: SC + WB journal
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

# Create payroll DRAFT manual journals from MYOB Pay Activity totals

## When to use

Triggered when the user has a MYOB Advanced "Pay Activity [Summary]" report
for a single pay period and wants the corresponding journal entries posted
in Xero. JBC's payroll spans three operational entities — Sunshine Coast,
Wide Bay, and Central Queenland — across two Xero tenants:

- **SC Xero tenant** holds Sunshine Coast + Wide Bay. WB is tagged via the
  `Location` tracking category (options "Sunshine Coast" and "Wide Bay").
- **CQ Xero tenant** holds Central Queenland only.

One DRAFT manual journal goes into each Xero tenant per pay period.

## Hard rules

1. **Status is hard-locked to DRAFT** in the Python below. No path flips
   it to POSTED. Nicole / Tony / external accountant posts in Xero.
2. **One journal per Xero tenant.** SC's journal contains BOTH Sunshine
   Coast and Wide Bay lines, distinguished by the Location tracking on
   each line. CQ's journal contains only CQ lines.
3. **Each journal must balance** (total DR = total CR within 1c).
4. **Pre-tax deductions and Salary-Sacrifice Super are the same dollar.**
   In MYOB's report `PreTaxDed` equals `EmpSuper` (employee elected to
   reduce pre-tax pay to fund super). Don't double-count.

## MYOB column → journal mapping

For each entity (SC / WB / CQ) the journal expands as:

```
DR  Wages & Salaries Expense         = Gross
DR  Allowances Expense (after-tax)   = AfterTax
DR  Superannuation Expense (SG)      = EmployerSuper
                                       ───────────
                                     = Gross + AfterTax + EmployerSuper

CR  PAYG Withholding Payable         = PAYG
CR  Post-tax Deductions Payable      = PostTaxDed
CR  Net Pay / Bank Clearing          = Net
CR  Salary Sacrifice Super Payable   = PreTaxDed   (≡ EmpSuper)
CR  Superannuation Payable (SG)      = EmployerSuper
                                       ───────────
                                       (balances)
```

Net is computed by MYOB as: `Net = Gross − PreTaxDed − PAYG + AfterTax − PostTaxDed`.
Confirmed against Tony's 20-24 Apr file (every entity balanced to the cent).

## Procedure

1. Get per-entity totals from the user. Either:
   - User pastes them (e.g. "SC: gross 181684.35, payg 38370…"), OR
   - User provides path to a parsed JSON, OR
   - User asks Mark to extract them from a MYOB Pay Activity file (Mark
     does that conversationally, then hands the totals back to the skill).

2. Confirm:
   - **Pay period** (date range and the journal-date — usually the last
     day of the period)
   - **Account codes** for each line type (Wages, Allowances, SuperExpense,
     PAYG Payable, PostTaxDed Payable, Net Pay clearing, SalarySacrifice
     Super Payable, Super Payable). Defaults below; user can override.

3. Render the proposal: two balanced journal blocks (SC with WB-tagged
   lines, then CQ). Show DR/CR sums. End with:
   **"Reply YES to confirm and I'll create both DRAFTS in Xero now."**

4. ON EXPLICIT YES — invoke the Python via `execute_code` with `PARAMS`
   populated from the user's confirmed proposal.

5. The script:
   - Discovers SC's Location tracking category + options
     (Sunshine Coast / Wide Bay UUIDs) via `/TrackingCategories`.
   - Builds SC journal lines for SC and WB, each line carrying the
     correct `Tracking` dimension.
   - Builds CQ journal lines (no tracking).
   - POSTs both as DRAFT manual journals.
   - Returns both deep-links.

6. Quote both Xero links back to the user; add the standard
   "Nicole / Tony / the external accountant clicks Post" close.

## Default account codes (placeholders — user overrides)

| Line | Default code |
|------|------|
| Wages & Salaries Expense | `6010` |
| Allowances Expense | `6011` |
| Superannuation Expense (SG) | `6020` |
| PAYG Withholding Payable | `2100` |
| Post-tax Deductions Payable | `2105` |
| Net Pay / Bank Clearing | `2110` |
| Salary Sacrifice Super Payable | `2130` |
| Superannuation Payable (SG) | `2140` |

If Xero rejects a code, relay the error verbatim and ask the user to
correct.

## The script (run via execute_code on YES)

```python
import base64, datetime as _dt, json, os, sys
import urllib.error, urllib.request

# ─── PARAMS — populate from the user's confirmed proposal ────────────
PARAMS = {
    "pay_period_start": "2026-04-20",
    "pay_period_end":   "2026-04-24",
    "journal_date":     "2026-04-24",  # usually the period end
    "narration":        "Payroll week ending 24 Apr 2026",
    # Per-entity totals (from the MYOB Pay Activity [Summary] report).
    # Set unused entities to None to skip them.
    "totals": {
        "SC": {"gross": 181684.35, "pretax_ded": 550.00, "payg": 38370.00,
               "after_tax": 9406.14, "post_tax_ded": 409.45,
               "net": 151761.04, "employer_super": 21802.13},
        "WB": {"gross": 21319.33, "pretax_ded": 0.00, "payg": 4493.00,
               "after_tax": 1256.69, "post_tax_ded": 0.00,
               "net": 18083.02, "employer_super": 2558.33},
        "CQ": {"gross": 53161.67, "pretax_ded": 0.00, "payg": 9913.00,
               "after_tax": 2468.82, "post_tax_ded": 0.00,
               "net": 45717.49, "employer_super": 6379.41},
    },
    # Account codes the user confirmed. Defaults match the skill table.
    "codes": {
        "wages":              "6010",
        "allowances":         "6011",
        "super_expense":      "6020",
        "payg_payable":       "2100",
        "post_tax_payable":   "2105",
        "net_pay_clearing":   "2110",
        "salary_sac_payable": "2130",
        "super_payable":      "2140",
    },
    # SC Xero's Location tracking category name + the option names that
    # correspond to "Sunshine Coast" and "Wide Bay". Defaults match what
    # Tony confirmed; tweak if Xero spells them differently.
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
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Xero GET {path} failed: {e.code} {e.read().decode()[:300]}") from e


def discover_sc_location_tracking(creds, token, cfg):
    """Return {sc_option_id, wb_option_id, category_id} for SC's Location dim."""
    data = _xero_get(creds, token, "/TrackingCategories")
    cats = data.get("TrackingCategories", [])
    target = None
    for c in cats:
        if c.get("Name", "").strip().lower() == cfg["category_name"].strip().lower() and c.get("Status") == "ACTIVE":
            target = c
            break
    if not target:
        raise RuntimeError(
            f"Couldn't find ACTIVE tracking category named '{cfg['category_name']}' on SC Xero — "
            f"saw: {[c.get('Name') for c in cats]}"
        )
    options = target.get("Options", [])
    by_name = {o.get("Name", "").strip().lower(): o for o in options if o.get("Status") == "ACTIVE"}
    sc_opt = by_name.get(cfg["sc_option_name"].strip().lower())
    wb_opt = by_name.get(cfg["wb_option_name"].strip().lower())
    if not sc_opt:
        raise RuntimeError(
            f"'{cfg['sc_option_name']}' not found in '{cfg['category_name']}' options on SC Xero — "
            f"saw: {[o.get('Name') for o in options]}"
        )
    if not wb_opt:
        raise RuntimeError(
            f"'{cfg['wb_option_name']}' not found in '{cfg['category_name']}' options on SC Xero — "
            f"saw: {[o.get('Name') for o in options]}"
        )
    return {
        "category_id": target["TrackingCategoryID"],
        "category_name": target["Name"],
        "sc_option_id": sc_opt["TrackingOptionID"],
        "wb_option_id": wb_opt["TrackingOptionID"],
        "sc_option_name": sc_opt["Name"],
        "wb_option_name": wb_opt["Name"],
    }


def journal_lines_for(entity_label, totals, codes, tracking=None):
    """Build the 8-or-fewer JournalLines for one entity.
       LineAmount sign in Xero: positive = DR, negative = CR."""
    lines = []

    def add(side, code, amount, desc):
        if amount is None:
            return
        amount = round(float(amount), 2)
        if abs(amount) < 0.005:  # skip zero lines
            return
        line = {
            "LineAmount": amount if side == "DR" else -amount,
            "AccountCode": str(code),
            "Description": f"{entity_label} — {desc}",
        }
        if tracking:
            line["Tracking"] = [tracking]
        lines.append(line)

    g = totals.get("gross")        or 0
    a = totals.get("after_tax")    or 0
    s = totals.get("employer_super") or 0
    p = totals.get("payg")          or 0
    ptd = totals.get("post_tax_ded") or 0
    n = totals.get("net")           or 0
    sac = totals.get("pretax_ded")  or 0  # salary-sacrifice super (≡ EmpSuper)

    # DRs
    add("DR", codes["wages"],         g,   "Wages & Salaries (gross)")
    add("DR", codes["allowances"],    a,   "Allowances (after-tax)")
    add("DR", codes["super_expense"], s,   "Superannuation Expense (SG)")
    # CRs
    add("CR", codes["payg_payable"],       p,   "PAYG Withholding Payable")
    add("CR", codes["post_tax_payable"],   ptd, "Post-tax Deductions Payable")
    add("CR", codes["net_pay_clearing"],   n,   "Net Pay / Bank Clearing")
    add("CR", codes["salary_sac_payable"], sac, "Salary Sacrifice Super Payable")
    add("CR", codes["super_payable"],      s,   "Superannuation Payable (SG)")

    dr = sum(l["LineAmount"] for l in lines if l["LineAmount"] > 0)
    cr = -sum(l["LineAmount"] for l in lines if l["LineAmount"] < 0)
    if abs(dr - cr) > 0.01:
        raise RuntimeError(
            f"{entity_label} unbalanced — DR {dr:.2f} != CR {cr:.2f}. "
            f"Likely Net != Gross − PreTax − PAYG + AfterTax − PostTax. "
            f"Check totals before retrying."
        )
    return lines


def post_draft_journal(entity_xero, narration, journal_date, lines):
    creds = _creds(entity_xero)
    if not creds["tenant_id"]:
        raise RuntimeError(f"XERO_{entity_xero}_TENANT_ID not set")
    token = _token(creds)

    brisbane_now = _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=10))
    )
    tag = f"[DRAFT auto-generated by JBC Hermes {brisbane_now:%Y-%m-%d %H:%M AEST}]"
    final_narration = f"{narration} {tag}"[:2500]

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
    mj_id = mj["ManualJournalID"]
    return {
        "ok": True,
        "tenant": entity_xero,
        "ManualJournalID": mj_id,
        "Status": mj.get("Status"),
        "Total DR": sum(l["LineAmount"] for l in lines if l["LineAmount"] > 0),
        "Total CR": -sum(l["LineAmount"] for l in lines if l["LineAmount"] < 0),
        "xero_link": f"https://go.xero.com/Bank/ViewManualJournal.aspx?ManualJournalID={mj_id}",
    }


def run(p):
    out = {"sc_xero": None, "cq_xero": None, "warnings": []}

    # SC + WB share the SC Xero tenant via Location tracking.
    sc_t = p["totals"].get("SC")
    wb_t = p["totals"].get("WB")
    if sc_t or wb_t:
        creds_sc = _creds("SC")
        if not creds_sc["client_id"]:
            raise RuntimeError("XERO_SC_* env vars not set — can't post SC/WB journal")
        token_sc = _token(creds_sc)
        tracking_dims = discover_sc_location_tracking(creds_sc, token_sc, p["sc_tracking"])
        out["warnings"].append(
            f"SC tracking dims: {tracking_dims['category_name']} → "
            f"{tracking_dims['sc_option_name']} ({tracking_dims['sc_option_id'][:8]}…), "
            f"{tracking_dims['wb_option_name']} ({tracking_dims['wb_option_id'][:8]}…)"
        )

        sc_lines = []
        if sc_t:
            sc_lines += journal_lines_for(
                "Sunshine Coast", sc_t, p["codes"],
                tracking={"TrackingCategoryID": tracking_dims["category_id"],
                          "TrackingOptionID":   tracking_dims["sc_option_id"]},
            )
        if wb_t:
            sc_lines += journal_lines_for(
                "Wide Bay", wb_t, p["codes"],
                tracking={"TrackingCategoryID": tracking_dims["category_id"],
                          "TrackingOptionID":   tracking_dims["wb_option_id"]},
            )
        narration = f"{p['narration']} — Sunshine Coast + Wide Bay (pay period {p['pay_period_start']} to {p['pay_period_end']})"
        out["sc_xero"] = post_draft_journal("SC", narration, p["journal_date"], sc_lines)

    cq_t = p["totals"].get("CQ")
    if cq_t:
        creds_cq = _creds("CQ")
        if not creds_cq["client_id"]:
            raise RuntimeError("XERO_CQ_* env vars not set — can't post CQ journal")
        cq_lines = journal_lines_for("Central Queensland", cq_t, p["codes"], tracking=None)
        narration = f"{p['narration']} — Central Queensland (pay period {p['pay_period_start']} to {p['pay_period_end']})"
        out["cq_xero"] = post_draft_journal("CQ", narration, p["journal_date"], cq_lines)

    out["ok"] = True
    return out


try:
    print(json.dumps(run(PARAMS), indent=2))
except Exception as exc:
    print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
    sys.exit(1)
```

## Example (week ending 24 Apr 2026)

User: "Create the payroll journals for week 20-24 April using the per-entity
totals I just gave you."

You confirm:

> **SC Xero — Sunshine Coast lines (Location: Sunshine Coast)**
> DR 6010 Wages $181,684.35 · DR 6011 Allowances $9,406.14 · DR 6020 Super-Exp $21,802.13
> CR 2100 PAYG $38,370 · CR 2105 PostTax $409.45 · CR 2110 Net Pay $151,761.04 · CR 2130 SalSac $550 · CR 2140 Super-Pay $21,802.13
> **Sunshine Coast subtotal: $212,892.62 DR / $212,892.62 CR ✓**
>
> **SC Xero — Wide Bay lines (Location: Wide Bay)**
> DR 6010 Wages $21,319.33 · DR 6011 Allowances $1,256.69 · DR 6020 Super-Exp $2,558.33
> CR 2100 PAYG $4,493 · CR 2110 Net Pay $18,083.02 · CR 2140 Super-Pay $2,558.33
> **Wide Bay subtotal: $25,134.35 DR / $25,134.35 CR ✓**
>
> **CQ Xero**
> DR 6010 Wages $53,161.67 · DR 6011 Allowances $2,468.82 · DR 6020 Super-Exp $6,379.41
> CR 2100 PAYG $9,913 · CR 2110 Net Pay $45,717.49 · CR 2140 Super-Pay $6,379.41
> **CQ subtotal: $62,009.90 DR / $62,009.90 CR ✓**
>
> Account codes are placeholders — adjust if your chart uses different codes.
> Reply YES to confirm and I'll create both DRAFTS in Xero now.

User: "YES" → script runs → returns 2 Xero deep-links.
