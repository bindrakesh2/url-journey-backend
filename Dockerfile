# Start with a modern Python version
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# 1. Install system dependencies required by Playwright's browsers
RUN apt-get update && apt-get install -y \
    libnss3 \
    libnspr4 \
    libdbus-1-3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    --no-install-recommends

# 2. Copy and install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Install the Chromium browser for Playwright
# This is a crucial step that downloads the browser into the Docker image
RUN playwright install chromium

# 4. Copy your application code into the container
COPY . .

# 5. Define the command to run your application
# Render provides the $PORT environment variable, which Gunicorn will use.
CMD gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app --bind "0.0.0.0:$PORT"
