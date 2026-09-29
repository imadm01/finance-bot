import re

# word the user types -> official category name
# Note: keep every key lowercase — the message is lowercased before matching,
# so a capitalized key here (as your old file had for "Zomato"/"Swiggy") would
# never match anything. That's fixed below.
CATEGORY_ALIASES = {
    "food": "Food", "lunch": "Food", "dinner": "Food", "snacks": "Food", "groceries": "Food",
    "fruits": "Food", "zomato": "Food", "swiggy": "Food", "chai": "Food",
    "travel": "Travel", "uber": "Travel", "bus": "Travel", "fuel": "Travel", "auto": "Travel",
    "flight": "Travel", "metro": "Travel",
    "savings": "Savings", "save": "Savings", "sip": "Savings",
    "parents": "Parents", "home": "Parents", "mom": "Parents", "dad": "Parents",
    "rent": "Bills", "bills": "Bills", "recharge": "Bills",
    "shopping": "Shopping", "clothes": "Shopping", "perfume": "Shopping", "socks": "Shopping",
    "netflix": "Entertainment", "movie": "Entertainment", "health": "Health",
}

PATTERN = re.compile(r"^(\+)?\s*(\d+(?:\.\d+)?)\s*(.*)$")


def parse_message(text):
    """'250 lunch food' -> dict, or None if it isn't a transaction."""
    match = PATTERN.match(text.strip())
    if not match:
        return None

    is_income = match.group(1) == "+"
    amount = float(match.group(2))
    words = match.group(3).lower().split()

    # category stays None until we find a matching word — that's how the bot
    # knows to show the category-picker buttons instead of guessing "Misc".
    category = "Income" if is_income else None
    note_words = []
    for word in words:
        if word in CATEGORY_ALIASES and category in (None, "Income"):
            category = CATEGORY_ALIASES[word]
        else:
            note_words.append(word)

    return {
        "type": "income" if is_income else "expense",
        "amount": amount,
        "category": category,
        "note": " ".join(note_words),
    }
