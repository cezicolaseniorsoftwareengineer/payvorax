.PHONY: help install test lint format run docker-up docker-down clean

help:  ## Show this help menu
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies via Poetry
	poetry install

test: ## Run unit tests with coverage
	poetry run pytest

lint: ## Run static analysis (mypy, black, isort)
	poetry run black --check .
	poetry run isort --check-only .
	poetry run mypy .

format: ## Auto-format code
	poetry run black .
	poetry run isort .

run: ## Start local development server
	python start.py

docker-up: ## Start containers in detached mode
	docker-compose up -d --build

docker-down: ## Stop and remove containers
	docker-compose down

clean: ## Clean up cache and temporary files
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	rm -rf .pytest_cache
	rm -rf .coverage
	rm -rf htmlcov
