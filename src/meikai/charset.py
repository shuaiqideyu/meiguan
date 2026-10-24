"""算式字符：花式数字和字母收成 ASCII，再算出选项。"""

import re
import unicodedata

OPERATORS = {
    "+": lambda a, b: a + b,
    "-": lambda a, b: a - b,
    "−": lambda a, b: a - b,
    "*": lambda a, b: a * b,
    "×": lambda a, b: a * b,
    "✖": lambda a, b: a * b,
    "/": lambda a, b: a / b,
    "÷": lambda a, b: a / b,
}

FULLWIDTH_OP = {
    "＋": "+",
    "－": "-",
    "＊": "*",
    "／": "/",
    "＝": "=",
    "？": "?",
}

OP_CHARS = r"+\-−×✖*/÷＋－＊／"
MATH_RE = re.compile(rf"(\S+)\s*([{OP_CHARS}])\s*(\S+)\s*[=＝]\s*[?？]")

DIGIT_TO_ALPHA = {"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"}
ALPHA_TO_DIGIT = {
    "O": "0", "I": "1", "L": "1", "Z": "2", "S": "5", "G": "6", "B": "8", "D": "0", "Q": "0",
}
CONFUSIONS: dict[str, list[str]] = {
    "0": ["O", "D", "Q"], "O": ["0", "D", "Q"],
    "1": ["I", "L", "7", "4"], "I": ["1", "L"], "L": ["1", "I"],
    "4": ["1", "A"], "7": ["1", "T"],
    "2": ["Z"], "Z": ["2"],
    "5": ["S"], "S": ["5"],
    "6": ["G", "B"], "G": ["6"], "B": ["6", "8"],
    "8": ["B", "3"], "3": ["8"],
    "9": ["Q", "G"], "Q": ["9", "O"],
    "D": ["0", "O"], "T": ["7"],
}


def normalize_char(ch: str) -> str:
    if ch.isascii() and ch.isalnum():
        return ch.upper()
    cp = ord(ch)
    if 0x1F170 <= cp <= 0x1F189:
        return chr(ord("A") + cp - 0x1F170)
    if 0x1F150 <= cp <= 0x1F169:
        return chr(ord("A") + cp - 0x1F150)
    if 0x1F1E6 <= cp <= 0x1F1FF:
        return chr(ord("A") + cp - 0x1F1E6)
    if cp == 0x1F51F:
        return "10"
    if ch in FULLWIDTH_OP:
        return FULLWIDTH_OP[ch]
    number = unicodedata.numeric(ch, None)
    if number is not None and number == int(number) and 0 <= number <= 100:
        return str(int(number))
    nfkc = unicodedata.normalize("NFKC", ch)
    if nfkc != ch:
        result = "".join(c for c in nfkc if c.isascii() and c.isalnum())
        if result:
            return result.upper()
    name = unicodedata.name(ch, "")
    for prefix in ("LATIN CAPITAL LETTER ", "LATIN SMALL LETTER ", "DIGIT "):
        if name.startswith(prefix):
            suffix = name[len(prefix):]
            if len(suffix) == 1 and suffix.isalnum():
                return suffix.upper()
    return ""


def normalize_operand(text: str) -> str:
    return "".join(normalize_char(ch) for ch in text)


def coerce(text: str, to_digit: bool) -> str:
    table = ALPHA_TO_DIGIT if to_digit else DIGIT_TO_ALPHA
    out = []
    for ch in text:
        if to_digit and ch.isalpha():
            out.append(table.get(ch, ch))
        elif not to_digit and ch.isdigit():
            out.append(table.get(ch, ch))
        else:
            out.append(ch)
    return "".join(out)


def buttons_keep_case(labels: list[str]) -> bool:
    for label in labels:
        for ch in label or "":
            if ch.isalpha() and ch.islower():
                return True
    return False


def buttons_are_digit(labels: list[str]) -> bool | None:
    raw = [label.strip() for label in labels if label and label.strip()]
    if not raw:
        return None
    norm = [normalize_operand(label) for label in raw]
    if all(item.isdigit() for item in norm):
        return True
    if all(item.isascii() and item.isalpha() and item == item.upper() for item in raw):
        folded = [item.casefold() for item in raw]
        if len(set(folded)) == len(folded):
            return False
    return None


def _expr_body(expr: str) -> str:
    return re.split(r"[=＝]", expr, maxsplit=1)[0]


_SPLIT = re.compile(rf"\s*([{OP_CHARS}])\s*")


def chain_parts(expr: str) -> tuple[list[str], list[str]] | None:
    body = _expr_body(expr).strip()
    if not body:
        return None
    bits = _SPLIT.split(body)
    if len(bits) < 3 or len(bits) % 2 == 0:
        return None
    operands = [item.strip() for item in bits[0::2]]
    operators = [item.strip() for item in bits[1::2]]
    if not all(operands) or not all(operators):
        return None
    return operands, operators


def piece_text(text: str, want_digit: bool | None, keep_case: bool = False) -> str:
    chars = []
    for ch in text:
        if "\u4e00" <= ch <= "\u9fff" or (ch.isascii() and ch.isalnum()):
            chars.append(ch)
            continue
        mapped = normalize_char(ch)
        if mapped:
            chars.append(mapped)
    raw = "".join(chars)
    if want_digit is None:
        if keep_case:
            return raw
        return "".join(ch.upper() if ch.isascii() and ch.isalpha() else ch for ch in raw)
    return coerce(raw.upper(), want_digit)


def fold_numeric(operands: list[str], operators: list[str]) -> str | None:
    if not operands or not all(item.isdigit() for item in operands):
        return None
    if len(operators) != len(operands) - 1:
        return None
    value = int(operands[0])
    for op, item in zip(operators, operands[1:]):
        fn = OPERATORS.get(FULLWIDTH_OP.get(op, op))
        if fn is None:
            return None
        try:
            value = int(fn(value, int(item)))
        except (ZeroDivisionError, ValueError):
            return None
    return str(value)


def _pick(candidates: list[str], labels: list[str]) -> str | None:
    raw = [label.strip() for label in labels if label and label.strip()]
    for answer in candidates:
        if answer and answer in raw:
            return answer
    for answer in candidates:
        if not answer:
            continue
        folded = [label for label in raw if label.casefold() == answer.casefold()]
        if len(folded) == 1:
            return folded[0]
    return None


def solve_expr(expr: str) -> str | None:
    parsed = chain_parts(expr)
    if not parsed:
        return None
    operands, operators = parsed
    pieces = [piece_text(item, None) for item in operands]
    if not all(pieces):
        return None
    if all(op in "+＋" for op in operators) and any(not item.isdigit() for item in pieces):
        return "".join(pieces)
    return fold_numeric(pieces, operators)


def solve_expr_with_buttons(expr: str, labels: list[str]) -> tuple[str | None, dict[str, str]]:
    parsed = chain_parts(expr)
    if not parsed:
        return None, {}
    operands, operators = parsed
    want_digit = buttons_are_digit(labels)
    keep_case = buttons_keep_case(labels)
    pieces = [piece_text(item, want_digit, keep_case) for item in operands]
    if not all(pieces):
        return None, {}
    plus = all(op in "+＋" for op in operators)
    joined = "".join(pieces) if plus else ""
    total = fold_numeric(pieces, operators)
    candidates = []
    if total is not None:
        candidates.append(total)
    if joined and joined != total:
        candidates.append(joined)
    chosen = _pick(candidates, labels)
    if chosen:
        return chosen, {}
    base = joined if joined and any(not item.isdigit() for item in pieces) else (total or joined)
    if not base:
        return None, {}
    for index, ch in enumerate(base):
        for alt in CONFUSIONS.get(ch, []):
            guess = base[:index] + alt + base[index + 1:]
            found = _pick([guess], labels)
            if found:
                return found, {ch: alt}
    return base, {}
