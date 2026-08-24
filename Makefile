# Shortcuts for the things I run often. Nothing here does anything you cannot
# do by hand; it is here so the commands in the report are the commands that run.

.PHONY: install train test lint format run evaluate demo clean

install:
	pip install -r requirements.txt

# Rewrites models/intent_classifier.json. The artefact is committed, so this is
# only needed after changing the training data or the features.
train:
	python -m scripts.train_classifier

test:
	python -m pytest tests/ -v --cov=src --cov-report=term-missing

lint:
	python -m flake8 src/ scripts/ evaluation/ tests/

format:
	python -m black src/ scripts/ evaluation/ tests/

run:
	uvicorn src.api:app --host 127.0.0.1 --port 8000 --reload

# The full validation run, which is what the acceptance criteria are measured on.
evaluate:
	python -m evaluation.harness --input data/validation_tickets.json --output evaluation/results/latest

# Small enough to watch scroll past, for the walkthrough.
demo:
	python -m evaluation.harness --input data/development_tickets.json --output evaluation/results/demo --limit 10

clean:
	rm -rf .pytest_cache .coverage htmlcov && find . -name __pycache__ -type d -prune -exec rm -rf {} +
