VERSION := $(shell pipenv run python setup.py --version)
HOSTNAME := $(shell hostname)
BDIST_WHEEL := dist/rsconnect_python-$(VERSION)-py2.py3-none-any.whl
S3_PREFIX := s3://rstudio-connect-downloads/connect/rsconnect-python

RUNNER = docker run \
  -it --rm \
  -v $(PWD):/rsconnect \
  -w /rsconnect \
  rsconnect-python:$* \
  bash -c

TEST_COMMAND ?= ./scripts/runtests
SHELL_COMMAND ?= pipenv shell

ifneq ($(GITHUB_RUN_ID),)
	RUNNER = bash -c
endif

TEST_ENV =

ifneq ($(CONNECT_SERVER),)
  TEST_ENV += CONNECT_SERVER=$(CONNECT_SERVER)
endif
ifneq ($(CONNECT_API_KEY),)
  TEST_ENV += CONNECT_API_KEY=$(CONNECT_API_KEY)
endif

# NOTE: See the `dist` target for why this exists.
SOURCE_DATE_EPOCH := $(shell date +%s)
export SOURCE_DATE_EPOCH

.PHONY: all-tests
all-tests: all-images test-2.7 test-3.5 test-3.6 test-3.7 test-3.8

.PHONY: all-images
all-images: image-2.7 image-3.5 image-3.6 image-3.7 image-3.8

image-%:
	docker build -t rsconnect-python:$* --build-arg BASE_IMAGE=python:$*-slim .

shell-%:
	$(RUNNER) '$(SHELL_COMMAND)'

test-%:
	$(RUNNER) '$(TEST_ENV) $(TEST_COMMAND)'

mock-test-%: clean-stores
	@$(MAKE) -C mock_connect image up
	@sleep 1
	CONNECT_SERVER=http://$(HOSTNAME):3939 CONNECT_API_KEY=0123456789abcdef0123456789abcdef $(MAKE) test-$*
	@$(MAKE) -C mock_connect down

fmt-%:
	$(RUNNER) 'pipenv run black .'

.PHONY: fmt-2.7
fmt-2.7: .fmt-unsupported

.PHONY: fmt-3.5
fmt-3.5: .fmt-unsupported

.PHONY: .fmt-unsupported
.fmt-unsupported:
	@echo ERROR: This python version cannot run the fmting tools
	@exit 1

deps-%:
	$(RUNNER) 'pipenv run ./scripts/install-deps'

lint-%:
	$(RUNNER) 'pipenv run black --check --diff .'
	$(RUNNER) 'pipenv run flake8 rsconnect/'

.PHONY: lint-2.7
lint-2.7: .lint-unsupported

.PHONY: lint-3.5
lint-3.5: .lint-unsupported

.PHONY: .lint-unsupported
.lint-unsupported:
	@echo ERROR: This python version cannot run the linting tools
	@exit 1

.PHONY: clean clean-stores
clean:
	@rm -rf build dist rsconnect_python.egg-info

clean-stores:
	@find . -name "rsconnect-python" | xargs rm -rf

.PHONY: shell
shell: RUNNER = bash -c
shell: shell-3.8

.PHONY: test
test: RUNNER = bash -c
test: test-3.8

.PHONY: lint
lint: RUNNER = bash -c
lint: lint-3.8

.PHONY: deps
deps: RUNNER = bash -c
deps: deps-3.8

.PHONY: fmt
fmt: RUNNER = bash -c
fmt: fmt-3.8

.PHONY: docs
docs:
	$(MAKE) -C docs

.PHONY: version
version:
	@echo $(VERSION)

# NOTE: Wheels won't get built if _any_ file it tries to touch has a timestamp
# before 1980 (system files) so the $(SOURCE_DATE_EPOCH) current timestamp is
# exported as a point of reference instead.
.PHONY: dist
dist:
	pipenv run python setup.py bdist_wheel
	pipenv run twine check $(BDIST_WHEEL)
	rm -vf dist/*.egg
	@echo "::set-output name=whl::$(BDIST_WHEEL)"
	@echo "::set-output name=whl_basename::$(notdir $(BDIST_WHEEL))"

.PHONY: sync-to-s3
sync-to-s3:
	aws s3 cp --acl bucket-owner-full-control \
		$(BDIST_WHEEL) \
		$(S3_PREFIX)/$(VERSION)/$(notdir $(BDIST_WHEEL))

.PHONY: sync-latest-to-s3
sync-latest-to-s3:
	aws s3 cp --acl bucket-owner-full-control \
		$(BDIST_WHEEL) \
		$(S3_PREFIX)/latest/rsconnect_python-latest-py2.py3-none-any.whl
