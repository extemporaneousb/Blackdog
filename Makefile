.PHONY: acceptance acceptance-artifact public-check test test-core release

CORE_AUDIT_COMMAND = PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_core_*.py'

acceptance: test
	$(MAKE) acceptance-artifact

acceptance-artifact: release
	python3 scripts/acceptance_runtime.py --artifact dist/blackdog.pyz --samples 1 --output dist/runtime-acceptance.json

public-check:
	python3 scripts/public_check.py

test: public-check
	PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'

test-core:
	$(CORE_AUDIT_COMMAND)

release: public-check
	python3 scripts/build_release.py
