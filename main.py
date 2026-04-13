import uvicorn
from api import api
from config import settings


def main():
    uvicorn.run(
        api,
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
