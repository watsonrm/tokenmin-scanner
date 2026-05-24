@AGENTS.md

## Claude Code

Always run `bash tests/run.sh` before claiming a change to `skills/tokenmin/anonymize.py`, `skills/tokenmin/tokenmin.py`, or anything that touches the anonymization or snapshot-write path actually works. The synthetic-leak gate is the canonical correctness check — if you change the scrubber, also extend the fixture in `tests/test_scrubber.py` to cover the new vector.
