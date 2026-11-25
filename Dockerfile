FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
# pre-download the local embedding + reranker models into the image
RUN python -c "from app.rag.embeddings import embed_query, rerank; embed_query('warmup'); rerank('a', ['b'])"
EXPOSE 8000 8501
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
