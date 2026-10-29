"""Add the missing spaces of the older, dense code style, provably safely.

    python tools/tidy_spacing.py FILE...          # rewrite the files
    python tools/tidy_spacing.py --check FILE...  # exit 1 if a file would change

Only whitespace and line breaks change (PEP 8):
  * a space after a comma:            f(a,b)      -> f(a, b)
  * spaces around statement-level "=": x=1        -> x = 1   (keyword arguments untouched)
  * spaces around comparisons / augmented assignment:  a==b, n+=1 -> a == b, n += 1
  * code lines longer than 100 characters are broken after a comma inside
    brackets (where Python allows a line break), aligned under the bracket;
  * comment-only lines longer than 100 characters are wrapped at spaces.

  * long one-line strings inside brackets are split into adjacent literals
    ("abc " "def" is the same string to Python);
  * long strings outside brackets are first put in parentheses ("x" == ("x"));
  * an end-of-line comment that makes a line too long moves onto its own line;
  * docstring lines longer than 100 characters are re-wrapped at spaces.

Safety: each rewritten file must parse to exactly the same syntax tree as the
original (docstrings compared word by word, since only their line breaks may
change); otherwise the file is left unchanged and reported. This is a stop-gap until "ruff format"
can run (see how to run.txt).
"""

import ast
import io
import re
import sys
import tokenize
from pathlib import Path

_SPACED_OPERATORS = {"==", "!=", "<=", ">=", "+=", "-=", "*=", "/=", "//=", "%=", "|=", "&="}


def _insertions(source):
    """(row, column) positions where one space must be inserted."""
    tokens = [t for t in tokenize.generate_tokens(io.StringIO(source).readline)]
    spots, depth = set(), 0
    for index, token in enumerate(tokens):
        if token.type == tokenize.OP and token.string in "([{":
            depth += 1
        elif token.type == tokenize.OP and token.string in ")]}":
            depth -= 1
        if token.type != tokenize.OP:
            continue
        before = tokens[index - 1] if index else None
        after = tokens[index + 1] if index + 1 < len(tokens) else None
        if after is None or after.type in (tokenize.NEWLINE, tokenize.NL, tokenize.COMMENT):
            continue
        if token.string == ",":
            if after.start == token.end and after.string not in ")]}":
                spots.add(token.end)
        elif token.string in _SPACED_OPERATORS or (token.string == "=" and depth == 0):
            if before is not None and before.end == token.start:
                spots.add(token.start)
            if after.start == token.end:
                spots.add(token.end)
    return spots


def tidy(source):
    """The source with spaces added and long lines wrapped (same syntax tree)."""
    lines = source.splitlines(keepends=True)
    for row, column in sorted(_insertions(source), reverse=True):
        line = lines[row - 1]
        lines[row - 1] = line[:column] + " " + line[column:]
    text = "".join(lines)
    # Each step is kept only if the result still parses to the same code; a step
    # that cannot handle some unusual construct is simply skipped for this file.
    for step in (
        _wrap_comment_lines,
        _move_trailing_comments,
        _parenthesise_long_strings,
        _wrap_code_lines,
        _split_long_strings,
        _wrap_docstrings,
        _wrap_one_line_docstrings,
        _rejoin_fragments,
    ):
        try:
            candidate = step(text)
            if _same_code(text, candidate):
                text = candidate
        except (SyntaxError, tokenize.TokenError, IndentationError, ValueError):
            continue
    return text


MAX_LINE = 100


def _wrap_comment_lines(source):
    """Wrap comment-only lines longer than MAX_LINE at spaces (comments never change code)."""
    import textwrap

    out = []
    for line in source.splitlines(keepends=True):
        body = line.rstrip("\n")
        stripped = body.lstrip()
        if len(body) > MAX_LINE and stripped.startswith("#") and not stripped.startswith("#!"):
            indent = body[: len(body) - len(stripped)]
            text = stripped.lstrip("#").strip()
            wrapped = textwrap.wrap(text, width=MAX_LINE - len(indent) - 2) or [""]
            out.extend(f"{indent}# {part}\n" for part in wrapped)
        else:
            out.append(line)
    return "".join(out)


def _break_point(source, row):
    """(column to break after, indent column) for a long code line, or None.

    The break goes after the last comma before the limit that sits inside a
    bracket (Python continues the line implicitly there); the next line is
    aligned one character after that bracket.
    """
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    stack, best, in_fstring = [], None, 0
    fstring_start = getattr(tokenize, "FSTRING_START", None)  # Python 3.12+
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    for token in tokens:
        if token.start[0] > row:
            break
        # Never break inside an f-string's {expression}: older Pythons forbid it
        # and it is hard to read.
        if token.type == fstring_start:
            in_fstring += 1
            continue
        if token.type == fstring_end:
            in_fstring -= 1
            continue
        if in_fstring:
            continue
        if token.type == tokenize.OP and token.string in "([{":
            stack.append(token.start)
        elif token.type == tokenize.OP and token.string in ")]}":
            if stack:
                stack.pop()
        elif (
            stack
            and token.start[0] == row
            and token.start[1] <= MAX_LINE
            and token.type == tokenize.NAME
            and token.string in ("and", "or")
        ):
            # Break BEFORE a boolean operator: the operator then starts the next line.
            opener = stack[-1]
            indent = opener[1] + 1 if opener[0] == row else None
            if indent is None:
                line = source.splitlines()[row - 1]
                indent = len(line) - len(line.lstrip())
            if indent < token.start[1] - 1:
                best = (token.start[1], indent)
        elif (
            token.type == tokenize.OP
            and token.string == ","
            and stack
            and token.start[0] == row
            and token.end[1] <= MAX_LINE
        ):
            opener = stack[-1]
            indent = opener[1] + 1 if opener[0] == row else None
            if indent is None:
                # Bracket opened on an earlier line: keep this line's own indentation.
                line = source.splitlines()[row - 1]
                indent = len(line) - len(line.lstrip())
            if indent < token.end[1] - 1:
                best = (token.end[1], indent)
    return best


def _wrap_code_lines(source):
    """Break long code lines after commas inside brackets, repeatedly."""
    for _ in range(2000):
        lines = source.splitlines(keepends=True)
        long_rows = [
            i + 1
            for i, line in enumerate(lines)
            if len(line.rstrip("\n")) > MAX_LINE and not line.lstrip().startswith("#")
        ]
        changed = False
        for row in long_rows:
            try:
                point = _break_point(source, row)
            except (tokenize.TokenError, IndentationError):
                return source
            if point is None:
                continue
            column, indent = point
            line = lines[row - 1]
            head, tail = line[:column].rstrip(), line[column:].lstrip()
            if not tail.strip():
                continue
            lines[row - 1] = head + "\n" + " " * indent + tail
            source = "".join(lines)
            changed = True
            break
        if not changed:
            return source
    return source


def _string_spans(source, row):
    """(start col, end col, prefix, quote, is_fstring) of one-line literals inside brackets."""
    spans, depth, fstart = [], 0, None
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.start[0] > row:
            break
        if token.type == tokenize.OP and token.string in "([{" and fstart is None:
            depth += 1
        elif token.type == tokenize.OP and token.string in ")]}" and fstart is None:
            depth -= 1
        elif token.type == fstring_start:
            fstart = (token, depth)
        elif token.type == fstring_end and fstart is not None:
            start_token, at_depth = fstart
            fstart = None
            if start_token.start[0] == row == token.end[0] and at_depth > 0:
                head = start_token.string
                spans.append((start_token.start[1], token.end[1], head[:-1], head[-1], True))
        elif (
            token.type == tokenize.STRING
            and fstart is None
            and token.start[0] == row == token.end[0]
            and depth > 0
        ):
            text = token.string
            prefix = text[: len(text) - len(text.lstrip("rRbBuUfF"))]
            quote = text[len(prefix)]
            if text[len(prefix) : len(prefix) + 3] in ('"""', "'''"):
                continue
            spans.append((token.start[1], token.end[1], prefix, quote, False))
    return spans


def _split_point(line, start, end, prefix, quote, is_fstring):
    """Column just after a space where the literal may be split, or None."""
    body_start = start + len(prefix) + 1
    braces, best = 0, None
    column = body_start
    while column < end - 1:
        char = line[column]
        if is_fstring and char in "{}":
            if line[column : column + 2] in ("{{", "}}"):
                column += 2
                continue
            braces += 1 if char == "{" else -1
        elif (
            char == " "
            and braces == 0
            and line[column - 1] != "\\"
            and column + 1 <= MAX_LINE - 1
            and column + 1 > body_start + 10
            and column + 1 < end - 2
        ):
            best = column + 1
        column += 1
    return best


def _split_long_strings(source):
    """Split long one-line string literals inside brackets into adjacent literals."""
    for _ in range(3000):
        lines = source.splitlines(keepends=True)
        changed = False
        for row, line in enumerate(lines, 1):
            if len(line.rstrip("\n")) <= MAX_LINE or line.lstrip().startswith("#"):
                continue
            try:
                spans = _string_spans(source, row)
            except (tokenize.TokenError, IndentationError, SyntaxError):
                return source
            for start, end, prefix, quote, is_fstring in spans:
                if end <= MAX_LINE:
                    continue
                cut = _split_point(line, start, end, prefix, quote, is_fstring)
                if cut is None:
                    continue
                lines[row - 1] = (
                    line[:cut] + quote + "\n" + " " * start + prefix + quote + line[cut:]
                )
                source = "".join(lines)
                changed = True
                break
            if changed:
                break
        if not changed:
            return source
    return source


def _docstring_rows(source):
    """Line numbers of every line that belongs to a docstring."""
    rows = set()
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        ):
            rows.update(range(node.body[0].lineno, node.body[0].end_lineno + 1))
    return rows


def _is_prose(line):
    """A plain text line of a docstring paragraph (not a list item, table, code or quotes)."""
    text = line.strip()
    return (
        bool(text)
        and not text.startswith(("-", "*", "|", ">>>", "#", '"""', "'''"))
        and text[:2] not in {"1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9."}
        and '"""' not in text
        and "'''" not in text
    )


def _wrap_docstrings(source):
    """Re-flow docstring lines longer than MAX_LINE: overflowing words move to the start
    of the next line of the same paragraph (never left as a one-word line); nothing in
    Sage reads __doc__ at run time."""
    import textwrap

    rows = _docstring_rows(source)
    lines = source.splitlines(keepends=True)
    for index in range(len(lines)):
        row = index + 1
        body = lines[index].rstrip("\n")
        if row not in rows or len(body) <= MAX_LINE or not _is_prose(body):
            continue
        indent = body[: len(body) - len(body.lstrip())]
        parts = textwrap.wrap(
            body.strip(),
            width=MAX_LINE - len(indent),
            break_long_words=False,
            break_on_hyphens=False,
        )
        if len(parts) < 2:
            continue
        following = lines[index + 1].rstrip("\n") if index + 1 < len(lines) else ""
        same_paragraph = (
            row + 1 in rows
            and _is_prose(following)
            and following[: len(following) - len(following.lstrip())] == indent
        )
        if same_paragraph:
            lines[index] = indent + parts[0] + "\n"
            lines[index + 1] = indent + " ".join(parts[1:] + [following.strip()]) + "\n"
        else:
            lines[index] = "".join(indent + part + "\n" for part in parts)
    return "".join(lines)


def _rejoin_fragments(source):
    """Join a very short docstring line (left by an earlier, naive re-wrap) with the
    lines around it and re-flow them, so paragraphs read naturally."""
    import textwrap

    rows = _docstring_rows(source)
    lines = source.splitlines(keepends=True)
    index = 1
    while index < len(lines) - 1:
        before, line, after = (
            lines[index - 1].rstrip("\n"),
            lines[index].rstrip("\n"),
            lines[index + 1].rstrip("\n"),
        )
        indent = before[: len(before) - len(before.lstrip())]
        if (
            index + 1 in rows
            and index in rows
            and index + 2 in rows
            and len(before) >= 88
            and 0 < len(line.strip()) <= 18
            and all(_is_prose(x) for x in (before, line, after))
            and all(x[: len(x) - len(x.lstrip())] == indent for x in (line, after))
        ):
            words = " ".join((before.strip(), line.strip(), after.strip()))
            parts = textwrap.wrap(
                words, width=MAX_LINE - len(indent), break_long_words=False, break_on_hyphens=False
            )
            lines[index - 1 : index + 2] = [indent + part + "\n" for part in parts]
            continue
        index += 1
    return "".join(lines)


def _wrap_one_line_docstrings(source):
    """Turn a too-long one-line docstring into a short multi-line one."""
    import textwrap

    tree = ast.parse(source)
    rows = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
            and node.body[0].lineno == node.body[0].end_lineno
        ):
            rows[node.body[0].lineno] = node.body[0].col_offset
    lines = source.splitlines(keepends=True)
    for row in sorted(rows, reverse=True):
        line = lines[row - 1]
        body = line.rstrip("\n")
        stripped = body.strip()
        if len(body) <= MAX_LINE or not (stripped.startswith('"""') and stripped.endswith('"""')):
            continue
        indent = " " * rows[row]
        text = stripped[3:-3]
        if "\\" in text:
            continue
        parts = textwrap.wrap(
            text, width=MAX_LINE - len(indent) - 3, break_long_words=False, break_on_hyphens=False
        )
        if len(parts) < 2:
            continue
        lines[row - 1] = (
            indent
            + '"""'
            + parts[0]
            + "\n"
            + "".join(indent + part + "\n" for part in parts[1:-1])
            + indent
            + parts[-1]
            + '"""\n'
        )
    return "".join(lines)


def _move_trailing_comments(source):
    """Move an end-of-line comment that makes a code line too long onto its own line above."""
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    lines = source.splitlines(keepends=True)
    moves = []
    for index, token in enumerate(tokens):
        if token.type != tokenize.COMMENT:
            continue
        # Tool directives only work on the line they annotate: never move them.
        if re.match(r"#\s*(noqa|nosec|type:|pragma|fmt:)", token.string):
            continue
        row = token.start[0]
        line = lines[row - 1]
        code = line[: token.start[1]].rstrip()
        if (
            len(line.rstrip("\n")) > MAX_LINE
            and code.strip()
            and len(code) <= MAX_LINE
            and not code.rstrip().endswith(("(", "[", "{", ",", "\\"))
            and index
            and tokens[index - 1].start[0] == row
            and tokens[index - 1].type not in (tokenize.NL, tokenize.NEWLINE)
        ):
            first = next(
                t
                for t in tokens
                if t.start[0] == row and t.type not in (tokenize.INDENT, tokenize.DEDENT)
            )
            # Only for a line that starts its own statement (not a continuation line).
            if first.start[1] == len(code) - len(code.lstrip()) and _starts_statement(
                tokens, first
            ):
                moves.append((row, token.string, code))
    for row, comment, code in sorted(moves, reverse=True):
        indent = code[: len(code) - len(code.lstrip())]
        lines[row - 1] = indent + comment + "\n" + code + "\n"
    return "".join(lines)


def _starts_statement(tokens, first):
    """True when ``first`` is the first token of a logical line."""
    position = tokens.index(first)
    previous = [
        t
        for t in tokens[:position]
        if t.type not in (tokenize.INDENT, tokenize.DEDENT, tokenize.COMMENT, tokenize.NL)
    ]
    return not previous or previous[-1].type == tokenize.NEWLINE


def _parenthesise_long_strings(source):
    """Wrap a long string that is NOT inside brackets in parentheses, so it can be split.

    ("text") is the same expression as "text"; only lone literals (not part of
    an implicit concatenation) are wrapped.
    """
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    lines = source.splitlines(keepends=True)
    edits, depth, fdepth = [], 0, 0
    fstring_start = getattr(tokenize, "FSTRING_START", None)
    fstring_end = getattr(tokenize, "FSTRING_END", None)
    start_token = None
    for index, token in enumerate(tokens):
        if token.type == fstring_start:
            if fdepth == 0:
                start_token = (index, token, depth)
            fdepth += 1
            continue
        if token.type == fstring_end:
            fdepth -= 1
            if fdepth == 0 and start_token and start_token[2] == 0:  # only outside brackets
                edits.append((start_token[0], start_token[1], index, token))
            continue
        if fdepth:
            continue
        if token.type == tokenize.OP and token.string in "([{":
            depth += 1
        elif token.type == tokenize.OP and token.string in ")]}":
            depth -= 1
        elif token.type == tokenize.STRING and depth == 0:
            edits.append((index, token, index, token))
    chosen = []
    for first_index, first, last_index, last in edits:
        row = first.start[0]
        if (
            first.start[0] != last.end[0]
            or len(lines[row - 1].rstrip("\n")) <= MAX_LINE
            or last.end[1] <= MAX_LINE
            or lines[row - 1].lstrip().startswith(('"""', "'''"))
            or tokens[first_index - 1].type in (tokenize.STRING, fstring_end)
            or tokens[last_index + 1].type in (tokenize.STRING, fstring_start)
            or tokens[first_index - 1].string in (".",)
        ):
            continue
        text = first.string if first.type == tokenize.STRING else ""
        if text[len(text) - len(text.lstrip("rRbBuUfF")) :][:3] in ('"""', "'''"):
            continue
        chosen.append((row, first.start[1], last.end[1]))
    for row, start, end in sorted(chosen, reverse=True):
        line = lines[row - 1]
        lines[row - 1] = line[:start] + "(" + line[start:end] + ")" + line[end:]
    return "".join(lines)


def _same_code(before, after):
    """True when two sources have the same syntax tree, docstrings compared by words."""

    def normalised(text):
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                node.body[0].value.value = " ".join(node.body[0].value.value.split())
        return ast.dump(tree)

    return normalised(before) == normalised(after)


def main(argv):
    """Rewrite (or with --check, only report) the given files."""
    check = "--check" in argv
    changed, refused = [], []
    for name in (a for a in argv if a != "--check"):
        path = Path(name)
        original = path.read_text(encoding="utf-8")
        rewritten = tidy(original)
        if rewritten == original:
            continue
        if not _same_code(original, rewritten):
            refused.append(name)
            continue
        changed.append(name)
        if not check:
            path.write_text(rewritten, encoding="utf-8")
    print(("Would change: " if check else "Changed: ") + (", ".join(changed) or "none"))
    if refused:
        print("Left unchanged (syntax tree would differ): " + ", ".join(refused))
    return 1 if (check and changed) or refused else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
