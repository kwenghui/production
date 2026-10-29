"""Static, non-executing audit of Sage's ask_sage request-routing branches."""

from __future__ import annotations
import ast
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'utils' / 'core_secure.py'


def audit(source: Path = SOURCE) -> dict:
    tree = ast.parse(source.read_text(encoding='utf-8-sig'))
    funcs = [
        n
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'ask_sage'
    ]
    if len(funcs) != 1:
        raise ValueError(f'Expected one ask_sage, found {len(funcs)}')
    fn = funcs[0]
    routes = []
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if any(
                isinstance(child, ast.Return)
                for statement in node.body
                for child in ast.walk(statement)
            ):
                routes.append(
                    {
                        'line': node.lineno,
                        'condition': test[:350],
                        'return_count': sum(
                            isinstance(x, ast.Return) for stmt in node.body for x in ast.walk(stmt)
                        ),
                    }
                )
    routes.sort(key=lambda x: x['line'])
    return {
        'file': str(source.relative_to(ROOT)) if source.is_relative_to(ROOT) else str(source),
        'function': 'ask_sage',
        'start_line': fn.lineno,
        'end_line': fn.end_lineno,
        'length_lines': fn.end_lineno - fn.lineno + 1,
        'conditional_return_branches': len(routes),
        'routes': routes,
    }


def main() -> None:
    result = audit()
    out = ROOT / 'reports'
    out.mkdir(exist_ok=True)
    (out / 'phase18_request_routes.json').write_text(
        json.dumps(result, indent=2) + '\n', encoding='utf-8'
    )
    lines = [
        '# ask_sage routing audit',
        '',
        'This is a static inventory, not a runtime call graph. Nested conditions and returns '
        'may be counted more than once. No application behaviour was changed.',
        '',
        f"Function: `{result['function']}` ({result['length_lines']} lines)",
        f"Conditional branches containing a return: {result['conditional_return_branches']}",
        '',
        '| Line | Condition | Returns in branch |',
        '|---:|---|---:|',
    ]
    for r in result['routes']:
        lines.append(
            f"| {r['line']} | `{r['condition'].replace('|', chr(92)+'|')}` | "
            f"{r['return_count']} |"
        )
    (out / 'phase18_request_routes.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(
        f"Audited {result['function']}: {result['conditional_return_branches']} "
        f"conditional-return branches"
    )


if __name__ == '__main__':
    main()
