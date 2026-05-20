FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends default-jdk maven && \
    rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="${JAVA_HOME}/bin:${PATH}"

WORKDIR /app

# Download MPXJ + all transitive dependencies via Maven
RUN mkdir -p /app/lib && \
    cd /tmp && \
    echo '<project><modelVersion>4.0.0</modelVersion><groupId>t</groupId><artifactId>t</artifactId><version>1</version><dependencies><dependency><groupId>net.sf.mpxj</groupId><artifactId>mpxj</artifactId><version>15.3.1</version></dependency></dependencies></project>' > pom.xml && \
    mvn dependency:copy-dependencies -DoutputDirectory=/app/lib -q && \
    mvn dependency:copy -Dartifact=net.sf.mpxj:mpxj:15.3.1 -DoutputDirectory=/app/lib -q && \
    rm -rf /tmp/pom.xml /root/.m2 && \
    apt-get purge -y maven && apt-get autoremove -y

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
