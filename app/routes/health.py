from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/")
def root() -> dict[str, str]:
    return {"message": "SensorThings testbed connector is running"}


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
