FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_ADDRESS=0.0.0.0 \
    STREAMLIT_SERVER_PORT=8501

WORKDIR /app

RUN addgroup --system wordpressgenius \
    && adduser --system --ingroup wordpressgenius wordpressgenius

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY . .

RUN mkdir -p data logs \
    && chown -R wordpressgenius:wordpressgenius /app

USER wordpressgenius

EXPOSE 8501 8081

CMD ["python", "launch.py"]
