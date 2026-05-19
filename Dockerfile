FROM python:3.11-slim

# --- Install Java (required for MPXJ via JPype) ---
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        default-jdk \
        wget \
        curl \
    && rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="${JAVA_HOME}/bin:${PATH}"

# --- Create working directory ---
WORKDIR /app

# --- Install Python dependencies ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Download MPXJ JAR (latest stable) ---
RUN mkdir -p /app/lib && \
    wget -q "https://repo1.maven.org/maven2/net/sf/mpxj/mpxj/13.4.0/mpxj-13.4.0.jar" \
         -O /app/lib/mpxj.jar

# --- Copy application code ---
COPY app.py .

# --- Hugging Face Spaces requires port 7860 ---
EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
