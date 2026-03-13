.PHONY: test

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'

