# Start with a modern Python version
FROM python:3.11-slim

# Set the working directory inside the container
WORKDIR /app

# Copy and install Python requirements first to leverage Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- KEY CHANGE ---
# Install the Chromium browser AND all its required system dependencies in one step.
# The --with-deps flag is the recommended way to solve the dependency issue.
RUN playwright install --with-deps chromium

# Copy your application code into the container
COPY . .

# Define the command to run your application
# This command is correct for running a FastAPI app on Render.
CMD ["gunicorn", "-w", "4", "-k", "uvicorn.workers.UvicornWorker", "main:app", "--bind", "0.0.0.0:$PORT"]
