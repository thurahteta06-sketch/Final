FROM python:3.11-slim

# Use a minimal, safe image that runs the included safe placeholder (main_safe.py)
# This avoids deploying the brute-force bot by default.

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install system CA certs and minimal packages
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install only the lightweight dependency needed by main_safe.py
RUN pip install --no-cache-dir aiohttp

# Copy only the safe placeholder and minimal repo files
COPY main_safe.py ./

# Railway provides the PORT env var at runtime. main_safe.py uses PORT env or 8000 default.
EXPOSE 8000

CMD ["python", "main_safe.py"]
