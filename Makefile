.PHONY: test coverage

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'

coverage:
	PYTHONPATH=src python3 -m blackdog.cli coverage --project-root .
