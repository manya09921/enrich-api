from dotenv import load_dotenv
from fastapi import FastAPI

load_dotenv()

from routes.enrich import router as enrich_router

app = FastAPI(title="Book Record Enrichment API")
app.include_router(enrich_router)


@app.get("/health")
def health():
    return {"status": "ok"}
