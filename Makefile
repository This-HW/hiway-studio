.PHONY: dev migrate seed gen-types test lint-imports demo web-install help

# Python interpreter: repo-local .venv first, then $PYTHON, then python3.
PYTHON := $(shell \
	if [ -x .venv/bin/python ]; then echo .venv/bin/python; \
	elif [ -n "$$PYTHON" ]; then echo "$$PYTHON"; \
	else echo python3; fi)

dev: ## 전체 개발 스택 기동 (compose: postgres, redis, api, worker + web dev server)
	docker compose up --build -d
	@echo "API: http://localhost:$${API_PORT:-8000}"
	cd apps/web && npm run dev

web-install: ## web 의존성 설치 (최초 1회)
	cd apps/web && npm install

migrate: ## DB 마이그레이션 적용
	docker compose run --rm api alembic upgrade head

seed: ## 모델·카테고리팩 seed 로드 (멱등)
	docker compose run --rm api python -m app.seed

gen-types: ## contracts JSON에서 TS 타입 생성
	$(PYTHON) packages/contracts/tools/gen_types.py

test: ## API pytest + web typecheck
	docker compose run --rm api pytest -q
	cd apps/web && npm run typecheck

lint-imports: ## 모듈 의존성 방향 검사
	$(PYTHON) tools/lint_imports.py

demo: ## stub 어댑터로 전체 P0 파이프라인 데모 (API 키 불필요, docker 불필요)
	$(PYTHON) tools/demo.py

help: ## 사용 가능한 타겟 목록
	@grep -E '^[a-z-]+: ##' $(MAKEFILE_LIST) | sort | awk -F': ## ' '{printf "  %-12s %s\n", $$1, $$2}'
