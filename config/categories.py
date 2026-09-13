"""Keyword rules used to auto-suggest a category and office/personal tag
for a transaction. These are starting defaults — edit freely, and any
manual correction made in the Review tab is remembered (via core.storage)
so the same merchant/description is not mis-tagged again."""

# Order matters: first matching category wins.
CATEGORY_RULES = [
    ("Salary Income", ["salary", "payroll", "sal credit"]),
    ("Business Income", ["invoice", "client payment", "consulting fee", "professional fee"]),
    ("Home Loan EMI", ["home loan", "housing loan", "hl emi"]),
    ("Personal Loan EMI", ["personal loan", "pl emi"]),
    ("Loan EMI", ["loan emi", "emi debit", "nach emi"]),
    ("Bank Interest Income", ["savings interest", "sb int", "interest credit", "int.pd", "int credit"]),
    ("Fixed Deposit", ["fixed deposit", "fd created", "fd booked", "term deposit", "fdr "]),
    ("Cashback/Rewards", ["cashback", "cash back", "reward point", "reward redemption", "reward credit"]),
    ("Interest/Dividend Income", ["dividend", "div credit"]),
    ("Trading/Investment", ["zerodha", "groww", "upstox", "icici direct", "kite", "demat", "cdsl", "nse", "bse", "mutual fund", "sip "]),
    ("Rent", ["rent"]),
    ("Utilities", ["electricity", "water bill", "gas bill", "broadband", "wifi", "internet bill"]),
    ("Communication", ["airtel", "jio", "vodafone", "vi recharge", "mobile recharge", "postpaid"]),
    ("Travel", ["uber", "ola", "irctc", "indigo", "makemytrip", "yatra", "redbus", "taxi", "cab fare", "flight"]),
    ("Fuel", ["petrol", "diesel", "fuel", "hpcl", "iocl", "bharat petroleum", "shell "]),
    ("Food & Dining", ["swiggy", "zomato", "restaurant", "cafe", "food court", "dining"]),
    ("Office Supplies", ["stationery", "printer", "cartridge", "office supplies", "courier", "xerox"]),
    ("Software/Subscriptions", ["aws", "google cloud", "azure", "github", "adobe", "microsoft 365", "zoom", "saas", "subscription", "netflix", "hotstar"]),
    ("Client Entertainment", ["client lunch", "client dinner", "hospitality"]),
    ("Bank Charges", ["bank charge", "annual fee", "late fee", "gst on charges", "penal", "atm charge"]),
    ("Taxes", ["income tax", "advance tax", "tds", "gst payment"]),
    ("Insurance", ["premium", "insurance", "lic "]),
    ("Medical", ["pharmacy", "hospital", "clinic", "medical", "diagnostic"]),
    ("Shopping/Personal", ["amazon", "flipkart", "myntra", "mall", "shopping"]),
    ("Self Transfer", ["self transfer", "own account", "imps to self"]),
]

DEFAULT_CATEGORY = "Uncategorized"

# Categories that should default is_office = True unless the user overrides.
OFFICE_DEFAULT_CATEGORIES = {
    "Office Supplies",
    "Software/Subscriptions",
    "Client Entertainment",
    "Communication",
    "Travel",
    "Fuel",
}

INCOME_CATEGORIES = {
    "Salary Income",
    "Business Income",
    "Interest/Dividend Income",
}


def suggest_category(description: str) -> str:
    text = (description or "").lower()
    for category, keywords in CATEGORY_RULES:
        for kw in keywords:
            if kw in text:
                return category
    return DEFAULT_CATEGORY


def suggest_is_office(category: str) -> bool:
    return category in OFFICE_DEFAULT_CATEGORIES
