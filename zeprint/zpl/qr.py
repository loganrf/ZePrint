"""QR sizing helpers (deterministic, so layouts can reserve space for ``^BQ``)."""

# Byte-mode capacity (bytes) by QR version at error-correction level M.
_QR_M_CAP = {1: 14, 2: 26, 3: 42, 4: 62, 5: 84, 6: 106, 7: 122, 8: 152,
             9: 180, 10: 213, 11: 251, 12: 287, 13: 322, 14: 370, 15: 428}


def qr_modules(data_len: int, ec_cap: dict[int, int] = _QR_M_CAP) -> int:
    """Side length in modules for a byte-mode QR holding ``data_len`` bytes at EC M."""
    for v, cap in sorted(ec_cap.items()):
        if data_len <= cap:
            return 17 + 4 * v
    return 17 + 4 * max(ec_cap)     # clamp; long URLs still size sanely
