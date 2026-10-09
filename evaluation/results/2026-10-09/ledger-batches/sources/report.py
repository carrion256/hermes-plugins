def summarize(state):
    return sorted(
        (account, balance)
        for account, balance in state['balances'].items()
        if balance != 0
    )
