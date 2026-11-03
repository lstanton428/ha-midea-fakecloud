FROM python:3.13-slim
# midea-lan is the library behind the midea_ac_lan integration (local control).
RUN pip install --no-cache-dir midea-lan==2026.10.0
COPY fakecloud.py lanbridge.py /app/
CMD ["python3", "-u", "/app/fakecloud.py"]
