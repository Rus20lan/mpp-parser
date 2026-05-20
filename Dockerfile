FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends default-jdk maven && \
    rm -rf /var/lib/apt/lists/*

ENV JAVA_HOME=/usr/lib/jvm/default-java
ENV PATH="${JAVA_HOME}/bin:${PATH}"
ENV MPXJ_LIB_DIR=/app/lib

WORKDIR /app

# Download MPXJ + all transitive dependencies via Maven.
# The final test is important: without mpxj-*.jar JPype starts,
# but `from net.sf.mpxj...` fails at application startup.
RUN mkdir -p /app/lib && \
    cd /tmp && \
    printf '%s\n' \
      '<project xmlns="http://maven.apache.org/POM/4.0.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 https://maven.apache.org/xsd/maven-4.0.0.xsd">' \
      '<modelVersion>4.0.0</modelVersion>' \
      '<groupId>local</groupId>' \
      '<artifactId>mpxj-loader</artifactId>' \
      '<version>1.0.0</version>' \
      '<dependencies>' \
      '<dependency>' \
      '<groupId>net.sf.mpxj</groupId>' \
      '<artifactId>mpxj</artifactId>' \
      '<version>15.3.1</version>' \
      '</dependency>' \
      '</dependencies>' \
      '</project>' > pom.xml && \
    mvn -q dependency:copy-dependencies -DincludeScope=runtime -DoutputDirectory=/app/lib && \
    mvn -q dependency:copy -Dartifact=net.sf.mpxj:mpxj:15.3.1 -DoutputDirectory=/app/lib && \
    test -n "$(find /app/lib -name 'mpxj-*.jar' -print -quit)" && \
    ls -lah /app/lib && \
    rm -rf /tmp/pom.xml /root/.m2 && \
    apt-get purge -y maven && \
    apt-get autoremove -y

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
