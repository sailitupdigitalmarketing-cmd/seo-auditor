FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY seo_auditor ./seo_auditor
ENV SEO_DB_PATH=/data/seo.db AUDIT_HOUR_UTC=6
VOLUME /data
EXPOSE 8000
CMD ["uvicorn", "seo_auditor.app:app", "--host", "0.0.0.0", "--port", "8000"]
