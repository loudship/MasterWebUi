FROM python:3.11-slim@sha256:a3ab0b966bc4e91546a033e22093cb840908979487a9fc0e6e38295747e49ac0
WORKDIR /app
# aiohttp + pydantic are required by the mounted pipeline modules
# (pipelines/langgraph_router.py), not just the runtime itself.
RUN pip install --no-cache-dir fastapi uvicorn aiohttp pydantic
COPY services/pipelines/app.py ./app.py
ENV PIPELINES_DIR=/app/pipelines
EXPOSE 9099
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9099"]
