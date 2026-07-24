# Use official Python image
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p logs user_configs bot-engine/logs

# Expose port
EXPOSE 5000

# Set environment variables
ENV PORT=5000
ENV HOST=0.0.0.0
ENV PYTHONUNBUFFERED=1

# Run the application
CMD ["python", "app.py"]
