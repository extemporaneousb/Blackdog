.PHONY: acceptance test test-core

CORE_AUDIT_COMMAND = PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_core_*.py'

acceptance:
	$(MAKE) test

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'

test-core:
	$(CORE_AUDIT_COMMAND)
