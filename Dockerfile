FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir pypdf python-docx openpyxl
WORKDIR /app
COPY app.py .
COPY winning_numbers.json .
ENV PORT=8000
EXPOSE 8000
CMD ["python", "app.py"]
