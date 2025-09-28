# Start with a modern Python version
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy and install Python requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install the Chromium browser AND all its required system dependencies in one step.
RUN playwright install --with-deps chromium

# Copy your application code into the container
COPY . .

# --- KEY CHANGE ---
# Define the command to run your application using the "shell form"
# This allows the $PORT environment variable to be correctly interpreted by the shell.
CMD gunicorn -w 4 -k uvicorn.workers.UvicornWorker main:app --bind "0.0.0.0:$PORT"

