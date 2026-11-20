FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    OTP_HOST=0.0.0.0 OTP_PORT=8000

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY otp_service ./otp_service
COPY scenarios ./scenarios
COPY attack.py scenario_runner.py ./

# Не root: сервису нужны права только на outbox/ и logs/.
RUN useradd --create-home otp && mkdir -p outbox logs && chown -R otp:otp /app
USER otp

EXPOSE 8000
CMD ["python", "-m", "otp_service"]
