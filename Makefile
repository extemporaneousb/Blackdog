.PHONY: test test-core test-emacs coverage coverage-core

CORE_AUDIT_COMMAND = PYTHONPATH=src python3 -m unittest tests/test_blackdog_cli.py -k core_audit
CORE_COVERAGE_OUTPUT = coverage/core-latest.json

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'

test-core:
	$(CORE_AUDIT_COMMAND)

test-emacs:
	emacs -Q --batch -L editors/emacs/lisp -L editors/emacs/test -l editors/emacs/test/blackdog-test.el -f ert-run-tests-batch-and-exit

coverage:
	PYTHONPATH=src python3 -m blackdog.cli coverage --project-root .

coverage-core:
	PYTHONPATH=src python3 -m blackdog.cli coverage --project-root . --command "$(CORE_AUDIT_COMMAND)" --output $(CORE_COVERAGE_OUTPUT)
