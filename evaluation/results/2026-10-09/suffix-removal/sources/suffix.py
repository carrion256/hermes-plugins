def remove_suffix(text, suffix):
    if suffix and text.endswith(suffix):
        return text[: len(text) - len(suffix)]
    return text
