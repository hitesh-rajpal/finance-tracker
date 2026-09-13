import hmac
import io
import uuid
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from core.storage import (
    init_db, upsert_transactions, fetch_all, make_txn_id, delete_transactions, delete_all,
    ensure_account, get_account_master, update_account, update_transaction,
    save_rule, get_rules, delete_rule, ensure_party, get_party_master, delete_party, rename_party,
    save_rate_change, get_rate_changes, delete_rate_change,
    save_upload_batch, get_upload_batches, save_upload_matches, get_upload_matches,
    delete_upload_batch, get_batch_transactions,
)
from core.categorize import categorize, apply_correction, is_office_for_category
from core import reports
from core import financial_rules as fr
from core import auth
from parsers import sms_parser, statement_parser, contract_note_parser

try:
    from streamlit_cookies_controller import CookieController
except ImportError:
    CookieController = None

st.set_page_config(page_title="Finance Tracker", layout="wide")

REMEMBER_COOKIE = "finance_tracker_remember_token"
REMEMBER_MAX_AGE = 60 * 60 * 24 * 30  # 30 days


def _legacy_password_gate():
    """Old single-shared-password check — kept only as a fallback for while
    [supabase_auth] isn't configured yet in secrets, so the deployed app
    doesn't break mid-migration to real login."""
    def on_submit():
        entered = st.session_state.get("password_input", "")
        expected = st.secrets.get("APP_PASSWORD", "")
        if expected and hmac.compare_digest(entered, expected):
            st.session_state["authenticated"] = True
        else:
            st.session_state["auth_failed"] = True

    st.caption("(Temporary fallback login — real email/password login isn't configured yet.)")
    st.text_input("Password", type="password", key="password_input", on_change=on_submit)
    if st.session_state.get("auth_failed"):
        st.error("Incorrect password.")


def require_login():
    """Gates the whole app behind real Supabase Auth email/password login
    (with a working forgot-password flow and an optional "Remember me" that
    persists across browser restarts via a cookie holding Supabase's
    refresh_token) once [supabase_auth] is set in secrets; falls back to the
    old shared-password check until then. This app is deployed to a public
    URL and holds bank/trading data, so nothing below this check should
    render for an unauthenticated visitor."""
    if st.session_state.get("authenticated"):
        return

    st.title("🔒 Finance Tracker")
    auth_cfg = st.secrets.get("supabase_auth")

    if not auth_cfg:
        _legacy_password_gate()
        st.stop()

    controller = CookieController() if CookieController else None

    # Try a silent re-login from a remembered refresh token before showing
    # the form at all. The cookie component reports its value asynchronously,
    # so this may only succeed a rerun or two after the page first loads —
    # that's fine, it just means a brief flash of the login form once.
    if controller is not None and not st.session_state.get("_remember_tried"):
        remembered = controller.get(REMEMBER_COOKIE)
        if remembered:
            ok, result = auth.refresh_session(auth_cfg["url"], auth_cfg["anon_key"], remembered)
            if ok:
                st.session_state["authenticated"] = True
                st.session_state["user_email"] = result.get("user", {}).get("email", "")
                new_refresh = result.get("refresh_token")
                if new_refresh:
                    controller.set(REMEMBER_COOKIE, new_refresh, max_age=REMEMBER_MAX_AGE)
                st.rerun()
        st.session_state["_remember_tried"] = True

    def on_submit():
        email = st.session_state.get("login_email", "").strip()
        password = st.session_state.get("login_password", "")
        ok, result = auth.sign_in(auth_cfg["url"], auth_cfg["anon_key"], email, password)
        if ok:
            st.session_state["authenticated"] = True
            st.session_state["user_email"] = result.get("user", {}).get("email", "")
            if st.session_state.get("remember_me") and controller is not None:
                controller.set(REMEMBER_COOKIE, result["refresh_token"], max_age=REMEMBER_MAX_AGE)
        else:
            st.session_state["auth_failed"] = result

    st.text_input("Email", key="login_email")
    st.text_input("Password", type="password", key="login_password", on_change=on_submit)
    st.checkbox("Remember me on this device", value=True, key="remember_me",
                disabled=controller is None,
                help=None if controller else "Cookie support isn't installed, so this can't persist.")
    if st.button("Log in"):
        on_submit()
    if st.session_state.get("auth_failed"):
        st.error(st.session_state["auth_failed"])

    with st.expander("Forgot password?"):
        reset_email = st.text_input("Your email", key="reset_email")
        if st.button("Send reset link"):
            auth.send_password_reset(auth_cfg["url"], auth_cfg["anon_key"], reset_email.strip())
            st.success("If that email has an account, a reset link has been sent to it.")

    st.stop()


def render_logout():
    """Sidebar control to end the session and forget this device, for when
    Remember me was used and someone wants to actually log out."""
    auth_cfg = st.secrets.get("supabase_auth")
    with st.sidebar:
        if st.session_state.get("user_email"):
            st.caption(f"Logged in as {st.session_state['user_email']}")
        if st.button("Log out"):
            if auth_cfg and CookieController:
                CookieController().remove(REMEMBER_COOKIE)
            for k in ("authenticated", "user_email", "_remember_tried"):
                st.session_state.pop(k, None)
            st.rerun()


require_login()
init_db()
ensure_party("Office")
render_logout()


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
    rest. Returns (saved_count, skipped_as_already_recorded_count). Also logs
    an upload_batches row (what/when, with counts) and, for every skipped
    row, an upload_matches row recording exactly which existing transaction
    it matched and why — visible in the Uploads tab."""
    if not rows:
        return 0, 0

    for r in rows:
        has_category = bool(r.get("category"))
        has_is_office = "is_office" in r
        if not has_category:
            cat, auto_is_office = categorize(r.get("description", ""))
            r["category"] = cat
            if not has_is_office:
                r["is_office"] = auto_is_office
            r["edit_source"] = "auto"
        else:
            if not has_is_office:
                r["is_office"] = is_office_for_category(r["category"])
            r["edit_source"] = "manual" if r.get("source") == "manual" else (
                "sms_tag" if r.get("source") == "sms" else (
                    "bank_category" if r.get("source") == "statement" else "auto"
                )
            )
        r["is_office"] = bool(r.get("is_office"))
        if not r.get("parties"):
            r["parties"] = ["Office"] if r["is_office"] else []
        for p in r["parties"]:
            ensure_party(p)
        for k in ("account", "bank", "source_file", "scrip", "raw_text"):
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

    batch_id = uuid.uuid4().hex[:16]
    for r in unmatched:
        r["upload_batch_id"] = batch_id
    n = upsert_transactions(unmatched)

    save_upload_batch({
        "id": batch_id,
        "source_file": rows[0].get("source_file"),
        "source_type": rows[0].get("source"),
        "parsed_count": len(rows),
        "saved_count": n,
        "skipped_count": len(matched),
    })
    save_upload_matches([
        {
            "id": uuid.uuid4().hex[:16],
            "batch_id": batch_id,
            "new_raw_text": m["new"].get("raw_text") or m["new"].get("description"),
            "new_amount": m["new"]["amount"],
            "new_date": m["new"]["date"],
            "matched_transaction_id": m["matched_id"],
            "match_reason": m["reason"],
        }
        for m in matched
    ])

    return n, len(matched)


st.title("Office Expenses, Bank & Trading Tracker")
st.caption("Data is stored in your private Supabase project — accessible only with the app password.")

(tab_upload, tab_uploads_log, tab_review, tab_accounts, tab_parties, tab_reports, tab_trading,
 tab_recon, tab_rules) = st.tabs(
    ["📥 Upload", "📁 Uploads", "🏷️ Review & Categorize", "🏦 Accounts", "🎭 Parties", "📊 Reports",
     "📈 Trading", "🔗 Reconciliation", "🎯 Expected & Cross-Check"]
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
        msg += " See the 📁 Uploads tab for the itemized list of exactly what was saved vs skipped, and why."
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
                msg += " See the 📁 Uploads tab for the itemized list."
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
                msg += " See the 📁 Uploads tab for the itemized list."
                st.success(msg)

    st.divider()
    st.subheader("Cash / manual entry")
    st.caption("For anything with no SMS or statement trail — cash spend, or a correction you know by hand.")
    with st.form("manual_entry_form", clear_on_submit=True):
        c1, c2, c3 = st.columns(3)
        m_date = c1.date_input("Date", value=date.today())
        m_amount = c2.number_input("Amount (₹)", min_value=0.0, step=10.0)
        m_direction = c3.selectbox("Direction", ["debit", "credit"])
        c1, c2 = st.columns(2)
        m_desc = c1.text_input("Description", placeholder="e.g. Auto fare, cash paid")
        m_category = c2.text_input("Category (optional — leave blank to auto-suggest)")
        if st.form_submit_button("Add cash transaction"):
            if not m_desc.strip():
                st.error("Add a description first.")
            else:
                row = {
                    "date": m_date.isoformat(), "amount": float(m_amount), "direction": m_direction,
                    "description": m_desc.strip(), "source": "manual", "source_file": "cash entry",
                    "bank": "Cash", "account": "Cash",
                }
                if m_category.strip():
                    row["category"] = m_category.strip()
                n, skipped = save_rows([row])
                if n:
                    st.success("Added.")
                elif skipped:
                    st.warning("This matched an existing transaction and wasn't added as a duplicate.")
                st.rerun()

# --------------------------------------------------------- Uploads tab ----
with tab_uploads_log:
    st.caption(
        "Every 'Parse & save' / 'Add transactions' click, with what happened. Expand a batch to see "
        "exactly which rows were newly saved and which were skipped because they matched something "
        "already recorded (and which existing transaction, and why). Delete a batch to remove every "
        "transaction it saved."
    )
    batches = get_upload_batches()
    if not batches:
        st.info("No uploads yet.")
    else:
        for b in batches:
            title = (
                f"{b['uploaded_at']:%Y-%m-%d %H:%M} — {b['source_file'] or '(unnamed)'} "
                f"({b['source_type']}) — parsed {b['parsed_count']}, saved {b['saved_count']}, "
                f"skipped {b['skipped_count']}"
            )
            with st.expander(title):
                saved_rows = get_batch_transactions(b["id"]) if b["saved_count"] else []
                st.markdown(f"**✅ Saved (new) — {len(saved_rows)}:**")
                if saved_rows:
                    st.dataframe(
                        pd.DataFrame(saved_rows)[["date", "amount", "direction", "category", "description"]],
                        use_container_width=True, hide_index=True,
                    )
                elif b["saved_count"]:
                    st.caption(f"{b['saved_count']} row(s) were saved here originally but have since been "
                               "deleted or edited elsewhere.")
                else:
                    st.caption("Nothing new — every parsed row already existed.")

                st.markdown(f"**⏭️ Skipped (already recorded) — {b['skipped_count']}:**")
                if b["skipped_count"]:
                    matches = get_upload_matches(b["id"])
                    for m in matches:
                        reason = "same reference number" if m["match_reason"] == "reference_number" else "same amount/date"
                        st.markdown(
                            f"- Incoming ({m['new_date']}, ₹{m['new_amount']:,.2f}): "
                            f"`{(m['new_raw_text'] or '')[:120]}`\n\n"
                            f"  → matched existing transaction from **{m['matched_source'] or '?'}** "
                            f"(`{m['matched_source_file'] or '?'}`, {m['matched_date']}, "
                            f"₹{m['matched_amount']:,.2f}): `{(m['matched_description'] or '')[:120]}` "
                            f"— matched by {reason}"
                        )
                else:
                    st.caption("Nothing was skipped — every parsed row was new.")

                confirm_key = f"confirm_del_{b['id']}"
                confirm = st.checkbox(f"Yes, delete the {b['saved_count']} transaction(s) this batch saved",
                                       key=confirm_key)
                if st.button("Delete this batch", key=f"del_{b['id']}", disabled=not confirm):
                    delete_upload_batch(b["id"], b["source_file"])
                    st.success("Batch and its transactions deleted.")
                    st.rerun()

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
                          "description", "category", "parties", "source", "source_file", "edit_source"]].copy()
        editable["parties"] = editable["parties"].apply(lambda ps: ", ".join(ps))
        editable = editable.rename(columns={
            "source": "linked from", "source_file": "upload file", "edit_source": "how it was set",
        })
        edited = st.data_editor(
            editable, use_container_width=True, hide_index=True, key="editor",
            disabled=["id", "date", "amount", "direction", "account", "bank", "description",
                      "linked from", "upload file", "how it was set"],
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
        with st.expander("🎭 Quick-assign parties to one transaction (pick from master list)"):
            st.caption(
                "The table above uses free-text (fast for bulk edits); this picks from your saved "
                "Parties master list instead, so there's no risk of a typo creating a near-duplicate "
                "name. Add new names in the 🎭 Parties tab first if they're not listed yet."
            )
            pick_options = {
                f"{r['date'].date()} — ₹{r['amount']:,.2f} — {r['description'][:60]}": r["id"]
                for _, r in view.iterrows()
            }
            if not pick_options:
                st.caption("No transactions in the current filter to pick from.")
            else:
                picked_label = st.selectbox("Transaction", list(pick_options.keys()), key="quick_assign_pick")
                picked_id = pick_options[picked_label]
                current = view.loc[view["id"] == picked_id, "parties"].iloc[0]
                master_now = get_party_master()
                chosen = st.multiselect("Parties", options=master_now, default=[p for p in current if p in master_now],
                                         key="quick_assign_multiselect")
                if st.button("Set parties for this transaction", key="quick_assign_save"):
                    is_office = any(p.lower() == "office" for p in chosen)
                    row_category = view.loc[view["id"] == picked_id, "category"].iloc[0]
                    update_transaction(picked_id, row_category, is_office, chosen)
                    st.success(f"Parties set to: {', '.join(chosen) if chosen else '(none)'}.")
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

# -------------------------------------------------------- Parties tab -----
with tab_parties:
    st.caption(
        "The master list of names a transaction's Parties can be picked from (used in Review & "
        "Categorize). 'Office' always exists — it's what drives the office-expense reports."
    )
    known_parties = get_party_master()
    c1, c2 = st.columns([3, 1])
    new_party = c1.text_input("Add a new party", placeholder="e.g. Brother, Ram, Sham", key="new_party_input")
    if c2.button("Add", key="add_party_btn") and new_party.strip():
        ensure_party(new_party.strip())
        st.success(f"Added '{new_party.strip()}'.")
        st.rerun()

    if not known_parties:
        st.info("No parties yet — add one above, or tag a transaction with one in Review & Categorize.")
    else:
        df_all = load_df()
        counts = reports.party_breakdown(df_all) if not df_all.empty else pd.DataFrame()
        count_map = dict(zip(counts["party"], counts["count"])) if not counts.empty else {}
        for name in known_parties:
            c1, c2, c3 = st.columns([3, 2, 1])
            c1.write(f"**{name}**")
            c2.caption(f"{count_map.get(name, 0)} transaction(s)")
            if name.lower() != "office" and c3.button("Remove", key=f"rm_party_{name}"):
                delete_party(name)
                st.success(f"Removed '{name}' from the list (past transactions keep the tag).")
                st.rerun()

        with st.expander("Rename a party (updates past transactions too)"):
            rename_from = st.selectbox("Rename", known_parties, key="rename_from")
            rename_to = st.text_input("To", key="rename_to")
            if st.button("Rename", key="rename_btn") and rename_to.strip():
                rename_party(rename_from, rename_to.strip())
                st.success(f"Renamed '{rename_from}' to '{rename_to.strip()}' everywhere.")
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
            rc = None
            if rule["rule_type"] == "Loan EMI" and rule.get("start_date"):
                stored_changes = get_rate_changes(rule["id"])
                if stored_changes:
                    rc = fr.rate_changes_for_schedule(rule["start_date"], stored_changes)
            result = fr.cross_check(rule, df, period_start, period_end, rate_changes=rc)
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
            base = "color: white; font-weight: 600; "
            return base + {
                "Matched": "background-color: #1e7a34",
                "Mismatch": "background-color: #b06a1a",
                "Missing": "background-color: #b02a2a",
            }.get(val, "background-color: #555; ")

        st.dataframe(
            result_df.drop(columns=["_id"]).style.map(_status_color, subset=["Status"]),
            use_container_width=True,
        )

        for rule in rules:
            if rule["rule_type"] == "Loan EMI" and rule.get("principal") and rule.get("tenure_months"):
                with st.expander(f"📐 Amortization schedule — {rule['name']}"):
                    stored_changes = get_rate_changes(rule["id"])
                    if stored_changes:
                        st.markdown("**Rate changes on record:**")
                        for c in stored_changes:
                            cc1, cc2 = st.columns([5, 1])
                            cc1.write(f"{c['effective_date']}: → {c['new_annual_rate']}%")
                            if cc2.button("Remove", key=f"rm_rate_{c['id']}"):
                                delete_rate_change(c["id"])
                                st.rerun()
                    with st.form(key=f"add_rate_change_{rule['id']}"):
                        st.caption("Record a floating-rate reset — the EMI recalculates on the balance "
                                   "at that date for whatever tenure remains, keeping the original payoff date.")
                        c1, c2 = st.columns(2)
                        eff_date = c1.date_input("Effective from", key=f"eff_date_{rule['id']}")
                        new_rate = c2.number_input("New annual rate %", min_value=0.0, step=0.1,
                                                    format="%.2f", key=f"new_rate_{rule['id']}")
                        if st.form_submit_button("Add rate change"):
                            save_rate_change({
                                "id": uuid.uuid4().hex[:16], "rule_id": rule["id"],
                                "effective_date": eff_date, "new_annual_rate": new_rate,
                            })
                            st.rerun()

                    rc = fr.rate_changes_for_schedule(rule["start_date"], stored_changes) if stored_changes else None
                    schedule = fr.amortization_schedule(
                        rule["principal"], rule.get("annual_rate") or 0,
                        int(rule["tenure_months"]), rule["start_date"], rate_changes=rc,
                    )
                    st.dataframe(schedule, use_container_width=True, height=250)

        del_name = st.selectbox("Delete a rule", ["-- none --"] + [r["name"] for r in rules])
        if del_name != "-- none --" and st.button("Delete selected rule"):
            match = next(r for r in rules if r["name"] == del_name)
            delete_rule(match["id"])
            st.success(f"Deleted '{del_name}'.")
            st.rerun()
