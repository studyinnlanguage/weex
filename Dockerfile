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
# Use --no-deps for binance-futures-connector to avoid its old requests pin
RUN pip install --no-cache-dir \
    "Flask>=3.0.3,<4.0" \
    "Flask-SocketIO>=5.3.6,<6.0" \
    "python-engineio>=4.9.1,<5.0" \
    "python-socketio>=5.11.4,<6.0" \
    "simple-websocket>=1.0.0,<2.0" \
    "requests>=2.25.1" \
    "python-binance>=1.0.19" \
    "pandas>=2.0.0" \
    "numpy>=1.24.0" \
    "pyarrow>=15.0.0" \
    "python-dotenv>=1.0.1" \
    && pip install --no-cache-dir --no-deps "binance-futures-connector>=1.5.0"

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
