def page_count(total, size):
    # Ceiling division with integers (exact for arbitrarily large values).
    # total >= 0, size > 0: zero items -> zero pages; any remainder needs
    # one extra partially filled page.
    return -(-total // size)
