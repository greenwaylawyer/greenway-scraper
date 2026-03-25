FROM mcr.microsoft.com/playwright/python:v1.48.0-noble

# Set working directory
WORKDIR /app

# Install dependencies first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Ensure playwright browsers are installed
RUN playwright install chromium

# Copy the application code
COPY . .

# Ensure required directories exist and have proper permissions
RUN mkdir -p output checkpoints

# Set python unbuffered mode for better logs
ENV PYTHONUNBUFFERED=1

# Entrypoint setup
ENTRYPOINT ["python", "run.py"]

# Default command shows help
CMD ["--help"]
