"""Synthetic launch scope shared by the engine and the hosted runner."""

FIXTURE_SYMBOLS = ("F00", "F01", "F02")
MAX_SESSION_MINUTES = 20


def valid_symbols(symbols: object) -> bool:
    return (isinstance(symbols, tuple) and 1 <= len(symbols) <= len(FIXTURE_SYMBOLS)
            and all(symbol in FIXTURE_SYMBOLS for symbol in symbols)
            and len(set(symbols)) == len(symbols))
