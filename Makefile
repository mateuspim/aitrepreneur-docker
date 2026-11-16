# Convenience wrapper around docker compose. Run `make help` for a summary.
# APP targets: ai-toolkit (default profile), krea2, ideogram, ltx, minimax,
# anima, klein, qwen, moss, mmimage, loramgr (standalone LoRA Manager).

.DEFAULT_GOAL := help
COMPOSE = sudo docker compose --profile krea2 --profile ideogram --profile ltx --profile minimax --profile anima --profile klein --profile qwen --profile moss --profile mmimage --profile loramgr

help: ## List available targets
	@grep -E '^[a-z%-]+:.*##' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-18s %s\n", $$1, $$2}'

tui: ## Open the interactive service dashboard
	sudo -v
	uv run scripts/tui.py

setup-model-dirs: ## Reorganise shared loras/controlnet/checkpoints/embeddings into app subfolders (dry-run; pass ARGS=--apply)
	@sudo bash scripts/migrate-model-dirs.sh $(ARGS)

setup: ## One-time: create .env from the template
	@test -f .env || (cp .env.example .env && echo "Created .env — edit it to add your HF_TOKEN.")

build: setup ## Build every image (cached layers reused)
	$(COMPOSE) build

proxy: setup ## Start the authenticated proxy
	sudo docker compose up -d --no-deps proxy

auth-password: setup ## Set the shared proxy login
	python3 scripts/auth-password.py

up: proxy ## Start ai-toolkit (training UI) at :8675
	sudo docker compose up -d --force-recreate ai-toolkit

krea2 ideogram anima moss klein qwen mmimage minimax ltx: proxy ## Start one ComfyUI app (krea2 :8188, ideogram :8189, anima :8190, moss :8191, klein :8192, qwen :8193, mmimage :8194, minimax :8195, ltx :8196)
	sudo docker compose --profile $@ up -d --force-recreate $@
	@lan_ip=$$(ip -4 -o addr show scope global 2>/dev/null | awk '$$2 !~ /^(docker|br-|veth)/ {print $$4}' | cut -d/ -f1 | paste -sd, -); \
	 port=$$(sudo docker compose --profile $@ port proxy $$(case $@ in krea2) echo 8188;; ideogram) echo 8189;; anima) echo 8190;; moss) echo 8191;; klein) echo 8192;; qwen) echo 8193;; mmimage) echo 8194;; minimax) echo 8195;; ltx) echo 8196;; esac) 2>/dev/null | awk -F: '{print $$NF}'); \
	 echo "UI:  http://localhost:$${port:-?}  |  http://$${lan_ip:-?}:$${port:-?}"; \
	 sleep 2; \
	 if sudo docker compose --profile $@ logs --since 1m $@ 2>/dev/null | grep -q '\[DL\]'; then \
	   echo "MODELS: downloading (first start) — watch progress with: make logs-$@"; \
	 else \
	   echo "MODELS: no active download — models already present or container still booting; verify with: make logs-$@"; \
	 fi

loramgr: proxy ## Start LoRA Manager (standalone, no ComfyUI) at :8197/loras
	sudo docker compose --profile loramgr up -d --force-recreate loramgr
	@lan_ip=$$(ip -4 -o addr show scope global 2>/dev/null | awk '$$2 !~ /^(docker|br-|veth)/ {print $$4}' | cut -d/ -f1 | paste -sd, -); \
	 port=$$(sudo docker compose --profile loramgr port proxy 8197 2>/dev/null | awk -F: '{print $$NF}'); \
	 echo "UI:  http://localhost:$${port:-?}/loras  |  http://$${lan_ip:-?}:$${port:-?}/loras"; \
	 echo "CivitAI API key + model folders: Settings page in the UI (first run creates ./data/loramgr/settings.json)"

up-all: proxy ## Start ai-toolkit + all ComfyUI apps + LoRA Manager
	$(COMPOSE) up -d --force-recreate

down: ## Stop everything
	$(COMPOSE) down

status: ## Show state of all services (e.g. make status)
	$(COMPOSE) ps -a --format 'table {{.Name}}\t{{.State}}\t{{.Status}}\t{{.Ports}}'

stop-%: ## Stop one service (e.g. make stop-krea2)
	$(COMPOSE) stop $*

logs-%: ## Follow logs of one service (e.g. make logs-krea2)
	$(COMPOSE) logs -f $*

shell-%: ## Shell into a running service (e.g. make shell-ltx)
	$(COMPOSE) exec $* bash

upgrade: setup ## Rebuild all images from fresh upstream clones and restart running ones
	CACHEBUST=$$(date +%s) $(COMPOSE) build
	$(COMPOSE) up -d --no-deps $$(sudo docker compose ps --services 2>/dev/null)

upgrade-%: setup ## Rebuild one app from a fresh clone (e.g. make upgrade-krea2)
	CACHEBUST=$$(date +%s) $(COMPOSE) build $*

version: ## Show upstream commits baked into the images
	@for s in ai-toolkit:/app/ai-toolkit-commit.txt comfyui-krea2:/opt/app/comfyui-commit.txt comfyui-ideogram:/opt/app/comfyui-commit.txt comfyui-ltx:/opt/app/comfyui-commit.txt comfyui-minimax:/opt/app/comfyui-commit.txt comfyui-anima:/opt/app/comfyui-commit.txt comfyui-klein:/opt/app/comfyui-commit.txt comfyui-qwen:/opt/app/comfyui-commit.txt comfyui-moss:/opt/app/comfyui-commit.txt comfyui-mmimage:/opt/app/comfyui-commit.txt lora-manager:/opt/lora-manager-commit.txt; do \
	  img="$${s%%:*}:local"; f="$${s#*:}"; \
	  sudo docker image inspect "$$img" >/dev/null 2>&1 && \
	    echo "$$img  $$(sudo docker run --rm --entrypoint cat $$img $$f)" || true; \
	done

gpu-check: ## Verify a container sees the GPU and torch has CUDA
	sudo docker compose run --rm --entrypoint "" ai-toolkit \
		python -c "import torch; print('torch', torch.__version__, '| cuda available:', torch.cuda.is_available(), '|', torch.cuda.get_device_name(0) if torch.cuda.is_available() else '-')"

delete-input: ## Delete all persisted ComfyUI input files (keeps the input folders)
	@set -e; \
	for input_dir in data/krea2/input data/ideogram/input data/ltx/input data/minimax/input data/anima/input data/klein/input data/qwen/input data/moss/input data/mmimage/input; do \
		if [ -d "$$input_dir" ]; then \
			echo "Clearing $$input_dir"; \
			sudo find "$$input_dir" -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +; \
		fi; \
	done

.PHONY: tui proxy auth-password help setup setup-model-dirs build up up-all krea2 ideogram ltx minimax anima klein qwen moss mmimage loramgr down status upgrade version gpu-check delete-input
