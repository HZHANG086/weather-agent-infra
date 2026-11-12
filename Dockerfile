FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY weather_agent.py .
COPY rag_infra/ rag_infra/
COPY fixtures/rag-suite.json fixtures/rag-suite.json
USER 10001:10001
EXPOSE 8080
CMD ["python", "weather_agent.py", "--host", "0.0.0.0"]
