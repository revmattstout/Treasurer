"""Optional Claude-powered helpers.

Everything here is optional: the app works without an API key, and these
features turn on when ANTHROPIC_API_KEY (or another Anthropic credential)
is set. They are used to:
  * read receipts (photos or PDFs) and fill in vendor / date / total,
  * read PDF bank statements when no CSV/OFX export is available,
  * suggest categories for transactions no rule matched,
  * draft a plain-language summary of a monthly report for the board.
"""
import base64
import json
import os
from typing import List, Optional

from pydantic import BaseModel, Field

MODEL = os.environ.get("TREASURER_MODEL", "claude-opus-5-5")

_client = None


def enabled():
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def client():
    global _client
    if _client is None:
        import anthropic
        # Bounded waits: a stuck call must never tie up the web server.
        _client = anthropic.Anthropic(timeout=120.0, max_retries=1)
    return _client


class AIError(RuntimeError):
    pass


def _parse(content, output_format, system, effort="medium", max_tokens=16000, timeout=120.0):
    import anthropic
    try:
        resp = client().with_options(timeout=timeout).beta.messages.parse(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_format=output_format,
            output_config={"effort": effort},
            # If the model declines, let the API retry on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
    except anthropic.AuthenticationError:
        raise AIError("The Anthropic API key was rejected.")
    except anthropic.RateLimitError:
        raise AIError("Rate limited by the Anthropic API. Try again in a minute.")
    except anthropic.BadRequestError as e:
        raise AIError(f"The request was rejected: {e.message}")
    except anthropic.APIStatusError as e:
        raise AIError(f"Anthropic API error ({e.status_code}). Try again later.")
    except anthropic.APITimeoutError:
        raise AIError("Claude took too long to respond. Try again, or enter the details by hand.")
    except anthropic.APIConnectionError:
        raise AIError("Couldn't reach the Anthropic API.")
    if resp.stop_reason == "refusal":
        raise AIError("Claude declined to process this file.")
    if resp.stop_reason == "max_tokens":
        raise AIError("The document was too long to process in one pass.")
    if resp.parsed_output is None:
        raise AIError("Claude's response couldn't be read.")
    return resp.parsed_output


def _file_block(data: bytes, mime: str):
    b64 = base64.standard_b64encode(data).decode("ascii")
    if mime == "application/pdf":
        return {"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
    if mime in ("image/jpeg", "image/png", "image/gif", "image/webp"):
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
    raise AIError(f"Unsupported file type for reading: {mime}")


# ---------------------------------------------------------------- receipts

class ReceiptInfo(BaseModel):
    document_type: str = Field(description="'receipt' for a purchase receipt or invoice, 'check' if this is a "
                                           "photo of one or more paper checks or a deposit slip, else 'other'")
    vendor: Optional[str] = Field(description="Store or payee name as printed")
    date: Optional[str] = Field(description="Purchase date as YYYY-MM-DD")
    total: Optional[float] = Field(description="Grand total paid, positive number, including tax and tip")
    category: Optional[str] = Field(description="Best matching category from the provided list")
    purpose: Optional[str] = Field(description="Short guess at what was bought, e.g. 'Groceries for Tuesday dinner'")


def read_receipt(data: bytes, mime: str, categories: List[str]) -> ReceiptInfo:
    system = (
        "You read receipts for the treasurer of a small campus ministry nonprofit. "
        "Extract exactly what is printed. If a field is not legible or not present, return null "
        "rather than guessing."
    )
    content = [
        _file_block(data, mime),
        {"type": "text", "text": "Extract this receipt. Choose category from: " + "; ".join(categories)},
    ]
    info = _parse(content, ReceiptInfo, system, effort="low", max_tokens=4000, timeout=90.0)
    if info.category not in categories:
        info.category = None
    return info


# ---------------------------------------------------------------- checks

class CheckItem(BaseModel):
    payer: Optional[str] = Field(description="Name of the person or organization who wrote the check "
                                             "(printed name/address block at top left, or the signature)")
    amount: Optional[float] = Field(description="Check amount as a positive number (use the written-out amount if the numbers are unclear)")
    date: Optional[str] = Field(description="Date written on the check, YYYY-MM-DD")
    check_number: Optional[str] = Field(description="Check number printed at the top right")
    memo: Optional[str] = Field(description="Text on the memo line, if any")


class CheckPhoto(BaseModel):
    checks: List[CheckItem] = Field(description="Every check visible in the image, one entry each. "
                                                "Do not list a deposit slip as a check.")


def read_checks(data: bytes, mime: str) -> CheckPhoto:
    system = (
        "You read photos of paper checks being deposited by the treasurer of a small campus ministry. "
        "A photo may show one check or several, or be a PDF scan. Extract each check exactly as written. "
        "Never transcribe bank account numbers or routing numbers (the numbers along the bottom edge). "
        "If a field is not legible, return null rather than guessing."
    )
    content = [_file_block(data, mime), {"type": "text", "text": "List every check in this image."}]
    result = _parse(content, CheckPhoto, system, effort="medium", max_tokens=8000, timeout=120.0)
    for c in result.checks:
        if c.amount is not None:
            c.amount = abs(c.amount)
    return result


# ---------------------------------------------------------------- PDF statements

class StatementLine(BaseModel):
    date: str = Field(description="Posting date as YYYY-MM-DD")
    description: str
    amount: float = Field(description="Signed amount: negative for withdrawals/debits/checks, positive for deposits/credits")
    balance: Optional[float] = Field(default=None, description="Running balance after this line if printed")


class Statement(BaseModel):
    account_last4: Optional[str] = None
    ending_balance: Optional[float] = None
    transactions: List[StatementLine]


def read_pdf_statement(data: bytes) -> Statement:
    system = (
        "You transcribe bank statements into structured data for a nonprofit treasurer. "
        "Include every deposit, withdrawal, check, fee and interest line exactly once. "
        "Skip summary totals and daily balance tables. Use the statement period to infer the "
        "year when lines only show month/day."
    )
    content = [_file_block(data, "application/pdf"),
               {"type": "text", "text": "List every transaction on this statement."}]
    return _parse(content, Statement, system, effort="medium", max_tokens=32000, timeout=240.0)


# ---------------------------------------------------------------- categorization

class Suggestion(BaseModel):
    id: int
    category: Optional[str] = Field(description="Category name from the list, or null if unsure")


class Suggestions(BaseModel):
    items: List[Suggestion]


def suggest_categories(transactions, income_categories, expense_categories):
    """transactions: list of dicts with id, date, description, amount."""
    system = (
        "You help the treasurer of the VSU Wesley Foundation (a United Methodist campus ministry "
        "at Valdosta State University) categorize bank transactions. Deposits (positive) must get an "
        "income category or 'Online Giving Payout' / 'Transfer'; withdrawals (negative) must get an "
        "expense category or 'Transfer'. Return null when the description doesn't make the "
        "purpose reasonably clear — a person will review it."
    )
    text = (
        "Income categories: " + "; ".join(income_categories) + "\n"
        "Expense categories: " + "; ".join(expense_categories) + "\n\n"
        "Transactions (JSON):\n" + json.dumps(transactions)
    )
    allowed = set(income_categories) | set(expense_categories)
    result = _parse([{"type": "text", "text": text}], Suggestions, system, effort="low")
    return {s.id: s.category for s in result.items if s.category in allowed}


# ---------------------------------------------------------------- narrative

class Narrative(BaseModel):
    summary: str = Field(description="3-6 short paragraphs of plain text, no markdown headings")


def write_narrative(report: dict, org_name: str) -> str:
    system = (
        f"You are drafting the treasurer's monthly report narrative for {org_name}, a campus "
        "ministry nonprofit, to be read by its board of directors. Be factual, concise and warm. "
        "Use only the numbers provided. Point out notable changes from the prior month, categories "
        "over budget, expenses missing receipts, and anything that needs the board's attention. "
        "Do not invent explanations for spending; when a reason is unknown, say it may be worth asking."
    )
    content = [{"type": "text", "text": "Monthly report data (JSON):\n" + json.dumps(report, default=str)}]
    return _parse(content, Narrative, system, effort="medium", max_tokens=8000).summary
