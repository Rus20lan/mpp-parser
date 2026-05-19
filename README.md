---
title: MPP Parser for R&D Change Log
emoji: 📊
colorFrom: blue
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# MPP Parser Microservice

Parses Microsoft Project `.mpp` files and returns structured JSON with tasks, dates, resources, costs, and hierarchy.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Service info |
| `/health` | GET | Health check (JVM status) |
| `/parse` | POST | Full parse — nested JSON with tasks, resources, predecessors |
| `/parse/compact` | POST | Flat table-ready format for Google Sheets import |

## Usage

```bash
curl -X POST https://YOUR-SPACE.hf.space/parse/compact \
  -F "file=@project.mpp"
```

## Tech Stack

- **MPXJ 13.4** (Java library) via JPype
- **FastAPI** + Uvicorn
- **OpenJDK** (Debian default-jdk)

## Supported Formats

`.mpp`, `.mpt`, `.mpx`, `.xml`, `.xer`, `.pmxml`
