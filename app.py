import hmac
import io
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from core.storage import (
    init_db, upsert_transactions, fetch_all, make_txn_id, delete_transactions, delete_all,
    ensure_account, get_account_master, update_account, update_transaction,
    save_rule, get_rules, delete_rule, ensure_party, get_party_master,
)
from core.categorize import categorize, apply_correction, is_office_for_category
from core import reports
from core import financial_rules as fr
from parsers import sms_parser, statement_parser, contract_note_parser

st.set_page_config(page_title="Finance Tracker", layout="wide")


def require_password():
    """Gate the whole app behind a single shared password (st.secrets['APP_PASSWORD']).
    This app is deployed to a public URL and holds bank/trading data, so nothing
    below this check should render for an unauthenticated visitor."""
    if st.session_state.get("authenticated"):
        return

    def on_submit():
        entered = st.session_state.get("password_input", "")
        expected = st.secrets.get("APP_PASSWORD", "")
        if expected and hmac.compare_digest(entered, expected):
            st.session_state["authenticated"] = True
        else:
            st.session_state["auth_failed"] = True

    st.title("🔒 Finance Tracker")
    st.text_input("Password", type="password", key="password_input", on_change=on_submit)
    if st.session_state.get("auth_failed"):
        st.error("Incorrect password.")
    st.stop()


require_password()
init_db()
ensure_party("Office")


def load_df() -> pd.DataFrame:
    df = reports.to_dataframe(fetch_all())
    if df.empty:
        return df
    masters = {m["key"]: m["label"] for m in get_account_master()}
    df["account"] = df.apply(
        lambda row: masters.get(row.get("account_key"), row["account"]), axis=1
    )
    return df


def account_key_and_label(bank: str | None, account: str | None) -> tuple[str | None, str | None]:
    """Builds one normalized key regardless of source. SMS rows already fold
    the bank name into 'account' (e.g. 'Kotak XX2071'); statement/contract-note
    rows keep them separate ('Kotak' + 'XX2071'). Concatenating unconditionally
    would double up the bank name for SMS rows and split what's really the same
    account into two different keys for statement rows — so only concatenate
    when the bank name isn't already present in the account text."""
    bank = (bank or "").strip()
    account = (account or "").strip()
    if account and bank and bank.lower() not in account.lower():
        label = f"{bank} {account}"
    else:
        label = account or bank
    label = label.strip()
    return (label.lower() or None), (label or None)


def save_rows(rows: list[dict]) -> tuple[int, int]:
    """Normalizes rows, skips ones that match an already-recorded transaction
    (by shared reference number, else amount+direction+date), and saves the
    rest. Returns (saved_count, skipped_as_already_recorded_count)."""
    for r in rows:
        has_category = bool(r.get("category"))
        has_is_office = "is_office" in r
        if not has_category:
            cat, auto_is_office = categorize(r.get("description", ""))
            r["category"] = cat
            if not has_is_office:
                r["is_office"] = auto_is_office
        elif not has_is_office:
            r["is_office"] = is_office_for_category(r["category"])
        r["is_office"] = bool(r.get("is_office"))
        if not r.get("parties"):
            r["parties"] = ["Office"] if r["is_office"] else []
        for p in r["parties"]:
            ensure_party(p)
        for k in ("account", "bank", "source_file", "scrip"):
            r.setdefault(k, None)
        for k in ("quantity", "price", "charges"):
            r.setdefault(k, None)
        r["id"] = make_txn_id(r["date"], r["amount"], r["description"], r["source"])
        key, label = account_key_and_label(r.get("bank"), r.get("account"))
        r["account_key"] = key
        if key:
            ensure_account(key, label)

    existing_df = load_df()
    matched, unmatched = reports.find_new_transactions(existing_df, rows)
    n = upsert_transactions(unmatched)
    return n, len(matched)


st.title("Office Expenses, Bank & Trading Tracker")
st.caption("Data is stored in your private Supabase project — accessible only with the app password.")

tab_upload, tab_review, tab_accounts, tab_reports, tab_trading, tab_recon, tab_rules = st.tabs(
    ["📥 Upload", "🏷️ Review & Categorize", "🏦 Accounts", "📊 Reports", "📈 Trading",
     "🔗 Reconciliation", "🎯 Expected & Cross-Check"]
)

# ---------------------------------------------------------------- Upload ---
with tab_upload:
    st.subheader("Bank SMS alerts")
    st.caption(
        "Upload a .txt file. Works best as forwarded-to-yourself chat text: each bank alert "
        "(can span several lines) followed by a tag reply — 'Office'/'Personal' set the "
        "office/personal split, and any other reply (e.g. 'FD', 'Cashback', 'Home Loan EMI') "
        "becomes the category directly. A .csv/.xlsx SMS-backup export also works (no tags there)."
    )
    sms_files = st.file_uploader("SMS export", type=["txt", "csv", "xlsx", "xls"],
                                  accept_multiple_files=True, key="sms_upl")
    if sms_files and st.button("Parse & save SMS"):
        total, total_skipped = 0, 0
        for f in sms_files:
            try:
                rows = sms_parser.parse_sms_file(f.name, f.read())
            except ValueError as e:
                st.error(str(e))
                continue
            for r in rows:
                r["source_file"] = f.name
            n, skipped = save_rows(rows)
            total += n
            total_skipped += skipped
        msg = f"Saved {total} new SMS transactions."
        if total_skipped:
            msg += f" Skipped {total_skipped} that matched an already-recorded transaction."
        st.success(msg)

    st.divider()
    st.subheader("Bank statement PDFs")
    stmt_files = st.file_uploader("Statement PDF(s)", type=["pdf"],
                                   accept_multiple_files=True, key="stmt_upl")
    for f in stmt_files or []:
        with st.expander(f"📄 {f.name}", expanded=True):
            raw_bytes = f.getvalue()
            df = statement_parser.extract_raw_table(raw_bytes)
            if df.empty:
                st.warning("Couldn't detect a transaction table in this PDF. "
                           "It may be a scanned/image PDF — try exporting a text-based statement instead.")
                continue
            st.dataframe(df.head(15), use_container_width=True, height=200)
            mapping_guess = statement_parser.guess_column_mapping(df)
            cols = ["-- none --"] + list(df.columns)

            def idx(field):
                val = mapping_guess.get(field)
                return cols.index(val) if val in cols else 0

            c1, c2 = st.columns(2)
            bank = c1.text_input("Bank name", key=f"bank_{f.name}",
                                  help="Use the same spelling as any existing account (Accounts tab) so they merge into one.")
            account = c2.text_input("Account/card label (e.g. XX1234)", key=f"acct_{f.name}")
            c1, c2, c3, c4 = st.columns(4)
            date_col = c1.selectbox("Date column", cols, index=idx("date"), key=f"date_{f.name}")
            desc_col = c2.selectbox("Description column", cols, index=idx("description"), key=f"desc_{f.name}")
            debit_col = c3.selectbox("Debit column (or use single amount)", cols, index=idx("debit"), key=f"debit_{f.name}")
            credit_col = c4.selectbox("Credit column", cols, index=idx("credit"), key=f"credit_{f.name}")
            c1, c2, c3 = st.columns(3)
            amount_col = c1.selectbox("...OR single amount column (leave debit/credit as none)",
                                       cols, index=idx("amount"), key=f"amt_{f.name}")
            default_dir = c2.selectbox(
                "If that amount has no sign/Cr/Dr marker, treat as",
                ["debit", "credit"], key=f"dir_{f.name}",
                help="Most credit-card statements list unsigned purchase amounts — pick 'debit'. "
                     "A 'Cr'/'Dr' suffix on a specific row (e.g. '3,643.00 Cr') always overrides this.",
            )
            category_col = c3.selectbox(
                "Category column (optional)", cols, index=idx("category"), key=f"cat_{f.name}",
                help="If your statement already assigns a category per transaction (e.g. 'Restaurants', 'Fuel'), "
                     "use it directly instead of guessing from the description.",
            )

            if st.button(f"Add transactions from {f.name}", key=f"commit_{f.name}"):
                mapping = {"date": date_col, "description": desc_col}
                if debit_col != "-- none --":
                    mapping["debit"] = debit_col
                if credit_col != "-- none --":
                    mapping["credit"] = credit_col
                if amount_col != "-- none --":
                    mapping["amount"] = amount_col
                if category_col != "-- none --":
                    mapping["category"] = category_col
                rows = statement_parser.normalize_table(df, mapping, bank or "Unknown Bank",
                                                         account or "Unknown", f.name,
                                                         default_direction=default_dir)
                n, skipped = save_rows(rows)
                msg = f"Saved {n} new transactions from {f.name} (of {len(rows)} parsed)."
                if skipped:
                    msg += f" {skipped} already matched an existing transaction (e.g. an SMS you tagged) and were not duplicated."
                st.success(msg)

    st.divider()
    st.subheader("Demat contract notes")
    cn_files = st.file_uploader("Contract note PDF(s)", type=["pdf"],
                                 accept_multiple_files=True, key="cn_upl")
    for f in cn_files or []:
        with st.expander(f"📄 {f.name}", expanded=True):
            raw_bytes = f.getvalue()
            df, meta = contract_note_parser.extract_trade_table(raw_bytes)
            if df.empty:
                st.warning("Couldn't detect a trade table in this PDF.")
                continue
            st.dataframe(df.head(15), use_container_width=True, height=200)
            if meta.get("net_settlement"):
                st.caption(f"Detected net settlement amount: {meta['net_settlement']:,.2f} "
                           f"on {meta.get('trade_date', 'unknown date')} — match this against your bank statement.")
            mapping_guess = contract_note_parser.guess_trade_mapping(df)
            cols = ["-- none --"] + list(df.columns)

            def idx2(field):
                val = mapping_guess.get(field)
                return cols.index(val) if val in cols else 0

            broker = st.text_input("Broker name", key=f"broker_{f.name}")
            c1, c2, c3, c4, c5 = st.columns(5)
            scrip_col = c1.selectbox("Scrip/Security", cols, index=idx2("scrip"), key=f"scrip_{f.name}")
            side_col = c2.selectbox("Buy/Sell", cols, index=idx2("side"), key=f"side_{f.name}")
            qty_col = c3.selectbox("Quantity", cols, index=idx2("quantity"), key=f"qty_{f.name}")
            price_col = c4.selectbox("Price/Rate", cols, index=idx2("price"), key=f"price_{f.name}")
            net_col = c5.selectbox("Net amount", cols, index=idx2("net_amount"), key=f"net_{f.name}")

            if st.button(f"Add trades from {f.name}", key=f"commitcn_{f.name}"):
                mapping = {k: v for k, v in {
                    "scrip": scrip_col, "side": side_col, "quantity": qty_col,
                    "price": price_col, "net_amount": net_col,
                }.items() if v != "-- none --"}
                rows = contract_note_parser.normalize_trades(df, mapping, meta, broker or "Unknown Broker", f.name)
                n, skipped = save_rows(rows)
                msg = f"Saved {n} new trade rows from {f.name} (of {len(rows)} parsed)."
                if skipped:
                    msg += f" {skipped} already matched an existing trade row and were not duplicated."
                st.success(msg)

# --------------------------------------------------------- Review tab -----
with tab_review:
    df = load_df()
    if df.empty:
        st.info("No transactions yet — upload some data first.")
    else:
        known_parties = get_party_master()
        st.caption(
            "Edit Category / Parties and hit Save — corrections are remembered for similar merchants "
            "going forward. Parties is who this relates to — 'Office' drives the office-expense reports, "
            "but you can add any name (comma-separated for more than one, e.g. 'Ram, Sham'). "
            f"Known so far: {', '.join(known_parties) if known_parties else '(none yet)'}. "
            "Delete a row with the trash icon on its left, then Save."
        )
        show_uncat_only = st.checkbox("Show only Uncategorized", value=True)
        view = df[df["category"] == "Uncategorized"] if show_uncat_only else df
        editable = view[["id", "date", "amount", "direction", "account", "bank",
                          "description", "category", "parties"]].copy()
        editable["parties"] = editable["parties"].apply(lambda ps: ", ".join(ps))
        edited = st.data_editor(
            editable, use_container_width=True, hide_index=True, key="editor",
            disabled=["id", "date", "amount", "direction", "account", "bank", "description"],
            num_rows="dynamic",
        )
        if st.button("Save corrections"):
            removed_ids = set(editable["id"]) - set(edited["id"])
            delete_transactions(list(removed_ids))
            changed = 0
            for _, row in edited.iterrows():
                raw_parties = "" if pd.isna(row["parties"]) else str(row["parties"])
                parties = [p.strip() for p in raw_parties.split(",") if p.strip()]
                is_office = any(p.lower() == "office" for p in parties)
                for p in parties:
                    ensure_party(p)
                update_transaction(row["id"], row["category"], is_office, parties)
                apply_correction(row["description"], row["category"], is_office)
                changed += 1
            msg = f"Updated {changed} rows."
            if removed_ids:
                msg += f" Deleted {len(removed_ids)} rows."
            st.success(msg)
            st.rerun()

        st.divider()
        with st.expander("⚠️ Danger zone — delete all transactions"):
            st.caption("Useful while testing. This does not affect Account labels (Accounts tab) or "
                       "category-correction memory — only the transaction rows themselves.")
            confirm = st.checkbox("Yes, delete every transaction in the database", key="confirm_wipe")
            if st.button("Delete ALL transactions", disabled=not confirm, type="primary"):
                delete_all()
                st.success("All transactions deleted.")
                st.rerun()

# ------------------------------------------------------- Accounts tab -----
with tab_accounts:
    st.caption(
        "Every distinct bank/card the parsers have seen gets a row here automatically "
        "(e.g. 'kotak xx2071' the first time a Kotak x2071 transaction is parsed). "
        "Give it a real label and a type — relabeling here updates how that account shows "
        "up everywhere (Reports, Reconciliation) without touching past transactions."
    )
    masters = get_account_master()
    if not masters:
        st.info("No accounts seen yet — upload some transactions first.")
    else:
        m_df = pd.DataFrame(masters)
        m_df["account_type"] = m_df["account_type"].fillna("Unknown")
        edited_m = st.data_editor(
            m_df, use_container_width=True, hide_index=True, key="account_editor",
            disabled=["key"],
            column_config={
                "key": st.column_config.TextColumn("Internal key"),
                "label": st.column_config.TextColumn("Label", help="Shown throughout the app"),
                "account_type": st.column_config.SelectboxColumn(
                    "Type", options=["Unknown", "Bank Account", "Credit Card", "Demat/Trading", "Other"]
                ),
            },
        )
        if st.button("Save account labels"):
            for _, row in edited_m.iterrows():
                update_account(row["key"], row["label"], row["account_type"])
            st.success("Account labels updated.")
            st.rerun()

# -------------------------------------------------------- Reports tab -----
with tab_reports:
    df = load_df()
    if df.empty:
        st.info("No transactions yet — upload some data first.")
    else:
        min_d, max_d = df["date"].min().date(), df["date"].max().date()
        c1, c2, c3 = st.columns(3)
        start = c1.date_input("From", value=min_d, key="rep_start")
        end = c2.date_input("To", value=max_d, key="rep_end")
        banks = sorted(df["bank"].dropna().unique().tolist())
        bank_filter = c3.multiselect("Bank/Broker", banks, default=banks)

        f = reports.filter_range(df, start, end)
        if bank_filter:
            f = f[f["bank"].isin(bank_filter) | f["bank"].isna()]

        st.markdown("### Office expense claim")
        claim = reports.office_expense_claim(f)
        if claim.empty:
            st.caption("No office-tagged expenses in this range.")
        else:
            st.dataframe(claim, use_container_width=True)
            st.metric("Total claimable", f"₹{claim['total'].sum():,.2f}")
            detail = reports.office_expense_detail(f)
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xw:
                claim.to_excel(xw, sheet_name="By Bank-Card", index=False)
                detail.to_excel(xw, sheet_name="Detail", index=False)
            st.download_button("Download claim as Excel", buf.getvalue(),
                                file_name=f"office_expense_claim_{start}_{end}.xlsx")

        st.markdown("### Monthly cash flow")
        flow = reports.monthly_cash_flow(f)
        if not flow.empty:
            fig = px.bar(flow, x="month", y=["credit", "debit"], barmode="group")
            st.plotly_chart(fig, use_container_width=True)
            st.dataframe(flow, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("### Expense by head (category)")
            cat = reports.category_breakdown(f)
            if not cat.empty:
                st.plotly_chart(px.pie(cat, names="category", values="total"), use_container_width=True)
                st.dataframe(cat, use_container_width=True)
        with c2:
            st.markdown("### Personal vs Office")
            pvo = reports.personal_vs_office(f)
            if not pvo.empty:
                st.plotly_chart(px.pie(pvo, names="tag", values="amount"), use_container_width=True)
                st.dataframe(pvo, use_container_width=True)

        st.markdown("### By party (who it's linked to)")
        party = reports.party_breakdown(f)
        if party.empty:
            st.caption("No transactions tagged to a party yet — add names in the Review & Categorize tab.")
        else:
            st.plotly_chart(px.bar(party, x="party", y="total"), use_container_width=True)
            st.dataframe(party, use_container_width=True)

        st.markdown("### Income analysis")
        inc = reports.income_analysis(f)
        if inc.empty:
            st.caption("No income (credit) transactions in this range.")
        else:
            st.plotly_chart(px.bar(inc, x="month", y="amount", color="category"), use_container_width=True)
            st.dataframe(inc, use_container_width=True)

# -------------------------------------------------------- Trading tab -----
with tab_trading:
    df = load_df()
    trades = df[df["source"] == "contract_note"] if not df.empty else df
    if trades.empty:
        st.info("No contract notes uploaded yet.")
    else:
        st.markdown("### Trade summary by scrip")
        st.dataframe(reports.trading_summary(df), use_container_width=True)
        st.markdown("### All trade rows")
        st.dataframe(
            trades[["date", "scrip", "direction", "quantity", "price", "amount", "bank", "source_file"]],
            use_container_width=True,
        )

# --------------------------------------------------- Reconciliation tab ---
with tab_recon:
    df = load_df()
    if df.empty:
        st.info("No transactions yet.")
    else:
        st.caption("Statement entries with no matching tagged SMS within 2 days / exact amount — "
                   "these are transactions you haven't Office/Personal-tagged, grouped by account "
                   "so you can see which account still needs SMS forwarded+tagged for it.")
        unmatched = reports.unmatched_between_sms_and_statement(df)
        if unmatched.empty:
            st.success("Nothing to flag (or you haven't uploaded both SMS and statement data yet).")
        else:
            st.markdown("### By account")
            by_account = (
                unmatched.groupby("account", dropna=False)["amount"]
                .agg(total="sum", count="count")
                .reset_index()
                .sort_values("total", ascending=False)
            )
            st.dataframe(by_account, use_container_width=True)
            st.markdown("### Detail")
            st.dataframe(unmatched[["date", "amount", "direction", "description", "account", "bank"]],
                        use_container_width=True)

# ------------------------------------------------- Expected & Cross-Check -
with tab_rules:
    st.caption(
        "Define what you EXPECT — savings interest at a rate, credit-card cashback at a rate, "
        "a loan on its own amortization schedule, or any other fixed recurring transfer with a "
        "bank, institute, your office, or a relative — then check whether the real transactions "
        "actually match, are missing, or differ."
    )

    masters = get_account_master()
    account_options = {m["label"]: m["key"] for m in masters}

    with st.expander("➕ Add a rule", expanded=not get_rules()):
        name = st.text_input("Name", placeholder="e.g. Axis savings interest, Kotak IOC cashback, HDFC home loan")
        c1, c2, c3 = st.columns(3)
        rule_type = c1.selectbox("Type", fr.RULE_TYPES, key="new_rule_type")
        counterparty_type = c2.selectbox("Counterparty", fr.COUNTERPARTY_TYPES)
        counterparty_name = c3.text_input("Counterparty name (optional)", placeholder="e.g. Axis Bank, Rahul")

        account_label = st.selectbox(
            "Account this applies to", ["-- none --"] + list(account_options.keys()),
            help="Add the account first in the Accounts tab if it's not listed yet.",
        )
        account_key = account_options.get(account_label)

        rule = {"name": name, "rule_type": rule_type, "counterparty_type": counterparty_type,
                "counterparty_name": counterparty_name or None, "account_key": account_key}

        if rule_type == "Interest":
            c1, c2, c3 = st.columns(3)
            rule["rate_percent"] = c1.number_input("Annual interest rate %", min_value=0.0, step=0.1, format="%.2f")
            rule["basis_amount"] = c2.number_input("Balance this rate applies to (₹)", min_value=0.0, step=1000.0)
            rule["frequency"] = c3.selectbox("Credited", fr.FREQUENCIES)
            rule["match_category"] = st.text_input("Category actual credits should have",
                                                     value="Bank Interest Income")
        elif rule_type == "Cashback":
            c1, c2, c3 = st.columns(3)
            rule["rate_percent"] = c1.number_input("Cashback rate %", min_value=0.0, step=0.1, format="%.2f")
            rule["basis_category"] = c2.text_input("Spend category it applies to (blank = all spend)",
                                                     placeholder="e.g. Fuel")
            rule["frequency"] = c3.selectbox("Credited", fr.FREQUENCIES, key="cb_freq")
            rule["match_category"] = st.text_input("Category actual credits should have",
                                                     value="Cashback/Rewards")
        elif rule_type == "Loan EMI":
            c1, c2, c3 = st.columns(3)
            rule["principal"] = c1.number_input("Loan principal (₹)", min_value=0.0, step=10000.0)
            rule["annual_rate"] = c2.number_input("Annual interest rate %", min_value=0.0, step=0.1, format="%.2f")
            rule["tenure_months"] = int(c3.number_input("Tenure (months)", min_value=1, step=1, value=60))
            rule["start_date"] = st.date_input("First EMI date")
            rule["match_category"] = st.text_input("Category actual debits should have",
                                                     value="Home Loan EMI" if counterparty_type != "Relative" else "Personal Loan EMI")
        else:  # Recurring
            c1, c2, c3 = st.columns(3)
            rule["expected_amount"] = c1.number_input("Expected amount (₹)", min_value=0.0, step=100.0)
            rule["due_day"] = int(c2.number_input("Roughly which day of month", min_value=1, max_value=31, step=1, value=1))
            rule["direction"] = c3.selectbox("Direction", ["credit", "debit"])
            rule["match_category"] = st.text_input("Category actual transactions should have (optional)")

        if st.button("Save rule"):
            if not name:
                st.error("Give it a name first.")
            else:
                rule["id"] = fr.make_rule_id(name, rule_type)
                save_rule(rule)
                st.success(f"Saved rule '{name}'.")
                st.rerun()

    st.divider()
    rules = get_rules()
    if not rules:
        st.info("No rules yet — add one above.")
    else:
        target_month = st.date_input("Check the period containing", value=date.today(), key="rules_period")
        df = load_df()

        account_labels = {m["key"]: m["label"] for m in masters}
        rows_out = []
        for rule in rules:
            period_start, period_end = fr.period_bounds(rule.get("frequency") or "Monthly", target_month)
            result = fr.cross_check(rule, df, period_start, period_end)
            rows_out.append({
                "Name": rule["name"],
                "Type": rule["rule_type"],
                "Counterparty": f"{rule.get('counterparty_type') or ''} {rule.get('counterparty_name') or ''}".strip(),
                "Account": account_labels.get(rule.get("account_key"), rule.get("account_key")),
                "Period": f"{period_start.date()} to {period_end.date()}",
                "Expected": result["expected"],
                "Actual": result["actual"],
                "Status": result["status"],
                "_id": rule["id"],
            })
        result_df = pd.DataFrame(rows_out)

        def _status_color(val):
            return {
                "Matched": "background-color: #1e4620",
                "Mismatch": "background-color: #5c3a0d",
                "Missing": "background-color: #5c1a1a",
            }.get(val, "")

        st.dataframe(
            result_df.drop(columns=["_id"]).style.map(_status_color, subset=["Status"]),
            use_container_width=True,
        )

        for rule in rules:
            if rule["rule_type"] == "Loan EMI" and rule.get("principal") and rule.get("tenure_months"):
                with st.expander(f"📐 Amortization schedule — {rule['name']}"):
                    schedule = fr.amortization_schedule(
                        rule["principal"], rule.get("annual_rate") or 0,
                        int(rule["tenure_months"]), rule["start_date"],
                    )
                    st.dataframe(schedule, use_container_width=True, height=250)

        del_name = st.selectbox("Delete a rule", ["-- none --"] + [r["name"] for r in rules])
        if del_name != "-- none --" and st.button("Delete selected rule"):
            match = next(r for r in rules if r["name"] == del_name)
            delete_rule(match["id"])
            st.success(f"Deleted '{del_name}'.")
            st.rerun()
