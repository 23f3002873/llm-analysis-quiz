# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Install system deps required by Playwright browsers
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gnupg \
    ca-certificates \
    build-essential \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libasound2 \
    libgbm1 \
    libpangocairo-1.0-0 \
    fonts-liberation \
    locales \
    libgtk-3-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app

RUN pip install --upgrade pip

# Ensure Playwright will install browsers into the package (not user cache)
ENV PLAYWRIGHT_BROWSERS_PATH=0
# Keep Python output unbuffered for real-time logs
ENV PYTHONUNBUFFERED=1
ENV PORT=8000

# Install Python dependencies
RUN pip install -r requirements.txt

# Install Playwright browsers into the package location (using deps)
RUN playwright install --with-deps

# Expose the port used by the app
EXPOSE 8000

# Start the app
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
