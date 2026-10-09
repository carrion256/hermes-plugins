def apply_batch(state, events):
    # Buffer all changes so a failure (including while consuming the
    # iterable) leaves the caller's state untouched.
    balances = dict(state['balances'])
    new_seen = set()
    for event in events:
        event_id = event['id']
        if event_id in state['seen'] or event_id in new_seen:
            continue
        account = event['account']
        balance = balances.get(account, 0) + event['delta']
        if balance < 0:
            raise ValueError('insufficient funds')
        balances[account] = balance
        new_seen.add(event_id)
    state['balances'] = balances
    state['seen'] = state['seen'] | new_seen
    return state
