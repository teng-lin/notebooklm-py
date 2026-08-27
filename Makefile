.PHONY: gates

# Run every AST/path/contract guard once with the same shape as CI's bounded
# manual and nightly lanes.
gates:
	uv run --frozen --extra browser --extra dev --extra markdown --extra mcp \
		--extra server --extra impersonate --extra cookies pytest -n auto \
		--dist loadgroup -m repo_lint --timeout=180 --no-cov
