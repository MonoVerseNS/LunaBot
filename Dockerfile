FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip pip install -r requirements.txt
COPY luna_bot.py README.md ./
COPY prompts ./prompts
RUN mkdir -p /app/data
HEALTHCHECK --interval=60s --timeout=10s --retries=3 CMD python -c "import pathlib; print('ok')" || exit 1
CMD ["python", "luna_bot.py"]
