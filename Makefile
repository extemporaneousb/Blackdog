.PHONY: test test-emacs coverage

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'

test-emacs:
	emacs -Q --batch -L editors/emacs/lisp -L editors/emacs/test -l editors/emacs/test/blackdog-test.el -f ert-run-tests-batch-and-exit

coverage:
	PYTHONPATH=src python3 -m blackdog.cli coverage --project-root .
