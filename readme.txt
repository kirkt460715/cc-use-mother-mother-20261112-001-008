County care continuity analysis. Python 3.11+, standard library only.
Demo: python3 continuity.py demo --fixture fixtures/care.json
Analyze: python3 continuity.py analyze --input fixtures/care.json --as-of 2026-11-12 --known-at 2026-11-12
Tests: python3 -m unittest discover -s tests -v
Build: python3 -m compileall -q encounters pathways waiting_analysis exports continuity.py
CLI: python3 continuity.py --help
Inputs are synthetic. Analytics describe observed records and make no clinical recommendations.
