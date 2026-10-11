"""Performance workloads run outside the test suite (docs/04 §11, P-04; #76).

``perf.lite_smoke`` is NFR-PERF-4's reference workload, Lite variant: ``uv run python -m
perf.lite_smoke --minutes 30 --host-class mac|reference|ci``. Its committed reports live in
``perf/results/``. It needs the ``perf`` dependency group (``uv sync --group perf``) and Docker.
"""
