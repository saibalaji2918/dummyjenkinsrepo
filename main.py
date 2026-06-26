from fastapi import FastAPI
from sqlalchemy.orm import Session
from transaction import models, schemas, database
from .routes import items 
from fastapi.openapi.utils import get_openapi


app = FastAPI()
app.include_router(items.router)

models.Base.metadata.create_all(bind=database.engine)

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    openapi_schema = get_openapi(
        title="My Transaction API",
        version="1.0.0",
        description="API for handling item transactions",
        routes=app.routes,
    )
    openapi_schema["components"]["securitySchemes"] = {
        "basicAuth": {
            "type": "http",
            "scheme": "basic"
        }
    }
    for path in openapi_schema["paths"].values():
        for method in path.values():
            method["security"] = [{"basicAuth": []}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema

app.openapi = custom_openapi
