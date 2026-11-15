from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from backend.api.routes import app as router
from backend.database import db
from backend.services.approval_poller import ApprovalPoller

@asynccontextmanager
async def lifespan(app):
    poller=ApprovalPoller(); poller.start()
    try: yield
    finally: poller.stop()

app = FastAPI(title='Real Estate AI Deal Analyzer',lifespan=lifespan)
if Path('frontend/dist/assets').exists():
    app.mount('/assets', StaticFiles(directory='frontend/dist/assets'), name='assets')
app.include_router(router)
