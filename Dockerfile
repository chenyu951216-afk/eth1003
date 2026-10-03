FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 ETH1003_DATA_DIR=/data ETH1003_CLOUD=1 PORT=8080
WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[test,backtest]"
EXPOSE 8080
CMD ["python", "main.py"]
