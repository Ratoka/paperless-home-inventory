FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV DATA_DIR=/data
EXPOSE 7070
# Source is volume-mounted at /app at runtime — no COPY of app code needed.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7070"]
