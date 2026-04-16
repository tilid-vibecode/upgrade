.PHONY: help install docker-build docker-push docker-run clean-local docker-shell

AWS_ACCOUNT_ID  ?= 123456789012
AWS_REGION      ?= us-east-1
REPO_URL        := $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/mula
IMAGE_NAME      := server
GIT_COMMIT      := $(shell git rev-parse --short HEAD)
TAG             := sha-$(GIT_COMMIT)

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install Python dependencies
	@echo "🚀 Installing dependencies"
	pip install -r requirements.txt

login: ## Login to AWS ECR
	@echo "🚀 Login into ECR"
	aws ecr get-login-password --region $(AWS_REGION) | \
		docker login --username AWS --password-stdin $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com

build-image: ## Build server Docker image
	@echo "🚀 Building server image: $(REPO_URL)/$(IMAGE_NAME):$(TAG)"
	DOCKER_BUILDKIT=1 docker buildx build --platform=linux/amd64 -t $(REPO_URL)/$(IMAGE_NAME):$(TAG) ./

push-image: ## Push server Docker image
	@echo "🚀 Pushing: $(REPO_URL)/$(IMAGE_NAME):$(TAG)"
	docker push $(REPO_URL)/$(IMAGE_NAME):$(TAG)

docker-build: login build-image ## Login + build

docker-push: login build-image push-image ## Login + build + push

docker-run: ## Run server container locally
	@echo "🚀 Running server image: $(REPO_URL)/$(IMAGE_NAME):$(TAG)"
	docker run -p 8000:8000 --env-file ../.env --rm $(REPO_URL)/$(IMAGE_NAME):$(TAG)

docker-shell: ## Shell into the server image
	docker run -it --rm $(REPO_URL)/$(IMAGE_NAME):$(TAG) /bin/bash

clean-local: ## Remove local Docker images
	docker rmi $(REPO_URL)/$(IMAGE_NAME):$(TAG) || true

.DEFAULT_GOAL := help
