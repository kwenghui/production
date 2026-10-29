"""Trace selected Sage discovery functions without modifying Sage source files."""

import argparse
import contextlib
import functools
import json
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if not (ROOT / 'utils').is_dir():
    ROOT = ROOT.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def compact(obj, depth=0):
    if depth > 3:
        return f'<{type(obj).__name__}>'
    if isinstance(obj, (str, int, float, bool, type(None))):
        return obj[:350] + '…' if isinstance(obj, str) and len(obj) > 350 else obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {
            str(k): compact(v, depth + 1)
            for k, v in list(obj.items())[:12]
            if not any(
                s in str(k).lower() for s in ('key', 'token', 'secret', 'password', 'credential')
            )
        }
    if isinstance(obj, (list, tuple, set)):
        seq = list(obj)
        return {'count': len(seq), 'sample': [compact(v, depth + 1) for v in seq[:5]]}
    if hasattr(obj, 'shape'):
        return {'type': type(obj).__name__, 'shape': str(obj.shape)}
    return f'<{type(obj).__module__}.{type(obj).__name__}>'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('question', nargs='*')
    parser.add_argument('--limit', type=int, default=10)
    parser.add_argument('--output', default='')
    args = parser.parse_args()
    question = ' '.join(args.question).strip() or input('Question: ').strip()
    if not question:
        return 2
    from utils import hybrid_discovery as hd
    from utils import kb
    from utils import query_text

    output = (
        Path(args.output).resolve()
        if args.output
        else ROOT / 'reports' / 'search_function_trace.jsonl'
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = threading.Lock()
    local = threading.local()
    original = []
    counter = [0]
    start = time.perf_counter()

    def log(event):
        with lock:
            counter[0] += 1
            event['sequence'] = counter[0]
            event['elapsed_from_start_ms'] = round((time.perf_counter() - start) * 1000, 2)
            with output.open('a', encoding='utf-8') as f:
                f.write(json.dumps(event, ensure_ascii=False, default=str) + '\n')
            if event['event'] in ('call', 'return', 'error'):
                print(json.dumps(event, ensure_ascii=False, default=str), flush=True)

    targets = [
        (hd, 'retrieve'),
        (hd, '_keyword_hits'),
        (hd, '_semantic_hits'),
        (hd, '_query_semantic_index'),
        (hd, '_vector_ids'),
        (hd, '_ranked_tables'),
        (hd, '_chroma_index'),
        (kb, 'lexical_search_series_first'),
        (kb, 'hydrate'),
        (kb, 'embed_query'),
        (query_text, 'normalise_query'),
    ]
    for module, name in targets:
        fn = getattr(module, name, None)
        if not callable(fn):
            continue

        def wrap(fn, module_name, name):
            @functools.wraps(fn)
            def traced(*a, **kw):
                call_id = f'{threading.get_ident()}-{time.perf_counter_ns()}'
                # Never print raw embedding vectors, credentials, or entire database objects.
                shown_args = [
                    compact(x)
                    if not (name == '_vector_ids' and i == 4)
                    else '<embedding vector omitted>'
                    for i, x in enumerate(a)
                ]
                shown_kw = {
                    k: ('<embedding vector omitted>' if 'embedding' in k else compact(v))
                    for k, v in kw.items()
                }
                log(
                    {
                        'event': 'call',
                        'id': call_id,
                        'function': f'{module_name}.{name}',
                        'args': shown_args,
                        'kwargs': shown_kw,
                    }
                )
                t = time.perf_counter()
                try:
                    result = fn(*a, **kw)
                    log(
                        {
                            'event': 'return',
                            'id': call_id,
                            'function': f'{module_name}.{name}',
                            'duration_ms': round((time.perf_counter() - t) * 1000, 2),
                            'result': '<embedding vector omitted>'
                            if name == 'embed_query'
                            else compact(result),
                        }
                    )
                    return result
                except Exception as e:
                    log(
                        {
                            'event': 'error',
                            'id': call_id,
                            'function': f'{module_name}.{name}',
                            'duration_ms': round((time.perf_counter() - t) * 1000, 2),
                            'exception_type': type(e).__name__,
                        }
                    )
                    raise

            return traced

        original.append((module, name, fn))
        setattr(module, name, wrap(fn, module.__name__, name))
    print('Sage discovery function trace (arguments and sampled returns)')
    print('Trace file:', output)
    print('Question:', question)
    print(
        'Note: output is appended; no databases are modified by this tester. Embedding API calls may incur charges.\n'
    )
    try:
        results = hd.retrieve(question, top_k=max(1, min(args.limit, 15)))
        print('\nFINAL CANDIDATES:')
        for i, item in enumerate(results, 1):
            print(
                f'{i}. {item.get("table_id")} | {item.get("title")} | {item.get("discovery_sources")}'
            )
        print(f'\nSaved {counter[0]} trace events to {output}')
    finally:
        for module, name, fn in reversed(original):
            setattr(module, name, fn)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
