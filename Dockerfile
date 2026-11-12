FROM python:3.12-slim
WORKDIR /app
COPY weather_agent.py .
USER 10001:10001
EXPOSE 8080
CMD ["python", "weather_agent.py", "--host", "0.0.0.0"]
