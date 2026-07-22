.PHONY: deps protos run lint test clean-protos

# Sync dependencies (incl. dev group) and initialise submodules.
deps:
	git submodule update --init --recursive
	uv sync --all-extras

# Compile falcon-protos + protos-proposed -> src/generated (betterproto2, matches the SDK).
protos:
	mkdir -p src/generated && uv run protoc -I falcon-protos -I protos-proposed --python_betterproto2_out=src/generated $$(find falcon-protos protos-proposed -name '*.proto')

clean-protos:
	rm -rf src/generated

# Run the predictor node.
run:
	uv run python -m src.main

# Lint.
lint:
	uv run ruff check src sim tests scripts

# Unit tests (pure-model tests need no core connection or protos).
test:
	uv run pytest
