"""Fitting oversized text into a prompt, counted in characters.

The token-accurate counterpart lives in token_utils, which needs tiktoken. This
module deliberately has no imports: it runs inside agent loops that must not
gain a new runtime dependency, and a character budget is close enough when the
goal is to stop a response from being reread on turn after turn.
"""


def truncate_middle_chars(value, max_chars: int) -> str:
    """Shorten text to roughly max_chars, keeping both ends.

    The middle is what gets dropped. Structured responses lead with their
    strongest rows and often close with a total or a summary, so the centre of a
    long list is the least costly part to lose.

    The omitted length is stated in place of the removed text, so a reader treats
    the result as a sample of a larger response rather than as the whole of a
    small one.
    """
    text = value if isinstance(value, str) else str(value)
    if len(text) <= max_chars:
        return text

    half = max_chars // 2
    omitted = len(text) - 2 * half
    return (
        text[:half].rstrip()
        + "\n\n... [{} characters omitted from the middle] ...\n\n".format(omitted)
        + text[-half:].lstrip()
    )
