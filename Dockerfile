# Host inside your own cloud tenant instead of a public service.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends libreoffice-writer fonts-liberation && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8501
ENV STREAMLIT_SERVER_HEADLESS=true
# Supply APP_PASSWORD, LLM_BASE_URL, LLM_API_KEY, LLM_MODEL, LLM_VISION as environment variables or mount .streamlit/secrets.toml
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
