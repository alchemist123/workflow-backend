FROM python:3.11-slim

# Unbuffered stdout so Cloud Logging sees log lines as they happen.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a code change does not re-resolve the environment.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Run as a non-root user.
RUN useradd --create-home --uid 10001 agent && chown -R agent:agent /app
USER agent

# Cloud Run injects PORT; 8080 is its default.
ENV PORT=8080
EXPOSE 8080

# JSON form (so signals reach the process directly) wrapped in `sh -c` because
# Cloud Run injects PORT at start-up and it has to be expanded then, not now.
CMD ["sh", "-c", "exec uvicorn main:a2a_app --host 0.0.0.0 --port ${PORT}"]
