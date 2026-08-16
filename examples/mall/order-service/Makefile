.PHONY: build test
build:
	python -c "import compileall,sys; sys.exit(0 if compileall.compile_dir('src', quiet=1) else 1)"
test:
	pytest -q
