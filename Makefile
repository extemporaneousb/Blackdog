.PHONY: acceptance public-check test test-core

CORE_AUDIT_COMMAND = PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_core_*.py'

acceptance:
	$(MAKE) test

public-check:
	python3 scripts/public_check.py

test: public-check
	PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'

test-core:
	$(CORE_AUDIT_COMMAND)
