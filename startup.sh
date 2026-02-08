#!/bin/bash
source /home/site/wwwroot/antenv/bin/activate
exec gunicorn -w 4 -k uvicorn.workers.UvicornWorker --bind=0.0.0.0:8000 app.main:app
