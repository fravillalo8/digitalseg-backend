# Base image desde el MIRROR de AWS ECR Public (misma python:3.11-slim oficial),
# NO desde Docker Hub: registry-1.docker.io devolvió un 500 y tumbó un auto-deploy
# (build 3ccf8197, 2026-07-13). ECR Public espeja las imágenes oficiales sin los
# rate-limits ni la flakiness de Docker Hub → builds mucho más confiables.
FROM public.ecr.aws/docker/library/python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
