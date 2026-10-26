FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /service
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && groupadd --gid 10001 mortgage && useradd --uid 10001 --gid mortgage --no-create-home mortgage
COPY --chown=mortgage:mortgage app ./app
COPY --chown=mortgage:mortgage static ./static
COPY --chown=mortgage:mortgage config ./config
COPY --chown=mortgage:mortgage fixtures ./fixtures
RUN mkdir -p /service/downloads /service/volume && chown -R mortgage:mortgage /service/downloads /service/volume
USER 10001:10001
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
