FROM python:3.12-slim

WORKDIR /locust

RUN pip install --no-cache-dir locust>=2.32.0 Pillow>=11.0.0

COPY locustfile.py /locust/locustfile.py

ENTRYPOINT ["locust"]
