"""Render login tokens only as QR codes in an interactive local terminal."""

import sys

from telegram_mcp.domain.models import ToolError


def require_terminal() -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ToolError(
            "INTERACTIVE_LOGIN_REQUIRED",
            "Run login --qr yourself in a terminal with interactive input and output; "
            "do not redirect the QR code to a file or pipe.",
        )
    try:
        "\u00a0\u2580\u2584\u2588".encode(sys.stdout.encoding or "utf-8")
    except (LookupError, UnicodeError):
        raise ToolError(
            "QR_TERMINAL_UNSUPPORTED", "Use a Unicode terminal, such as Windows Terminal."
        ) from None


def show_qr(url: str) -> None:
    from qrcode import QRCode

    require_terminal()
    if sys.platform == "win32":
        from colorama import just_fix_windows_console

        just_fix_windows_console()
    code = QRCode(border=4)
    code.add_data(url)
    code.make(fit=True)
    print("\nOn your phone: Telegram > Settings > Devices > Link Desktop Device.")
    print("Select the correct account and scan the latest QR below.")
    try:
        code.print_ascii(out=sys.stdout, tty=True)
    except (OSError, UnicodeError):
        raise ToolError(
            "QR_TERMINAL_UNSUPPORTED", "Cannot display the QR code. Use a Unicode terminal."
        ) from None
    print(
        "Waiting for confirmation. Expired codes refresh automatically; Ctrl+C cancels.", flush=True
    )
