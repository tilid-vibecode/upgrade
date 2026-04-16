.PHONY: help install docker-build docker-push docker-run clean-local docker-shell

AWS_ACCOUNT_ID  ?= 123456789012
AWS_REGION      ?= eu-west-1
REPO_URL        := $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/upg
IMAGE_NAME      := client
GIT_COMMIT      := $(shell git rev-parse --short HEAD)
TAG             := sha-$(GIT_COMMIT)

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

install: ## Install npm dependencies
	@echo "🚀 Installing dependencies"
	npm install

login: ## Login to AWS ECR
	@echo "🚀 Login into ECR"
	aws ecr get-login-password --region $(AWS_REGION) | \
		docker login --username AWS --password-stdin $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com

build-image: ## Build client Docker image
	@echo "🚀 Building client image: $(REPO_URL)/$(IMAGE_NAME):$(TAG)"
	DOCKER_BUILDKIT=1 docker buildx build --platform=linux/amd64 -t $(REPO_URL)/$(IMAGE_NAME):$(TAG) ./

push-image: ## Push client Docker image
	@echo "🚀 Pushing: $(REPO_URL)/$(IMAGE_NAME):$(TAG)"
	docker push $(REPO_URL)/$(IMAGE_NAME):$(TAG)

docker-build: login build-image ## Login + build

docker-push: login build-image push-image ## Login + build + push

docker-run: ## Run client container locally
	@echo "🚀 Running client image: $(REPO_URL)/$(IMAGE_NAME):$(TAG)"
	docker run -p 3000:3000 --rm $(REPO_URL)/$(IMAGE_NAME):$(TAG)

docker-shell: ## Shell into the client image
	docker run -it --rm $(REPO_URL)/$(IMAGE_NAME):$(TAG) /bin/sh

clean-local: ## Remove local Docker images
	docker rmi $(REPO_URL)/$(IMAGE_NAME):$(TAG) || true

.DEFAULT_GOAL := help
