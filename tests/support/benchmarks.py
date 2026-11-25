"""Repeated benchmark summaries with no wall-clock pass/fail thresholds."""

import gc
import statistics
import time
import tracemalloc


def _summary(samples, scale=1.0):
    values = [sample * scale for sample in samples]
    return {
        "samples": values,
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def measure(operation, repeats=5, trace_memory=False):
    """Measure an actual callable, retaining results and optional allocation data.

    ``tracemalloc`` reports Python allocations, not process RSS or native/VFS
    buffers. Setup and correctness belong in ``operation``; callers should keep
    any comparison's setup identical on both sides.
    """
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats <= 0:
        raise ValueError("repeats must be a positive integer")
    elapsed_ns = []
    peaks = []
    results = []
    for _ in range(repeats):
        gc.collect()
        if trace_memory:
            tracemalloc.start()
        started = time.perf_counter_ns()
        result = operation()
        elapsed_ns.append(time.perf_counter_ns() - started)
        if trace_memory:
            _, peak = tracemalloc.get_traced_memory()
            peaks.append(peak)
            tracemalloc.stop()
        results.append(result)
    summary = {
        "elapsed_ms": _summary(elapsed_ns, scale=1 / 1_000_000),
        "results": results,
    }
    if trace_memory:
        summary["tracemalloc_peak_bytes"] = _summary(peaks)
    return summary
