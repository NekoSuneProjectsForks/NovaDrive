FROM node:22-alpine AS assets

WORKDIR /app

COPY package.json package-lock.json ./
COPY tailwind.config.js ./
COPY novadrive/static/src ./novadrive/static/src
COPY novadrive/static/js ./novadrive/static/js
COPY novadrive/templates ./novadrive/templates
RUN npm ci
RUN npm run build:css

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .
COPY --from=assets /app/novadrive/static/dist/output.css /app/novadrive/static/dist/output.css

EXPOSE 5000 5051

CMD ["waitress-serve", "--host=0.0.0.0", "--port=5000", "--call", "novadrive.app:create_app"]
