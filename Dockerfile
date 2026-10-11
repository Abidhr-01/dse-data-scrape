FROM mcr.microsoft.com/playwright/python:latest
WORKDIR /app
RUN pip install --no-cache-dir playwright
COPY app.py scrape.py ./
COPY web ./web
ENV HOST=0.0.0.0 PYTHONUNBUFFERED=1
CMD ["python", "app.py"]
